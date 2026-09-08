"""
tests/integration/_cs_case_concurrency_runner.py
----------------------------------------------------
실제(격리) PostgreSQL 위에서, 서로 다른 DB 커넥션/세션을 쓰는 두 "worker"가
CS(고객문의) 케이스의 두 경합 지점을 실제로 동시에 건드렸을 때 안전한지
검증한다 - SQLite는 파일 단위 잠금이 두 커넥션의 동시 쓰기를 직렬화해버려
이 경합 자체를 재현하지 못한다.

시나리오 1) concurrent_assignment_same_case:
아직 담당자가 없는(assignee_id=NULL) 같은 케이스에 두 worker가 동시에 서로
다른 담당자를 배정하려 하면(둘 다 expected_assignee_id=None으로 요청 -
"내가 본 화면엔 미배정으로 보였다"), CsCaseRepository.claim_assignee()의
원자적 UPDATE(WHERE assignee_id IS NULL)가 정확히 하나만 성공시키고 나머지는
CsCaseConflictError로 막아야 한다.

시나리오 2) concurrent_duplicate_external_inquiry:
같은 (platform_id, external_inquiry_id) 조합의 채널 문의를 두 worker가 동시에
"처음 보는 문의"로 판단해(_upsert_one의 get_by_external() 조회 시점엔 아직
서로의 커밋을 못 봄) 각자 새 케이스를 만들려 하면, DB 유니크 제약
(uq_cs_case_external_inquiry)이 실제로 하나만 통과시키고 나머지는
IntegrityError -> SAVEPOINT 롤백으로 안전하게 흡수돼야 한다(전체 동기화가
깨지지 않고 그 항목만 failed로 카운트).

tests/integration/test_cs_case_concurrency_pg.py가 격리된 1회성 docker
postgres 컨테이너를 띄우고, 이 스크립트를 그 postgres와 같은(--internal)
네트워크에 붙은 러너 컨테이너 안에서 실행한다(레포를 읽기전용으로 마운트) -
tests/integration/_fulfillment_concurrency_runner.py와 동일한 격리 패턴.

DB 스키마는 Alembic 마이그레이션이 아니라 Base.metadata.create_all()로 만든다
(마이그레이션 자체의 정합성은 별도 테스트가 검증한다 - 이 스크립트는 런타임
동시성 동작만 검증한다). 실제 채널 API는 호출하지 않는다(스텁 커넥터만 사용).

결과를 JSON 한 줄로 표준출력 마지막 줄에 출력한다.
"""

import json
import os
import sys
import threading
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

db_host = os.environ["CSCONC_DB_HOST"]
db_user = os.environ["CSCONC_DB_USER"]
db_password = os.environ["CSCONC_DB_PASSWORD"]
db_name = os.environ["CSCONC_DB_NAME"]
scenario = os.environ["CSCONC_SCENARIO"]

os.environ["DATABASE_URL"] = f"postgresql+psycopg://{db_user}:{db_password}@{db_host}:5432/{db_name}"

from sqlalchemy import select  # noqa: E402

from core.database import SessionLocal, engine  # noqa: E402
from models import Base  # noqa: E402
from models.cs_case import CsCase  # noqa: E402
from models.platform import Platform  # noqa: E402
from models.user import Role, User  # noqa: E402
from services.cs_case_service import CsCaseConflictError, CsCaseService  # noqa: E402
from services.cs_channel_sync_service import CsChannelSyncService  # noqa: E402

Base.metadata.create_all(engine)


def _make_platform() -> int:
    setup = SessionLocal()
    platform = Platform(
        code=f"pf-{uuid.uuid4().hex[:8]}",
        name="동시성테스트플랫폼",
        connector_class="StubConnector",
        settlement_cycle_days=7,
        is_active=True,
    )
    setup.add(platform)
    setup.commit()
    platform_id = platform.id
    setup.close()
    return platform_id


def _make_user() -> int:
    setup = SessionLocal()
    role = Role(name=f"role-{uuid.uuid4().hex[:8]}")
    setup.add(role)
    setup.flush()
    user = User(
        username=f"agent-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        name="동시성테스트상담원",
        role_id=role.id,
        is_active=True,
    )
    setup.add(user)
    setup.commit()
    user_id = user.id
    setup.close()
    return user_id


