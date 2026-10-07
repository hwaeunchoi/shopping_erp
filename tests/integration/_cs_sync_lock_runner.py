"""
tests/integration/_cs_sync_lock_runner.py
---------------------------------------------
실제(격리) PostgreSQL 위에서 CS 문의 동기화의 (platform, source) advisory lock과 이를 쓰는
catch-up 서비스·stale 작업 정리·수동 sync 경로의 경합을 검증한다. SQLite에는 advisory lock이
없어(프로세스 내 대체 잠금만 있다) 실제 두 DB 커넥션 경합은 PostgreSQL에서만 재현된다.

시나리오:
1) lock_contention - 같은 (platform, source)는 두 번째 커넥션이 잡지 못하고, 다른 source/다른
   platform은 서로 막지 않으며, 해제(정상/연결 종료) 후에는 다시 잡힌다.
2) concurrent_sync_platform - 두 worker가 같은 플랫폼 sync_platform()을 동시에 실행하면 source마다
   정확히 하나만 외부 호출(스텁 fetch)·DB 쓰기를 하고 나머지는 ALREADY_RUNNING이다(중복 case 0건).
3) stale_probe_sees_real_lock - 실제 잠금을 들고 있는 동안 stale 작업 정리는 RUNNING 행을 건드리지
   않고, 해제 후에는 FAILED/PROCESS_INTERRUPTED로 정리한다.
4) manual_vs_running_sync - 자동 실행이 진행 중일 때 수동 sync(두 source 잠금 전부 요구)는 거절된다.

tests/integration/test_cs_sync_lock_pg.py가 격리된 1회성 postgres 컨테이너를 띄우고 이 스크립트를
같은(--internal) 네트워크의 러너 컨테이너에서 실행한다. 스키마는 Base.metadata.create_all()로
만든다. 실제 채널 API는 호출하지 않는다(스텁 커넥터만 사용, 합성 데이터만 사용).
결과를 JSON 한 줄로 표준출력 마지막 줄에 출력한다.
"""

import json
import os
import sys
import threading
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

db_host = os.environ["CSLOCK_DB_HOST"]
db_user = os.environ["CSLOCK_DB_USER"]
db_password = os.environ["CSLOCK_DB_PASSWORD"]
db_name = os.environ["CSLOCK_DB_NAME"]
scenario = os.environ["CSLOCK_SCENARIO"]

os.environ["DATABASE_URL"] = f"postgresql+psycopg://{db_user}:{db_password}@{db_host}:5432/{db_name}"

from sqlalchemy import event, func, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from config.settings import settings  # noqa: E402
from core.database import SessionLocal, engine  # noqa: E402
from models import Base  # noqa: E402
from models.cs_case import CsCase, CsCaseHistory  # noqa: E402
from models.extra import IntegrationStatus, TaskExecutionHistory  # noqa: E402
from models.platform import Platform  # noqa: E402
from services.cs_inquiry_catchup_service import CsInquiryCatchupService  # noqa: E402
from services.cs_sync_lock import cs_sync_source_lock  # noqa: E402
from services.stale_task_recovery_service import recover_stale_running_tasks  # noqa: E402

settings.cs_inquiry_sync_enabled = True
Base.metadata.create_all(engine)

CC = "COUPANG_CALL_CENTER"
PR = "COUPANG_PRODUCT_INQUIRY"


def _make_platform() -> int:
    setup = SessionLocal()
    platform = Platform(
        code=f"pf-{uuid.uuid4().hex[:8]}",
        name="잠금테스트플랫폼",
        connector_class="StubConnector",
        settlement_cycle_days=7,
        is_active=True,
    )
    setup.add(platform)
    setup.commit()
    platform_id = platform.id
    setup.close()
    return platform_id


class _GatedConnector:
    """fetch는 gate Event가 열릴 때까지 머문다 - 시간(sleep)이 아니라 "다른 worker가 잠금 시도를 끝냈다"는
    사건으로 겹침을 보장한다(타이밍에 의존하지 않는 결정적 시나리오). gate가 None이면 바로 반환한다."""

    supports_inquiry_sync = True
    supports_product_inquiry_sync = True

    def __init__(self, gate: Optional[threading.Event], on_fetch: Optional[threading.Event] = None) -> None:
        self.gate = gate
        self.on_fetch = on_fetch
        self.calls: list[str] = []

    def _item(self, inquiry_id: str) -> dict[str, Any]:
        return {
            "platform_inquiry_id": inquiry_id,
            "content": "합성 문의",
            "inquiry_at": datetime.now(timezone.utc).replace(tzinfo=None),
            "raw_status": "progress:requestAnswer",
            "needs_answer": True,
            "platform_order_no": None,
            "customer_phone": None,
        }

    def _wait(self) -> None:
        if self.on_fetch is not None:
            self.on_fetch.set()
        if self.gate is not None and not self.gate.wait(timeout=30):
            raise RuntimeError("gate timeout")

    def fetch_inquiries(self, start_date, end_date, *, max_pages=None, max_retries=None, request_budget=None):
        self.calls.append(CC)
        request_budget.consume("coupang")
        self._wait()
        return [self._item("LOCK-CC-1")]

    def fetch_product_inquiries(self, start_date, end_date, *, max_pages=None, max_retries=None, request_budget=None):
        self.calls.append(PR)
        request_budget.consume("coupang")
        self._wait()
        return [self._item("LOCK-PR-1")]


