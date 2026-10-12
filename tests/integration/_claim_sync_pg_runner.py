"""
tests/integration/_claim_sync_pg_runner.py
-----------------------------------------------
실제(격리) PostgreSQL 위에서 교환·반품(클레임) 수집의 저장 안전성을 검증한다 - SQLite로는 재현되지 않는 unique 제약 경합,
SAVEPOINT/commit 실패 롤백, 미매칭 승격, 취소 후보 cursor 순환을 다룬다. 스텁 커넥터만 쓰며 외부 API는 호출하지 않는다.

시나리오:
6) job_lock_exclusion   - claim_sync job 두 개와 수동 잠금이 겹치면 하나만 실행하고 외부 요청·cursor 처리는 1회분이다.
7) cursor_commit_failure - 취소 후보 cursor는 데이터와 같은 트랜잭션이라 commit 실패 시 전진하지 않고 재실행이 같은 주문을 다시 본다.
8) promotion_poison    - 승격에 실패하는 미매칭 한 건이 다른 승격과 수집 전체를 막지 않는다(주문+해결 표시는 함께 반영).
1) concurrent_same_claim  - 두 세션이 같은 claim을 동시에 저장해도 행은 1개, 예외는 새지 않는다.
2) commit_failure         - 최상위 commit이 실패하면 반품·교환·미매칭이 하나도 남지 않고, 재실행하면 1건으로 수렴한다.
3) unmatched_then_promoted - 주문 없는 claim은 보존되고, 주문이 생긴 뒤 재수집하면 중복 없이 승격된다.
4) partial_failure         - 반품이 PAGE_LIMIT_EXCEEDED로 실패해도 교환은 저장되고, 다음 실행이 반품을 복구한다.
5) cancellation_rotation   - 45개 후보를 20개씩 3회에 한 바퀴 돌고, 영구 오류 주문 하나가 회전을 막지 않는다(cursor 행은 1개).

tests/integration/test_claim_sync_pg.py가 격리 postgres 컨테이너와 이 스크립트를 실행한다(env 이름은 CSLOCK_*을 재사용).
결과를 JSON 한 줄로 표준출력 마지막 줄에 출력한다.
"""

import json
import os
import sys
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
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

from sqlalchemy import event, func, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from config.settings import settings  # noqa: E402
from core.database import SessionLocal, engine  # noqa: E402
from integrations.malls.errors import MarketplaceExternalAPIError, RequestBudget  # noqa: E402
from models import Base  # noqa: E402
from models.order import ClaimCollectionCursor, ClaimUnmatched, Exchange, Order, Return  # noqa: E402
from models.platform import Platform  # noqa: E402
from repositories.platform_repository import PlatformRepository  # noqa: E402
from scheduler.jobs import claim_sync_job  # noqa: E402
from services.claim_sync_lock import claim_sync_platform_lock  # noqa: E402
from services.claim_sync_service import ClaimSyncLimits, ClaimSyncService  # noqa: E402

Base.metadata.create_all(engine)

TODAY = date(2026, 10, 8)
START = TODAY - timedelta(days=3)


def _limits() -> ClaimSyncLimits:
    return ClaimSyncLimits(max_pages=3, max_retries=2, budget=RequestBudget(max_requests=105))


def _platform() -> int:
    s = SessionLocal()
    p = Platform(
        code=f"pf-{uuid.uuid4().hex[:8]}",
        name="클레임테스트",
        connector_class="X",
        settlement_cycle_days=7,
        is_active=True,
    )
    s.add(p)
    s.commit()
    pid = p.id
    s.close()
    return pid


def _order(s: Session, pid: int, no: str, status: str = "NEW") -> None:
    s.add(
        Order(
            platform_id=pid,
            platform_order_no=no,
            status=status,
            order_date=datetime.now(timezone.utc),
            total_amount=1000,
            discount_amount=0,
        )
    )


def _claim(order_no: str, claim_id: str, status: str = "REQUESTED") -> dict[str, Any]:
    return {
        "platform_order_no": order_no,
        "platform_claim_id": claim_id,
        "reason": "사유",
        "status": status,
        "raw_status": "RAW",
        "requested_at": datetime(2026, 10, 7, 9, 0, 0),
    }