def _worker_assign(case_id: int, assignee_id: int, barrier: threading.Barrier, out: dict, key: str) -> None:
    session = SessionLocal()
    try:
        service = CsCaseService(session)
        barrier.wait(timeout=10)
        try:
            case = service.assign(case_id, assignee_id, None, actor=None)
            session.commit()
            out[key] = {"outcome": "ACCEPTED", "assignee_id": case.assignee_id, "exception": None}
        except CsCaseConflictError as e:
            session.rollback()
            out[key] = {"outcome": "REJECTED", "assignee_id": None, "error": str(e), "exception": None}
    except Exception as e:  # noqa: BLE001 - 실패 자체를 결과로 보고해야 호스트에서 판정할 수 있다.
        session.rollback()
        out[key] = {"outcome": None, "assignee_id": None, "exception": f"{type(e).__name__}: {e}"}
    finally:
        session.close()


def scenario_concurrent_assignment_same_case() -> dict:
    setup = SessionLocal()
    case = CsCase(
        inquiry_type="ETC", priority="NORMAL", status="OPEN", customer_message="동시성 테스트 문의", reopened_count=0
    )
    setup.add(case)
    setup.commit()
    case_id = case.id
    setup.close()

    agent_a = _make_user()
    agent_b = _make_user()
    barrier = threading.Barrier(2)
    out: dict = {}
    t1 = threading.Thread(target=_worker_assign, args=(case_id, agent_a, barrier, out, "a"), name="worker-A")
    t2 = threading.Thread(target=_worker_assign, args=(case_id, agent_b, barrier, out, "b"), name="worker-B")
    t1.start()
    t2.start()
    t1.join(timeout=20)
    t2.join(timeout=20)

    check = SessionLocal()
    final_assignee = check.execute(select(CsCase.assignee_id).where(CsCase.id == case_id)).scalar_one()
    check.close()

    return {
        "scenario": "concurrent_assignment_same_case",
        "final_assignee_id": final_assignee,
        "agent_a": agent_a,
        "agent_b": agent_b,
        "a": out.get("a"),
        "b": out.get("b"),
    }


class _StubInquiryConnector:
    supports_inquiry_sync = True

    def __init__(self, item: dict[str, Any]) -> None:
        self.item = item

    def fetch_inquiries(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        return [self.item]


def _worker_sync(platform_id: int, item: dict[str, Any], barrier: threading.Barrier, out: dict, key: str) -> None:
    from config.settings import settings

    session = SessionLocal()
    try:
        settings.cs_inquiry_sync_enabled = True
        service = CsChannelSyncService(session)
        connector = _StubInquiryConnector(item)
        barrier.wait(timeout=10)
        result = service.sync_inquiries(connector, platform_id, date(2026, 1, 1), date(2026, 1, 7))
        session.commit()
        out[key] = {"result": result, "exception": None}
    except Exception as e:  # noqa: BLE001 - 실패 자체를 결과로 보고해야 호스트에서 판정할 수 있다.
        session.rollback()
        out[key] = {"result": None, "exception": f"{type(e).__name__}: {e}"}
    finally:
        session.close()


def scenario_concurrent_duplicate_external_inquiry() -> dict:
    platform_id = _make_platform()
    item = {
        "platform_inquiry_id": "DUP-EXT-0001",
        "content": "동시성 테스트 채널 문의",
        "inquiry_at": datetime(2026, 1, 10, 9, 0, 0),
        "raw_status": "progress:requestAnswer",
        "needs_answer": True,
        "platform_order_no": None,
        "customer_phone": None,
    }

    barrier = threading.Barrier(2)
    out: dict = {}
    t1 = threading.Thread(target=_worker_sync, args=(platform_id, item, barrier, out, "a"), name="worker-A")
    t2 = threading.Thread(target=_worker_sync, args=(platform_id, item, barrier, out, "b"), name="worker-B")
    t1.start()
    t2.start()
    t1.join(timeout=20)
    t2.join(timeout=20)

    check = SessionLocal()
    rows = list(
        check.execute(
            select(CsCase).where(CsCase.platform_id == platform_id, CsCase.external_inquiry_id == "DUP-EXT-0001")
        ).scalars()
    )
    check.close()

    return {
        "scenario": "concurrent_duplicate_external_inquiry",
        "row_count": len(rows),
        "a": out.get("a"),
        "b": out.get("b"),
    }


SCENARIOS = {
    "concurrent_assignment_same_case": scenario_concurrent_assignment_same_case,
    "concurrent_duplicate_external_inquiry": scenario_concurrent_duplicate_external_inquiry,
}


def main() -> None:
    fn = SCENARIOS.get(scenario)
    if fn is None:
        raise ValueError(f"알 수 없는 시나리오: {scenario}")
    result = fn()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