def scenario_lock_contention() -> dict:
    pid = _make_platform()
    other_pid = _make_platform()
    result: dict[str, Any] = {}

    with cs_sync_source_lock(pid, CC, engine) as first:
        with cs_sync_source_lock(pid, CC, engine) as second:
            result["same_source_second_acquired"] = second
        with cs_sync_source_lock(pid, PR, engine) as other_source:
            result["other_source_acquired"] = other_source
        with cs_sync_source_lock(other_pid, CC, engine) as other_platform:
            result["other_platform_acquired"] = other_platform
        result["first_acquired"] = first

    with cs_sync_source_lock(pid, CC, engine) as after_release:
        result["reacquired_after_release"] = after_release

    # 비정상 종료 흉내: 같은 키를 raw 커넥션이 잡은 채 unlock 없이 커넥션만 닫으면(프로세스 크래시와 동일)
    # 세션 잠금은 DB가 자동으로 해제한다.
    from services.cs_sync_lock import _ADVISORY_CLASSID, lock_object_id

    raw = engine.connect()
    got = raw.execute(
        text("SELECT pg_try_advisory_lock(:c, :o)"), {"c": _ADVISORY_CLASSID, "o": lock_object_id(pid, PR)}
    ).scalar()
    result["raw_held"] = bool(got)
    with cs_sync_source_lock(pid, PR, engine) as while_raw_held:
        result["acquired_while_raw_connection_holds_it"] = while_raw_held
    raw.invalidate()  # 풀로 돌려보내지 않고 물리 연결을 끊는다(프로세스 크래시와 동일)
    raw.close()
    with cs_sync_source_lock(pid, PR, engine) as after_crash:
        result["acquired_after_holder_connection_closed"] = after_crash
    return {"scenario": "lock_contention", **result}


def _sync_worker(
    pid: int, barrier: threading.Barrier, gate: threading.Event, lock_factory: Any, out: dict, key: str
) -> None:
    session = SessionLocal()
    try:
        connector = _GatedConnector(gate)
        service = CsInquiryCatchupService(session, lock_factory=lock_factory)
        barrier.wait(timeout=15)
        res = service.sync_platform(connector, pid)
        out[key] = {
            "status": res["status"],
            "by_source": {s: v["status"] for s, v in res["by_source"].items()},
            "calls": list(connector.calls),
            "exception": None,
        }
    except Exception as exc:  # noqa: BLE001 - 결과 JSON으로 보고한다
        out[key] = {"exception": type(exc).__name__}
    finally:
        session.close()


def scenario_concurrent_sync_platform() -> dict:
    pid = _make_platform()
    barrier = threading.Barrier(2)
    gate = threading.Event()
    guard = threading.Lock()
    attempts = {"n": 0}

    @contextmanager
    def counting_lock(platform_id: int, source: str) -> Iterator[bool]:
        with cs_sync_source_lock(platform_id, source, engine) as acquired:
            with guard:
                attempts["n"] += 1
                if attempts["n"] >= 4:  # 두 worker가 두 source의 잠금 시도를 모두 끝냈다 -> fetch 진행 허용
                    gate.set()
            yield acquired

    out: dict[str, Any] = {}
    threads = [
        threading.Thread(target=_sync_worker, args=(pid, barrier, gate, counting_lock, out, "a")),
        threading.Thread(target=_sync_worker, args=(pid, barrier, gate, counting_lock, out, "b")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=90)

    check = SessionLocal()
    case_counts: dict[str, int] = {
        str(source): int(count)
        for source, count in check.execute(
            select(CsCase.external_source, func.count())
            .where(CsCase.platform_id == pid)
            .group_by(CsCase.external_source)
        ).all()
    }
    checkpoint_rows = list(
        check.execute(
            select(IntegrationStatus.integration_code, IntegrationStatus.last_success_at).where(
                IntegrationStatus.integration_type == "CS_CHECKPOINT",
                IntegrationStatus.integration_code.like(f"{pid}:%"),
            )
        ).all()
    )
    check.close()
    return {
        "scenario": "concurrent_sync_platform",
        "a": out.get("a"),
        "b": out.get("b"),
        "attempts": attempts["n"],
        "case_counts": case_counts,
        "checkpoint_codes": sorted(code for code, _ in checkpoint_rows),
        "checkpoints_all_have_progress": all(ts is not None for _, ts in checkpoint_rows),
    }


def scenario_stale_probe_sees_real_lock() -> dict:
    pid = _make_platform()
    setup = SessionLocal()
    row = TaskExecutionHistory(
        task_type="FULL_SYNC",
        target="cs_inquiry_sync",
        trigger_type="SCHEDULE",
        status="RUNNING",
        started_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=21),
    )
    setup.add(row)
    setup.commit()
    row_id = row.id
    setup.close()

    targets = ["cs_inquiry_sync", "cs_inquiry_catchup", "backup"]

    s1 = SessionLocal()
    with cs_sync_source_lock(pid, CC, engine) as held:
        during = recover_stale_running_tasks(s1, targets)
        s1.commit()
    status_during = s1.execute(
        select(TaskExecutionHistory.status).where(TaskExecutionHistory.id == row_id)
    ).scalar_one()
    s1.close()

    s2 = SessionLocal()
    after = recover_stale_running_tasks(s2, targets)
    s2.commit()
    final = s2.execute(
        select(TaskExecutionHistory.status, TaskExecutionHistory.error_message).where(TaskExecutionHistory.id == row_id)
    ).one()
    s2.close()
    return {
        "scenario": "stale_probe_sees_real_lock",
        "lock_acquired": held,
        "during_lock": during,
        "status_during_lock": status_during,
        "after_release": after,
        "final_status": final[0],
        "final_error_message": final[1],
    }