class _Conn:
    supports_cancellation_sync = False
    supports_return_sync = True
    supports_exchange_sync = True
    supports_cancellation_lookup_by_order = True

    def __init__(self, returns=None, exchanges=None, return_error=None, barrier=None, errors=None):
        self.returns = returns or []
        self.exchanges = exchanges or []
        self.return_error = return_error
        self.barrier = barrier
        self.errors = errors or {}
        self.lookups: list[str] = []

    def estimate_claim_requests(self, feature, start, end, max_pages, max_retries):
        return None

    def fetch_returns(self, s, e, **kw):
        if self.barrier is not None:
            self.barrier.wait(timeout=20)  # 두 세션이 모두 "아직 없음"을 본 뒤 동시에 저장하게 만든다
        if self.return_error:
            raise self.return_error
        return list(self.returns)

    def fetch_exchanges(self, s, e, **kw):
        return list(self.exchanges)

    def fetch_cancellation_status(self, order_no, since, **kw):
        self.lookups.append(order_no)
        if order_no in self.errors:
            raise self.errors[order_no]
        return None


def _counts(pid: int) -> dict[str, int]:
    s = SessionLocal()
    try:
        order_ids = select(Order.id).where(Order.platform_id == pid)
        return {
            "returns": int(
                s.execute(select(func.count()).select_from(Return).where(Return.order_id.in_(order_ids))).scalar_one()
            ),
            "exchanges": int(
                s.execute(
                    select(func.count()).select_from(Exchange).where(Exchange.order_id.in_(order_ids))
                ).scalar_one()
            ),
            "unmatched": int(
                s.execute(
                    select(func.count()).select_from(ClaimUnmatched).where(ClaimUnmatched.platform_id == pid)
                ).scalar_one()
            ),
            "unmatched_resolved": int(
                s.execute(
                    select(func.count())
                    .select_from(ClaimUnmatched)
                    .where(ClaimUnmatched.platform_id == pid, ClaimUnmatched.resolved_at.is_not(None))
                ).scalar_one()
            ),
        }
    finally:
        s.close()


