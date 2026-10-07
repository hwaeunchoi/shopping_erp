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
    from services.cs_sync_lock import lock_key

    raw = engine.connect()
    classid, objid = lock_key(pid, PR)
    got = raw.execute(text("SELECT pg_try_advisory_lock(:c, :o)"), {"c": classid, "o": objid}).scalar()
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


# ---------------------------------------------------------------------------
# 시나리오 5) atomicity_matrix - 실제 PostgreSQL에서 실패 유형별 case/history/checkpoint의 "커밋된" 행 수
# ---------------------------------------------------------------------------

FIXED_NOW = datetime(2026, 10, 7, 1, 0, 0)  # KST 2026-10-07 10:00 (naive UTC) - 시계 고정


def _kst_midnight_utc(year: int, month: int, day: int) -> datetime:
    kst = timezone(timedelta(hours=9))
    return datetime(year, month, day, tzinfo=kst).astimezone(timezone.utc).replace(tzinfo=None)


def _item(inquiry_id: str) -> dict[str, Any]:
    return {
        "platform_inquiry_id": inquiry_id,
        "content": "합성 문의",
        "inquiry_at": FIXED_NOW,
        "raw_status": "progress:requestAnswer",
        "needs_answer": True,
        "platform_order_no": None,
        "customer_phone": None,
    }


class _ScriptedConnector:
    """호출 순번별로 결과(항목 리스트 또는 예외)를 정하고, 호출마다 예산을 cost만큼 소비한다."""

    supports_inquiry_sync = True
    supports_product_inquiry_sync = True

    def __init__(
        self, cc: Optional[list[Any]] = None, pr: Optional[list[Any]] = None, cost_cc: int = 4, cost_pr: int = 1
    ):
        self.plan = {CC: cc if cc is not None else [[_item("A")]], PR: pr if pr is not None else [[_item("P")]]}
        self.cost = {CC: cost_cc, PR: cost_pr}
        self.calls = {CC: 0, PR: 0}

    def _run(self, source: str, request_budget: Any) -> list[dict[str, Any]]:
        plan = self.plan[source]
        outcome = plan[min(self.calls[source], len(plan) - 1)]
        self.calls[source] += 1
        for _ in range(self.cost[source]):
            request_budget.consume("coupang")
        if isinstance(outcome, Exception):
            raise outcome
        return list(outcome)

    def fetch_inquiries(self, start_date, end_date, *, max_pages=None, max_retries=None, request_budget=None):
        return self._run(CC, request_budget)

    def fetch_product_inquiries(self, start_date, end_date, *, max_pages=None, max_retries=None, request_budget=None):
        return self._run(PR, request_budget)


def _snapshot(pid: int) -> dict[str, dict[str, Any]]:
    """새 커넥션에서 보이는(= 커밋된) source별 case/history/checkpoint."""
    check = SessionLocal()
    try:
        out: dict[str, dict[str, Any]] = {}
        for source, short in ((CC, "CC"), (PR, "PI")):
            cases = check.execute(
                select(func.count())
                .select_from(CsCase)
                .where(CsCase.platform_id == pid, CsCase.external_source == source)
            ).scalar_one()
            history = check.execute(
                select(func.count())
                .select_from(CsCaseHistory)
                .join(CsCase, CsCase.id == CsCaseHistory.case_id)
                .where(CsCase.platform_id == pid, CsCase.external_source == source)
            ).scalar_one()
            checkpoint = check.execute(
                select(IntegrationStatus.last_success_at).where(
                    IntegrationStatus.integration_type == "CS_CHECKPOINT",
                    IntegrationStatus.integration_code == f"{pid}:{short}",
                )
            ).scalar_one_or_none()
            out[short] = {
                "cases": int(cases),
                "history": int(history),
                "checkpoint": checkpoint.isoformat() if checkpoint is not None else None,
            }
        return out
    finally:
        check.close()


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        short: {"status": result["by_source"][src]["status"], "reason": result["by_source"][src].get("reason_code")}
        for src, short in ((CC, "CC"), (PR, "PI"))
    }


def _service(session: Any) -> CsInquiryCatchupService:
    return CsInquiryCatchupService(session, now_fn=lambda: FIXED_NOW)


