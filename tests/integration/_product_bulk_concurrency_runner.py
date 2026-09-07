"""
tests/integration/_product_bulk_concurrency_runner.py
------------------------------------------------------------------
실제(격리) PostgreSQL 위에서, 서로 다른 DB 커넥션/세션을 쓰는 두 "worker" 스레드가
ProductBulkService.submit_inventory_updates()를 barrier로 동시에(같은
product_platform_map_id, 같은 target_quantity - 즉 같은 idempotency_key) 호출했을
때 정확히 하나의 ExternalCommand만 남고 둘 다 예외 없이 끝나는지 검증한다.

이 경합은 SQLite로는 재현할 수 없다(파일 단위 잠금이 두 커넥션의 동시 쓰기를
직렬화해버린다) - 진짜 두 커넥션이 겹쳐 쓰기를 시도해야 idempotency_key 유니크
인덱스 위반(IntegrityError)이 실제로 발생하고, services.product_bulk_service.
ProductBulkService._submit_bulk의 IntegrityError 재시도 경로(모듈 docstring 참고)가
실제로 그 경합을 흡수하는지 확인할 수 있다.

tests/integration/test_product_bulk_concurrency_pg.py가 격리된 1회성 docker
postgres 컨테이너를 띄우고, 이 스크립트를 그 postgres와 같은(--internal) 네트워크에
붙은 러너 컨테이너 안에서 실행한다(레포를 읽기전용으로 마운트) -
tests/integration/_recent_view_concurrency_runner.py와 동일한 격리 패턴.

DB 스키마는 Alembic 마이그레이션이 아니라 Base.metadata.create_all()로 만든다
(마이그레이션 자체의 정합성은 별도 테스트가 검증한다 - 이 스크립트는 런타임
동시성 동작만 검증).

실제 채널 API는 호출하지 않는다 - connector_factory를 스텁으로 주입해 capability
확인만 하고 update_inventory() 등은 애초에 호출되지 않는다(submit_inventory_updates는
enqueue만 한다).

결과를 JSON 한 줄로 표준출력 마지막 줄에 출력한다.
"""

import json
import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

db_host = os.environ["PBCONC_DB_HOST"]
db_user = os.environ["PBCONC_DB_USER"]
db_password = os.environ["PBCONC_DB_PASSWORD"]
db_name = os.environ["PBCONC_DB_NAME"]
scenario = os.environ["PBCONC_SCENARIO"]

os.environ["DATABASE_URL"] = f"postgresql+psycopg://{db_user}:{db_password}@{db_host}:5432/{db_name}"

from sqlalchemy import select  # noqa: E402

from config.settings import settings  # noqa: E402
from core.database import SessionLocal, engine  # noqa: E402
from models import Base  # noqa: E402
from models.integration_sync import ExternalCommand  # noqa: E402
from models.platform import Platform  # noqa: E402
from models.product import Product, ProductOption, ProductPlatformMap  # noqa: E402
from services.product_bulk_service import BulkInventoryItem, ProductBulkService  # noqa: E402
from services.product_sync_dispatch_service import INVENTORY_UPDATE, TARGET_TYPE  # noqa: E402

Base.metadata.create_all(engine)
settings.product_channel_sync_enabled = True


class _StubConnector:
    supports_inventory_update = True


def _connector_factory(connector_class: str, session: Any = None, platform_id: Optional[int] = None) -> Any:
    return _StubConnector()


def _make_mapping() -> int:
    setup = SessionLocal()
    platform = Platform(
        code=f"pf-{uuid.uuid4().hex[:8]}",
        name="동시성테스트플랫폼",
        connector_class="StubConnector",
        settlement_cycle_days=7,
        is_active=True,
    )
    setup.add(platform)
    setup.flush()
    product = Product(name="동시성테스트상품", category="TEST", status="ACTIVE")
    setup.add(product)
    setup.flush()
    option = ProductOption(product_id=product.id, sku_code=f"SKU-{uuid.uuid4().hex[:8]}", is_active=True)
    setup.add(option)
    setup.flush()
    mapping = ProductPlatformMap(
        product_option_id=option.id, platform_id=platform.id, platform_option_id=f"EXT-{uuid.uuid4().hex[:8]}"
    )
    setup.add(mapping)
    setup.flush()
    setup.commit()
    mapping_id = mapping.id
    setup.close()
    return mapping_id


def _worker(mapping_id: int, quantity: int, barrier: threading.Barrier, out: dict, key: str) -> None:
    session = SessionLocal()
    try:
        service = ProductBulkService(session, connector_factory=_connector_factory)
        barrier.wait(timeout=10)
        result = service.submit_inventory_updates(
            [BulkInventoryItem(product_platform_map_id=mapping_id, target_quantity=quantity)]
        )
        item = result.items[0]
        out[key] = {
            "outcome": item.outcome,
            "command_id": item.command_id,
            "error_code": item.error_code,
            "aborted": result.aborted,
            "exception": None,
        }
    except Exception as e:  # noqa: BLE001 - 실패 자체를 결과로 보고해야 호스트에서 판정할 수 있다.
        out[key] = {
            "outcome": None,
            "command_id": None,
            "error_code": None,
            "aborted": None,
            "exception": f"{type(e).__name__}: {e}",
        }
    finally:
        session.close()


def scenario_concurrent_same_target_same_value() -> dict:
    mapping_id = _make_mapping()
    barrier = threading.Barrier(2)
    out: dict = {}
    t1 = threading.Thread(target=_worker, args=(mapping_id, 42, barrier, out, "a"), name="worker-A")
    t2 = threading.Thread(target=_worker, args=(mapping_id, 42, barrier, out, "b"), name="worker-B")
    t1.start()
    t2.start()
    t1.join(timeout=20)
    t2.join(timeout=20)

    check = SessionLocal()
    rows = list(
        check.execute(
            select(ExternalCommand).where(
                ExternalCommand.command_type == INVENTORY_UPDATE,
                ExternalCommand.target_type == TARGET_TYPE,
                ExternalCommand.target_id == mapping_id,
            )
        ).scalars()
    )
    check.close()

    return {
        "scenario": "concurrent_same_target_same_value",
        "a": out.get("a"),
        "b": out.get("b"),
        "row_count": len(rows),
        "command_ids": [r.id for r in rows],
    }


SCENARIOS = {"concurrent_same_target_same_value": scenario_concurrent_same_target_same_value}


def main() -> None:
    fn = SCENARIOS.get(scenario)
    if fn is None:
        raise ValueError(f"알 수 없는 시나리오: {scenario}")
    result = fn()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