def scenario_manual_vs_running_sync() -> dict:
    pid = _make_platform()
    started = threading.Event()
    release = threading.Event()
    out: dict[str, Any] = {}

    def _auto() -> None:
        session = SessionLocal()
        try:
            connector = _GatedConnector(release, on_fetch=started)
            out["auto"] = CsInquiryCatchupService(session).sync_platform(connector, pid)["status"]
        finally:
            session.close()

    t = threading.Thread(target=_auto)
    t.start()
    started.wait(timeout=30)

    # API 수동 sync와 같은 방식: 두 source 잠금을 모두 잡아야만 진행한다.
    with ExitStack() as stack:
        acquired = [stack.enter_context(cs_sync_source_lock(pid, s, engine)) for s in (CC, PR)]
    out["manual_all_acquired"] = all(acquired)
    out["manual_acquired_per_source"] = acquired
    release.set()
    t.join(timeout=60)

    with ExitStack() as stack:
        after = [stack.enter_context(cs_sync_source_lock(pid, s, engine)) for s in (CC, PR)]
    out["manual_all_acquired_after_auto_finished"] = all(after)
    return {"scenario": "manual_vs_running_sync", **out}


def _durable_counts(pid: int) -> dict[str, int]:
    """새 커넥션에서 보이는(= 커밋된) 값만 센다."""
    check = SessionLocal()
    try:
        return {
            "cases": int(
                check.execute(select(func.count()).select_from(CsCase).where(CsCase.platform_id == pid)).scalar_one()
            ),
            "history": int(
                check.execute(
                    select(func.count())
                    .select_from(CsCaseHistory)
                    .join(CsCase, CsCase.id == CsCaseHistory.case_id)
                    .where(CsCase.platform_id == pid)
                ).scalar_one()
            ),
            "success_checkpoints": int(
                check.execute(
                    select(func.count())
                    .select_from(IntegrationStatus)
                    .where(
                        IntegrationStatus.integration_type == "CS_CHECKPOINT",
                        IntegrationStatus.integration_code.like(f"{pid}:%"),
                        IntegrationStatus.last_success_at.is_not(None),
                    )
                ).scalar_one()
            ),
        }
    finally:
        check.close()


def scenario_atomic_segment_commit_failure() -> dict:
    """실제 PostgreSQL에서 구간 commit이 실패하면 case/history/checkpoint가 모두 반영되지 않고, 이어서 성공한
    다음 실행에서 source마다 정확히 1건으로 수렴한다(commit 직전 실패 = 부분 반영 없음)."""
    pid = _make_platform()
    session = SessionLocal()
    state = {"fail_once": True}

    @event.listens_for(session, "before_commit")
    def _fail_first_commit(_session: Session) -> None:
        # SAVEPOINT release(중첩 트랜잭션)에서도 before_commit이 호출되므로, 최상위 트랜잭션 commit에만 주입한다.
        if state["fail_once"] and not _session.in_nested_transaction():
            state["fail_once"] = False
            raise RuntimeError("synthetic commit failure")

    service = CsInquiryCatchupService(session)
    first = service.sync_platform(_GatedConnector(None), pid)
    after_first = _durable_counts(pid)

    second = service.sync_platform(_GatedConnector(None), pid)
    after_second = _durable_counts(pid)
    session.close()
    return {
        "scenario": "atomic_segment_commit_failure",
        "first_by_source": {s: v["status"] for s, v in first["by_source"].items()},
        "first_reason_codes": {s: v.get("reason_code") for s, v in first["by_source"].items()},
        "after_first": after_first,
        "second_status": second["status"],
        "after_second": after_second,
    }


SCENARIOS = {
    "lock_contention": scenario_lock_contention,
    "concurrent_sync_platform": scenario_concurrent_sync_platform,
    "stale_probe_sees_real_lock": scenario_stale_probe_sees_real_lock,
    "manual_vs_running_sync": scenario_manual_vs_running_sync,
    "atomic_segment_commit_failure": scenario_atomic_segment_commit_failure,
}


def main() -> None:
    fn = SCENARIOS.get(scenario)
    if fn is None:
        raise ValueError(f"알 수 없는 시나리오: {scenario}")
    print(json.dumps(fn()))


if __name__ == "__main__":
    main()