def scenario_atomicity_matrix() -> dict:
    from sqlalchemy.orm import sessionmaker

    from integrations.malls.errors import MarketplaceExternalAPIError
    from services.cs_inquiry_catchup_service import CsSyncCheckpointRepository

    matrix: dict[str, Any] = {}

    # A) case 저장(flush) 후 checkpoint 갱신 단계에서 실패 -> 둘 다 반영 안 됨, 같은 구간 재실행하면 수렴
    pid = _make_platform()
    session = SessionLocal()
    service = _service(session)

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("synthetic checkpoint failure")

    service.checkpoints.advance = _boom  # type: ignore[method-assign]
    first = service.sync_platform(_ScriptedConnector(), pid)
    row: dict[str, Any] = {"first": _summary(first), "after_first": _snapshot(pid)}
    del service.checkpoints.advance  # 인스턴스 덮어쓰기 제거 -> 원래 메서드
    row["rerun"] = _summary(service.sync_platform(_ScriptedConnector(), pid))
    row["after_rerun"] = _snapshot(pid)
    session.close()
    matrix["A_fail_before_checkpoint"] = row

    # B) checkpoint flush 후 최상위 commit 실패(첫 source) -> 첫 source만 반영 안 됨, 두 번째 source는 성공
    pid = _make_platform()
    session = SessionLocal()
    state = {"fail_once": True}

    @event.listens_for(session, "before_commit")
    def _fail_first_top_level_commit(_s: Session) -> None:
        if state["fail_once"] and not _s.in_nested_transaction():
            state["fail_once"] = False
            raise RuntimeError("synthetic commit failure")

    service = _service(session)
    row = {"first": _summary(service.sync_platform(_ScriptedConnector(), pid)), "after_first": _snapshot(pid)}
    row["rerun"] = _summary(service.sync_platform(_ScriptedConnector(), pid))
    row["after_rerun"] = _snapshot(pid)
    session.close()
    matrix["B_flush_then_commit_failure"] = row

    # C) chunk 1 성공 후 chunk 2 실패 -> chunk 1만 보존, checkpoint는 chunk 1 끝
    pid = _make_platform()
    session = SessionLocal()
    CsSyncCheckpointRepository(session).advance(pid, CC, _kst_midnight_utc(2026, 9, 24))
    session.commit()
    service = _service(session)
    server_error = MarketplaceExternalAPIError("coupang", "SERVER_ERROR", True, http_status=500)
    connector = _ScriptedConnector(cc=[[_item("A")], server_error], pr=[[]])
    row = {"first": _summary(service.sync_platform(connector, pid)), "after_first": _snapshot(pid)}
    connector2 = _ScriptedConnector(cc=[[_item("B")]], pr=[[]])
    row["rerun"] = _summary(service.sync_platform(connector2, pid))
    row["after_rerun"] = _snapshot(pid)
    session.close()
    matrix["C_chunk2_fails_after_chunk1"] = row

    # D) failed > 0: 항목 하나가 실제 DB 오류(컬럼 길이 초과) -> SAVEPOINT 롤백, 나머지 보존, checkpoint 불변
    pid = _make_platform()
    session = SessionLocal()
    service = _service(session)
    connector = _ScriptedConnector(cc=[[_item("good"), _item("x" * 150)]])
    row = {"first": _summary(service.sync_platform(connector, pid)), "after_first": _snapshot(pid)}
    row["rerun"] = _summary(service.sync_platform(_ScriptedConnector(cc=[[_item("good")]]), pid))
    row["after_rerun"] = _snapshot(pid)
    session.close()
    matrix["D_item_failure_failed_gt_0"] = row

    # E) PAGE_LIMIT_EXCEEDED -> 그 source는 쓰기·checkpoint 없음
    pid = _make_platform()
    session = SessionLocal()
    service = _service(session)
    page_limit = MarketplaceExternalAPIError("coupang", "PAGE_LIMIT_EXCEEDED", False)
    row = {
        "first": _summary(service.sync_platform(_ScriptedConnector(cc=[page_limit]), pid)),
        "after_first": _snapshot(pid),
    }
    row["rerun"] = _summary(service.sync_platform(_ScriptedConnector(), pid))
    row["after_rerun"] = _snapshot(pid)
    session.close()
    matrix["E_page_limit_exceeded"] = row

    # F) REQUEST_BUDGET_EXCEEDED(콜센터가 예산보다 많이 쓰려 함) -> 쓰기·checkpoint 없음, 상품별은 미뤄짐
    pid = _make_platform()
    session = SessionLocal()
    service = _service(session)
    row = {"first": _summary(service.sync_platform(_ScriptedConnector(cost_cc=50), pid)), "after_first": _snapshot(pid)}
    row["rerun"] = _summary(service.sync_platform(_ScriptedConnector(), pid))
    row["after_rerun"] = _snapshot(pid)
    session.close()
    matrix["F_request_budget_exceeded"] = row

    # G) 최상위 commit 직전 BaseException(SystemExit) -> 아무것도 반영 안 됨, 잠금 해제, 새 세션 재실행으로 수렴
    pid = _make_platform()
    session = SessionLocal()
    state_g = {"die": True}

    @event.listens_for(session, "before_commit")
    def _die_before_top_level_commit(_s: Session) -> None:
        if state_g["die"] and not _s.in_nested_transaction():
            state_g["die"] = False
            raise SystemExit("simulated process death")

    raised = False
    try:
        _service(session).sync_platform(_ScriptedConnector(), pid)
    except SystemExit:
        raised = True
    session.close()
    row = {"raised_system_exit": raised, "after_first": _snapshot(pid)}
    with ExitStack() as stack:
        row["locks_free_after_death"] = all(stack.enter_context(cs_sync_source_lock(pid, s, engine)) for s in (CC, PR))
    rerun_session = sessionmaker(bind=engine, autoflush=False)()
    row["rerun"] = _summary(_service(rerun_session).sync_platform(_ScriptedConnector(), pid))
    rerun_session.close()
    row["after_rerun"] = _snapshot(pid)
    matrix["G_base_exception_before_commit"] = row

    return {"scenario": "atomicity_matrix", "fixed_now": FIXED_NOW.isoformat(), "matrix": matrix}


