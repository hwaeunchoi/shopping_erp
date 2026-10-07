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
import time
import uuid
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

db_host = os.environ["CSLOCK_DB_HOST"]
db_user = os.environ["CSLOCK_DB_USER"]
db_password = os.environ["CSLOCK_DB_PASSWORD"]
db_name = os.environ["CSLOCK_DB_NAME"]
scenario = os.environ["CSLOCK_SCENARIO"]

os.environ["DATABASE_URL"] = f"postgresql+psycopg://{db_user}:{db_password}@{db_host}:5432/{db_name}"

from sqlalchemy import func, select, text  # noqa: E402

from config.settings import settings  # noqa: E402
from core.database import SessionLocal, engine  # noqa: E402
from models import Base  # noqa: E402
from models.cs_case import CsCase  # noqa: E402
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


class _SlowConnector:
    """fetch마다 sleep_s초 머문다 - 다른 worker가 잠금을 시도할 시간 동안 잠금이 유지되도록."""

    supports_inquiry_sync = True
    supports_product_inquiry_sync = True

    def __init__(self, sleep_s: float) -> None:
        self.sleep_s = sleep_s
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

    def fetch_inquiries(self, start_date, end_date, *, max_pages=None, max_retries=None, request_budget=None):
        self.calls.append(CC)
        request_budget.consume("coupang")
        time.sleep(self.sleep_s)
        return [self._item("LOCK-CC-1")]

    def fetch_product_inquiries(self, start_date, end_date, *, max_pages=None, max_retries=None, request_budget=None):
        self.calls.append(PR)
        request_budget.consume("coupang")
        time.sleep(self.sleep_s)
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


def _sync_worker(pid: int, barrier: threading.Barrier, out: dict, key: str) -> None:
    session = SessionLocal()
    try:
        connector = _SlowConnector(sleep_s=1.5)
        service = CsInquiryCatchupService(session)
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
    out: dict[str, Any] = {}
    threads = [
        threading.Thread(target=_sync_worker, args=(pid, barrier, out, "a")),
        threading.Thread(target=_sync_worker, args=(pid, barrier, out, "b")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

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
    out: dict[str, Any] = {}

    class _Signalling(_SlowConnector):
        def fetch_inquiries(self, *a, **k):
            started.set()
            return super().fetch_inquiries(*a, **k)

    def _auto() -> None:
        session = SessionLocal()
        try:
            out["auto"] = CsInquiryCatchupService(session).sync_platform(_Signalling(sleep_s=2.0), pid)["status"]
        finally:
            session.close()

    t = threading.Thread(target=_auto)
    t.start()
    started.wait(timeout=15)

    # API 수동 sync와 같은 방식: 두 source 잠금을 모두 잡아야만 진행한다.
    with ExitStack() as stack:
        acquired = [stack.enter_context(cs_sync_source_lock(pid, s, engine)) for s in (CC, PR)]
    out["manual_all_acquired"] = all(acquired)
    out["manual_acquired_per_source"] = acquired
    t.join(timeout=60)

    with ExitStack() as stack:
        after = [stack.enter_context(cs_sync_source_lock(pid, s, engine)) for s in (CC, PR)]
    out["manual_all_acquired_after_auto_finished"] = all(after)
    return {"scenario": "manual_vs_running_sync", **out}


SCENARIOS = {
    "lock_contention": scenario_lock_contention,
    "concurrent_sync_platform": scenario_concurrent_sync_platform,
    "stale_probe_sees_real_lock": scenario_stale_probe_sees_real_lock,
    "manual_vs_running_sync": scenario_manual_vs_running_sync,
}


def main() -> None:
    fn = SCENARIOS.get(scenario)
    if fn is None:
        raise ValueError(f"알 수 없는 시나리오: {scenario}")
    print(json.dumps(fn()))


if __name__ == "__main__":
    main()
