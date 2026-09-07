"""
tests/integration/_recent_view_concurrency_runner.py
------------------------------------------------------------------
실제(격리) PostgreSQL 위에서, 서로 다른 DB 커넥션/세션을 쓰는 두 "worker" 스레드가
RecentViewRepository.touch()를 barrier로 동시에(같은 (user_id, target_type,
target_id)) 호출했을 때 원자적 UPSERT(INSERT ... ON CONFLICT DO UPDATE)가 실제로
경합을 흡수하는지 검증한다 - "조회 후 없으면 INSERT" 방식은 두 커넥션이 조회
시점엔 서로의 아직 커밋되지 않은 변경을 보지 못해(TOCTOU) 하나가 유니크 제약
위반으로 실패할 수 있다(실제 발견된 결함).

tests/integration/test_recent_view_concurrency_pg.py가 격리된 1회성 docker
postgres 컨테이너를 띄우고, 이 스크립트를 그 postgres와 같은(--internal) 네트워크에
붙은 러너 컨테이너 안에서 실행한다(레포를 읽기전용으로 마운트) -
tests/integration/_product_sync_concurrency_runner.py와 동일한 격리 패턴.

DB 스키마는 Alembic 마이그레이션이 아니라 Base.metadata.create_all()로 만든다 -
이 스크립트는 마이그레이션 경로가 아니라 런타임 동시성 동작만 검증하므로, 매번
새로 뜨는 1회성 스크래치 DB에 현재 모델(uq_recent_view 유니크 제약 포함) 그대로
스키마를 만드는 편이 더 정확하고 빠르다(마이그레이션 자체의 정합성은
tests/integration/test_recent_view_migration_dedup.py가 별도로 검증한다).

시나리오(RVCONC_SCENARIO 환경변수로 선택):
  - concurrent_first_insert: 같은 (user, target)에 대해 아직 행이 없는 상태에서
    두 요청이 동시에 처음 기록을 시도한다 - 정확히 한 행만 남고 둘 다 예외 없이
    끝나야 한다.
  - concurrent_update_existing: 이미 행이 있는 상태에서 두 요청이 동시에 다시
    열람을 기록한다 - 행 개수가 늘지 않고 둘 다 예외 없이 끝나야 한다.

각 시나리오는 worker마다 touch() 호출 전에 "호출자의 관계없는 변경"(다른
target_id의 RecentView 한 행)을 같은 트랜잭션에 먼저 만들어 두고, touch() 성공
후 커밋한 뒤 그 관계없는 행도 함께 남아있는지 확인한다(충돌 처리가 호출자의
무관한 변경까지 되돌리지 않는지 검증).

결과를 JSON 한 줄로 표준출력 마지막 줄에 출력한다.
"""

import json
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

db_host = os.environ["RVCONC_DB_HOST"]
db_user = os.environ["RVCONC_DB_USER"]
db_password = os.environ["RVCONC_DB_PASSWORD"]
db_name = os.environ["RVCONC_DB_NAME"]
scenario = os.environ["RVCONC_SCENARIO"]

os.environ["DATABASE_URL"] = f"postgresql+psycopg://{db_user}:{db_password}@{db_host}:5432/{db_name}"

from sqlalchemy import select  # noqa: E402

from core.database import SessionLocal, engine  # noqa: E402
from models import Base  # noqa: E402
from models.extra import RecentView  # noqa: E402
from models.user import Role, User  # noqa: E402
from repositories.extra_repository import RecentViewRepository  # noqa: E402

Base.metadata.create_all(engine)


def _make_user() -> int:
    setup = SessionLocal()
    role = Role(name=f"role-{uuid.uuid4().hex[:8]}")
    setup.add(role)
    setup.flush()
    user = User(
        username=f"user-{uuid.uuid4().hex[:8]}", password_hash="x", name="동시성테스트", role_id=role.id, is_active=True
    )
    setup.add(user)
    setup.flush()
    setup.commit()
    user_id = user.id
    setup.close()
    return user_id


def _worker(
    user_id: int,
    target_type: str,
    target_id: int,
    unrelated_target_id: int,
    barrier: threading.Barrier,
    out: dict,
    key: str,
) -> None:
    session = SessionLocal()
    try:
        # 호출자의 관계없는 변경 - touch()와 같은 트랜잭션 안에서 커밋 전까지
        # 함께 대기한다(아직 flush/commit 전).
        session.add(
            RecentView(
                user_id=user_id,
                target_type="UNRELATED",
                target_id=unrelated_target_id,
                viewed_at=datetime.now(timezone.utc),
            )
        )
        session.flush()

        barrier.wait(timeout=10)
        repo = RecentViewRepository(session)
        view = repo.touch(user_id, target_type, target_id)
        session.commit()
        out[key] = {
            "id": view.id,
            "viewed_at": view.viewed_at.isoformat(),
            "unrelated_committed": session.execute(
                select(RecentView).where(
                    RecentView.user_id == user_id,
                    RecentView.target_type == "UNRELATED",
                    RecentView.target_id == unrelated_target_id,
                )
            ).scalar_one_or_none()
            is not None,
            "error": None,
        }
    except Exception as e:  # noqa: BLE001 - 실패 자체를 결과로 보고해야 호스트에서 판정할 수 있다.
        session.rollback()
        out[key] = {"id": None, "viewed_at": None, "unrelated_committed": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        session.close()


def scenario_concurrent_first_insert() -> dict:
    user_id = _make_user()
    target_type, target_id = "ORDER", 777
    barrier = threading.Barrier(2)
    out: dict = {}
    t1 = threading.Thread(
        target=_worker, args=(user_id, target_type, target_id, 9001, barrier, out, "a"), name="worker-A"
    )
    t2 = threading.Thread(
        target=_worker, args=(user_id, target_type, target_id, 9002, barrier, out, "b"), name="worker-B"
    )
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    check = SessionLocal()
    rows = list(
        check.execute(
            select(RecentView).where(
                RecentView.user_id == user_id, RecentView.target_type == target_type, RecentView.target_id == target_id
            )
        ).scalars()
    )
    check.close()

    return {"scenario": "concurrent_first_insert", "a": out.get("a"), "b": out.get("b"), "row_count": len(rows)}


def scenario_concurrent_update_existing() -> dict:
    user_id = _make_user()
    target_type, target_id = "PRODUCT", 555
    seed = SessionLocal()
    RecentViewRepository(seed).touch(user_id, target_type, target_id)
    seed.commit()
    seed.close()

    barrier = threading.Barrier(2)
    out: dict = {}
    t1 = threading.Thread(
        target=_worker, args=(user_id, target_type, target_id, 9101, barrier, out, "a"), name="worker-A"
    )
    t2 = threading.Thread(
        target=_worker, args=(user_id, target_type, target_id, 9102, barrier, out, "b"), name="worker-B"
    )
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    check = SessionLocal()
    rows = list(
        check.execute(
            select(RecentView).where(
                RecentView.user_id == user_id, RecentView.target_type == target_type, RecentView.target_id == target_id
            )
        ).scalars()
    )
    check.close()

    return {"scenario": "concurrent_update_existing", "a": out.get("a"), "b": out.get("b"), "row_count": len(rows)}


SCENARIOS = {
    "concurrent_first_insert": scenario_concurrent_first_insert,
    "concurrent_update_existing": scenario_concurrent_update_existing,
}


def main() -> None:
    fn = SCENARIOS.get(scenario)
    if fn is None:
        raise ValueError(f"알 수 없는 시나리오: {scenario}")
    result = fn()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