def scenario_stale_concurrent_recovery() -> dict:
    """두 scheduler 인스턴스가 같은 stale 행을 동시에 정리해도 오류 없이 같은 최종 값이 된다."""
    setup = SessionLocal()
    row = TaskExecutionHistory(
        task_type="PRODUCT_SYNC",
        target="product_sync",
        trigger_type="SCHEDULE",
        status="RUNNING",
        started_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=21),
    )
    setup.add(row)
    setup.commit()
    row_id = row.id
    setup.close()

    barrier = threading.Barrier(2)
    out: dict[str, Any] = {}

    def _worker(key: str) -> None:
        session = SessionLocal()
        try:
            barrier.wait(timeout=15)
            out[key] = recover_stale_running_tasks(session, ["product_sync"])
            session.commit()
        except Exception as exc:  # noqa: BLE001
            out[key] = {"exception": type(exc).__name__}
        finally:
            session.close()

    threads = [threading.Thread(target=_worker, args=(k,)) for k in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    check = SessionLocal()
    final = check.execute(
        select(TaskExecutionHistory.status, TaskExecutionHistory.error_message, TaskExecutionHistory.finished_at).where(
            TaskExecutionHistory.id == row_id
        )
    ).one()
    check.close()
    return {
        "scenario": "stale_concurrent_recovery",
        "a": out.get("a"),
        "b": out.get("b"),
        "final_status": final[0],
        "final_error_message": final[1],
        "finished_at_set": final[2] is not None,
    }


SCENARIOS = {
    "lock_contention": scenario_lock_contention,
    "concurrent_sync_platform": scenario_concurrent_sync_platform,
    "stale_probe_sees_real_lock": scenario_stale_probe_sees_real_lock,
    "manual_vs_running_sync": scenario_manual_vs_running_sync,
    "atomic_segment_commit_failure": scenario_atomic_segment_commit_failure,
    "atomicity_matrix": scenario_atomicity_matrix,
    "stale_concurrent_recovery": scenario_stale_concurrent_recovery,
}


def main() -> None:
    fn = SCENARIOS.get(scenario)
    if fn is None:
        raise ValueError(f"알 수 없는 시나리오: {scenario}")
    print(json.dumps(fn()))


if __name__ == "__main__":
    main()