def scenario_concurrent_same_claim() -> dict:
    pid = _platform()
    setup = SessionLocal()
    _order(setup, pid, "RACE-1")
    setup.commit()
    setup.close()
    barrier = threading.Barrier(2)
    out: dict[str, Any] = {}

    def worker(key: str) -> None:
        s = SessionLocal()
        try:
            conn = _Conn(returns=[_claim("RACE-1", "CLAIM-RACE")], barrier=barrier)
            res = ClaimSyncService(s).sync_claims(conn, pid, START, TODAY, _limits())
            s.commit()
            out[key] = {"returns": res["returns"]["status"], "reason": res["returns"]["reason_code"], "exception": None}
        except Exception as exc:  # noqa: BLE001 - 결과 JSON으로 보고한다
            s.rollback()
            out[key] = {"exception": type(exc).__name__}
        finally:
            s.close()

    threads = [threading.Thread(target=worker, args=(k,)) for k in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return {"scenario": "concurrent_same_claim", "workers": out, "counts": _counts(pid)}


def scenario_commit_failure() -> dict:
    pid = _platform()
    setup = SessionLocal()
    _order(setup, pid, "COMMIT-1")
    setup.commit()
    setup.close()

    session = SessionLocal()
    state = {"fail": True}

    @event.listens_for(session, "before_commit")
    def _boom(_s: Session) -> None:
        if state["fail"] and not _s.in_nested_transaction():
            state["fail"] = False
            raise RuntimeError("synthetic commit failure")

    conn = _Conn(
        returns=[_claim("COMMIT-1", "C-1"), _claim("NO-SUCH-ORDER", "C-2")], exchanges=[_claim("COMMIT-1", "E-1")]
    )
    service = ClaimSyncService(session)
    service.sync_claims(conn, pid, START, TODAY, _limits())
    failed = False
    try:
        session.commit()
    except RuntimeError:
        failed = True
        session.rollback()
    after_failure = _counts(pid)
    service.sync_claims(conn, pid, START, TODAY, _limits())
    session.commit()
    session.close()
    return {
        "scenario": "commit_failure",
        "commit_failed": failed,
        "after_failure": after_failure,
        "after_rerun": _counts(pid),
    }


def scenario_unmatched_then_promoted() -> dict:
    pid = _platform()
    session = SessionLocal()
    conn = _Conn(returns=[_claim("LATE-ORDER", "LATE-1")])
    ClaimSyncService(session).sync_claims(conn, pid, START, TODAY, _limits())
    session.commit()
    held = _counts(pid)
    _order(session, pid, "LATE-ORDER")
    session.commit()
    res = ClaimSyncService(session).sync_claims(conn, pid, START, TODAY, _limits())
    session.commit()
    session.close()
    again = SessionLocal()
    ClaimSyncService(again).sync_claims(conn, pid, START, TODAY, _limits())
    again.commit()
    again.close()
    return {
        "scenario": "unmatched_then_promoted",
        "held": held,
        "after_promotion": _counts(pid),
        "resolved_unmatched": res["resolved_unmatched"],
        "after_third_run": _counts(pid),
    }


def scenario_partial_failure() -> dict:
    pid = _platform()
    setup = SessionLocal()
    _order(setup, pid, "PART-1")
    setup.commit()
    setup.close()
    session = SessionLocal()
    failing = _Conn(
        return_error=MarketplaceExternalAPIError("coupang", "PAGE_LIMIT_EXCEEDED", False),
        exchanges=[_claim("PART-1", "E-9")],
    )
    first = ClaimSyncService(session).sync_claims(failing, pid, START, TODAY, _limits())
    session.commit()
    after_first = _counts(pid)
    ok = _Conn(returns=[_claim("PART-1", "R-9")], exchanges=[_claim("PART-1", "E-9")])
    second = ClaimSyncService(session).sync_claims(ok, pid, START, TODAY, _limits())
    session.commit()
    session.close()
    return {
        "scenario": "partial_failure",
        "first": {k: first[k]["status"] for k in ("returns", "exchanges")} | {"overall": first["overall_status"]},
        "first_reason": first["returns"]["reason_code"],
        "after_first": after_first,
        "second": {k: second[k]["status"] for k in ("returns", "exchanges")},
        "after_second": _counts(pid),
    }


def scenario_cancellation_rotation() -> dict:
    pid = _platform()
    setup = SessionLocal()
    for i in range(1, 46):
        _order(setup, pid, f"ROT-{i:03d}")
    setup.commit()
    setup.close()
    bad = {"ROT-005": MarketplaceExternalAPIError("coupang", "PAGE_LIMIT_EXCEEDED", False)}
    seen: list[str] = []
    skipped = 0
    for _ in range(3):
        s = SessionLocal()
        conn = _Conn(errors=bad)
        res = ClaimSyncService(s).sync_cancellations_by_candidate_orders(conn, pid, 20, _limits())
        s.commit()
        s.close()
        seen.extend(conn.lookups)
        skipped += res.get("skipped_failed", 0)
    check = SessionLocal()
    cursor_rows = int(
        check.execute(
            select(func.count()).select_from(ClaimCollectionCursor).where(ClaimCollectionCursor.platform_id == pid)
        ).scalar_one()
    )
    check.close()
    return {
        "scenario": "cancellation_rotation",
        "distinct_checked": len(set(seen)),
        "total_lookups": len(seen),
        "skipped_failed": skipped,
        "cursor_rows": cursor_rows,
    }


def scenario_job_lock_exclusion() -> dict:
    pid = _platform()
    setup = SessionLocal()
    for i in range(1, 6):
        _order(setup, pid, f"LK-{i:03d}")
    setup.commit()
    setup.close()

    settings.claim_sync_enabled = True
    settings.claim_cancel_detail_max_orders_per_run = 20
    entered = threading.Event()
    release = threading.Event()
    calls = {"returns": 0, "exchanges": 0}
    lookups: list[str] = []

    class _JobConn(_Conn):
        def fetch_returns(self, s, e, **kw):
            calls["returns"] += 1
            kw["request_budget"].consume("coupang")
            entered.set()
            release.wait(timeout=30)
            return []

        def fetch_exchanges(self, s, e, **kw):
            calls["exchanges"] += 1
            kw["request_budget"].consume("coupang")
            return []

        def fetch_cancellation_status(self, order_no, since, **kw):
            lookups.append(order_no)
            kw["request_budget"].consume("coupang")
            return None

    conn = _JobConn()
    claim_sync_job.get_mall_connector = lambda *a, **k: conn  # type: ignore[assignment]
    PlatformRepository.list_active = lambda self: list(  # type: ignore[method-assign]
        self.session.execute(select(Platform).where(Platform.id == pid)).scalars()
    )
    out: dict[str, Any] = {}

    def run_job(key: str) -> None:
        try:
            out[key] = claim_sync_job.run()
        except Exception as exc:  # noqa: BLE001 - 결과 JSON으로 보고한다
            out[key] = {"exception": type(exc).__name__}

    first = threading.Thread(target=run_job, args=("first",))
    first.start()
    assert entered.wait(timeout=30)
    run_job("second")  # 첫 job이 잠금을 쥔 채 fetch 중일 때 두 번째 job
    with claim_sync_platform_lock(pid, engine) as manual_while_running:
        manual_acquired_while_job_runs = manual_while_running  # 수동 sync-claims와 같은 잠금
    release.set()
    first.join(timeout=60)
    with claim_sync_platform_lock(pid, engine) as after:
        manual_acquired_after = after
    check = SessionLocal()
    cursor = check.execute(
        select(ClaimCollectionCursor.last_order_id).where(ClaimCollectionCursor.platform_id == pid)
    ).scalar_one_or_none()
    max_order = check.execute(select(func.max(Order.id)).where(Order.platform_id == pid)).scalar_one()
    advisory = int(check.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")).scalar_one())
    check.close()
    code_keys = {k: (list(v.values())[0] if v else None) for k, v in out.items()}
    return {
        "scenario": "job_lock_exclusion",
        "second_status": [v.get("status") for v in out["second"].values() if isinstance(v, dict)],
        "first_has_requests_used": [v.get("requests_used") for v in out["first"].values() if isinstance(v, dict)],
        "return_fetches": calls["returns"],
        "exchange_fetches": calls["exchanges"],
        "lookups": len(lookups),
        "distinct_lookups": len(set(lookups)),
        "manual_acquired_while_job_runs": manual_acquired_while_job_runs,
        "manual_acquired_after": manual_acquired_after,
        "cursor_is_last_order": cursor == max_order,
        "advisory_locks_left": advisory,
        "raw_keys": sorted(code_keys),
    }


def scenario_cursor_commit_failure() -> dict:
    pid = _platform()
    setup = SessionLocal()
    for i in range(1, 7):
        _order(setup, pid, f"CUR-{i:03d}")
    setup.commit()
    setup.close()
    session = SessionLocal()
    state = {"fail": True}

    @event.listens_for(session, "before_commit")
    def _boom(_s: Session) -> None:
        if state["fail"] and not _s.in_nested_transaction():
            state["fail"] = False
            raise RuntimeError("synthetic commit failure")

    first = _Conn()
    ClaimSyncService(session).sync_cancellations_by_candidate_orders(first, pid, 3, _limits())
    failed = False
    try:
        session.commit()
    except RuntimeError:
        failed = True
        session.rollback()
    check = SessionLocal()
    after_failure = check.execute(
        select(ClaimCollectionCursor.last_order_id).where(ClaimCollectionCursor.platform_id == pid)
    ).scalar_one_or_none()
    check.close()
    second = _Conn()
    ClaimSyncService(session).sync_cancellations_by_candidate_orders(second, pid, 3, _limits())
    session.commit()
    session.close()
    check = SessionLocal()
    after_rerun = check.execute(
        select(ClaimCollectionCursor.last_order_id).where(ClaimCollectionCursor.platform_id == pid)
    ).scalar_one_or_none()
    third_id = check.execute(
        select(Order.id).where(Order.platform_id == pid).order_by(Order.id).offset(2).limit(1)
    ).scalar_one()
    check.close()
    return {
        "scenario": "cursor_commit_failure",
        "commit_failed": failed,
        "cursor_after_failure": after_failure,
        "same_orders_rechecked": first.lookups == second.lookups,
        "lookups": second.lookups,
        "cursor_after_rerun_is_third_order": after_rerun == third_id,
    }


def scenario_promotion_poison() -> dict:
    from sqlalchemy.exc import IntegrityError

    pid = _platform()
    session = SessionLocal()
    unmatched_conn = _Conn(returns=[_claim("POISON-ORDER", "P-1"), _claim("GOOD-ORDER", "G-1")])
    ClaimSyncService(session).sync_claims(unmatched_conn, pid, START, TODAY, _limits())
    session.commit()
    _order(session, pid, "POISON-ORDER")
    _order(session, pid, "GOOD-ORDER")
    session.commit()

    original = ClaimSyncService._promote_unmatched

    def poisoned(self, pending, order_id):
        if pending.platform_claim_id == "P-1":
            self.return_repo.add(
                Return(order_id=order_id, status="REVIEW", requested_at=datetime.now(), platform_claim_id="P-1")
            )
            raise IntegrityError("INSERT", {}, Exception("synthetic"))
        return original(self, pending, order_id)

    ClaimSyncService._promote_unmatched = poisoned  # type: ignore[method-assign]
    try:
        quiet = _Conn()  # 이번 실행은 새 claim이 없다 - 승격만 일어난다
        res = ClaimSyncService(session).sync_claims(quiet, pid, START, TODAY, _limits())
        session.commit()
        after_poison = _counts(pid)
    finally:
        ClaimSyncService._promote_unmatched = original  # type: ignore[method-assign]
    res2 = ClaimSyncService(session).sync_claims(_Conn(), pid, START, TODAY, _limits())
    session.commit()
    session.close()
    return {
        "scenario": "promotion_poison",
        "run_status": res["overall_status"],
        "resolved_in_poison_run": res["resolved_unmatched"],
        "after_poison_run": after_poison,
        "resolved_after_recovery": res2["resolved_unmatched"],
        "after_recovery": _counts(pid),
    }


def scenario_claim_lock_domain() -> dict:
    from core.advisory_locks import CLAIM_SYNC_CLASSID, claim_sync_lock_key

    pid_a, pid_b = _platform(), _platform()
    out: dict[str, Any] = {"classid_is_claim": claim_sync_lock_key(pid_a)[0] == CLAIM_SYNC_CLASSID}

    def can_take(pid: int) -> bool:
        with claim_sync_platform_lock(pid, engine) as got:
            return bool(got)

    # 같은 플랫폼은 직렬화, 다른 플랫폼은 동시 진행.
    with claim_sync_platform_lock(pid_a, engine) as holder:
        out["holder_acquired"] = holder
        out["same_platform_blocked"] = not can_take(pid_a)
        out["other_platform_not_blocked"] = can_take(pid_b)
        with claim_sync_platform_lock(pid_b, engine) as both:
            out["two_platforms_held_together"] = both
    out["free_after_with"] = can_take(pid_a) and can_take(pid_b)

    # 해제: Exception / BaseException / 연결 종료.
    try:
        with claim_sync_platform_lock(pid_a, engine) as g:
            assert g
            raise RuntimeError("synthetic")
    except RuntimeError:
        pass
    out["after_exception"] = can_take(pid_a)

    try:
        with claim_sync_platform_lock(pid_a, engine) as g:
            assert g
            raise KeyboardInterrupt()
    except BaseException as exc:  # noqa: BLE001 - BaseException 경로 확인
        out["base_exception_type"] = type(exc).__name__ == "KeyboardInterrupt"
    out["after_base_exception"] = can_take(pid_a)

    killer = engine.connect()
    kill_ok: bool | str = False
    try:
        with claim_sync_platform_lock(pid_a, engine) as g:
            assert g
            key = claim_sync_lock_key(pid_a)
            backend = killer.execute(
                text(
                    "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND classid = :c AND objid = :o AND granted"
                ),
                {"c": key[0], "o": key[1]},
            ).scalar_one()
            killer.execute(text("SELECT pg_terminate_backend(:p)"), {"p": backend})
            killer.commit()
        kill_ok = True  # 끊긴 연결에서도 컨텍스트 종료가 예외를 내지 않는다
    except Exception as exc:  # noqa: BLE001
        kill_ok = type(exc).__name__
    killer.close()
    out["kill_exit_clean"] = kill_ok
    out["after_connection_killed"] = can_take(pid_a)
    check = SessionLocal()
    out["advisory_locks_left"] = int(
        check.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")).scalar_one()
    )
    check.close()
    return {"scenario": "claim_lock_domain", **out}


SCENARIOS = {
    "concurrent_same_claim": scenario_concurrent_same_claim,
    "commit_failure": scenario_commit_failure,
    "unmatched_then_promoted": scenario_unmatched_then_promoted,
    "partial_failure": scenario_partial_failure,
    "cancellation_rotation": scenario_cancellation_rotation,
    "job_lock_exclusion": scenario_job_lock_exclusion,
    "cursor_commit_failure": scenario_cursor_commit_failure,
    "promotion_poison": scenario_promotion_poison,
    "claim_lock_domain": scenario_claim_lock_domain,
}

if __name__ == "__main__":
    fn = SCENARIOS.get(scenario)
    if fn is None:
        raise ValueError(f"알 수 없는 시나리오: {scenario}")
    print(json.dumps(fn()))
