"""
tests/integration/_fulfillment_concurrency_runner.py
------------------------------------------------------
실제(격리) PostgreSQL 위에서, 서로 다른 DB 커넥션/세션을 쓰는 두 "worker"가
services.fulfillment_service.FulfillmentService의 두 경합 지점을 실제로
동시에 건드렸을 때 안전한지 검증한다 - SQLite는 파일 단위 잠금이 두 커넥션의
동시 쓰기를 직렬화해버려 이 경합 자체를 재현하지 못한다.

시나리오 1) concurrent_batch_creation_same_order_item:
두 worker가 같은 order_item(수량=10)에 대해 각각 6개씩 배치를 생성하면
(합계 12 > 10) FulfillmentService.create_batch()의
ExternalCommandRepository.acquire_target_lock(_LOCK_ORDER_ITEM)이 실제로
두 트랜잭션을 직렬화해, 정확히 한 worker만 성공하고 다른 worker는
FulfillmentValidationError(잔여수량 초과)로 막히는지 확인한다 - 초과 배정이
발생하면 안 된다.

시나리오 2) concurrent_pack_same_batch_item:
검수완료(VERIFIED)된 같은 배치 항목 하나를 두 worker가 동시에
pack_and_register_tracking()으로 포장 확정하면, _LOCK_INVENTORY 잠금 대기 중
먼저 커밋된 worker의 결과를 나중 worker가 잠금 획득 직후 재조회(refresh)해
스스로 물러나는지(ALREADY_PROCESSED) - 재고가 정확히 한 번만 차감되는지
확인한다(단순 SELECT 검사만으로는 이 경합을 못 막는다는 것이 이 스크립트가
검증하는 핵심이다).

tests/integration/test_fulfillment_concurrency_pg.py가 격리된 1회성 docker
postgres 컨테이너를 띄우고, 이 스크립트를 그 postgres와 같은(--internal) 네트워크에
붙은 러너 컨테이너 안에서 실행한다(레포를 읽기전용으로 마운트) -
tests/integration/_product_bulk_concurrency_runner.py와 동일한 격리 패턴.

DB 스키마는 Alembic 마이그레이션이 아니라 Base.metadata.create_all()로 만든다
(마이그레이션 자체의 정합성은 별도 테스트가 검증한다 - 이 스크립트는 런타임
동시성 동작만 검증한다). 실제 채널 API는 호출하지 않는다(submit_to_channel은
이 스크립트에서 아예 호출하지 않는다 - enqueue 이후는 기존 outbox 테스트가
이미 커버한다).

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

db_host = os.environ["FULCONC_DB_HOST"]
db_user = os.environ["FULCONC_DB_USER"]
db_password = os.environ["FULCONC_DB_PASSWORD"]
db_name = os.environ["FULCONC_DB_NAME"]
scenario = os.environ["FULCONC_SCENARIO"]

os.environ["DATABASE_URL"] = f"postgresql+psycopg://{db_user}:{db_password}@{db_host}:5432/{db_name}"

from sqlalchemy import select  # noqa: E402

from core.database import SessionLocal, engine  # noqa: E402
from models import Base  # noqa: E402
from models.inventory import Inventory, Warehouse  # noqa: E402
from models.order import Order, OrderItem  # noqa: E402
from models.platform import Platform  # noqa: E402
from models.product import Product, ProductOption  # noqa: E402
from services.fulfillment_service import FulfillmentService, FulfillmentValidationError  # noqa: E402

Base.metadata.create_all(engine)


def _make_order_item(quantity: int) -> tuple[int, int, int]:
    """(order_item_id, product_option_id, warehouse_id)를 반환한다."""
    setup = SessionLocal()
    platform = Platform(
        code=f"pf-{uuid.uuid4().hex[:8]}",
        name="동시성테스트플랫폼",
        connector_class="StubConnector",
        settlement_cycle_days=7,
        is_active=True,
    )
    setup.add(platform)
    warehouse = Warehouse(name=f"동시성창고-{uuid.uuid4().hex[:6]}", is_active=True)
    setup.add(warehouse)
    setup.flush()
    product = Product(name="동시성테스트상품", category="TEST", status="ACTIVE")
    setup.add(product)
    setup.flush()
    option = ProductOption(product_id=product.id, sku_code=f"SKU-{uuid.uuid4().hex[:8]}", is_active=True)
    setup.add(option)
    setup.flush()
    order = Order(
        platform_id=platform.id,
        platform_order_no=f"FULCONC-{uuid.uuid4().hex[:8]}",
        status="NEW",
        order_date=datetime.now(timezone.utc),
        total_amount=quantity * 1000,
    )
    setup.add(order)
    setup.flush()
    item = OrderItem(
        order_id=order.id,
        product_option_id=option.id,
        quantity=quantity,
        unit_price=1000,
        line_amount=1000 * quantity,
        platform_order_item_no=f"FULCONC-LINE-{uuid.uuid4().hex[:8]}",
    )
    setup.add(item)
    setup.add(
        Inventory(
            product_option_id=option.id,
            warehouse_id=warehouse.id,
            sellable_stock=1000,
            reserved_stock=0,
            safety_stock=0,
            updated_at=datetime.now(timezone.utc),
        )
    )
    setup.flush()
    setup.commit()
    ids = (item.id, option.id, warehouse.id)
    setup.close()
    return ids


def _worker_create_batch(
    order_item_id: int, warehouse_id: int, quantity: int, barrier: threading.Barrier, out: dict, key: str
) -> None:
    session = SessionLocal()
    try:
        service = FulfillmentService(session)
        barrier.wait(timeout=10)
        try:
            batch = service.create_batch(warehouse_id, [(order_item_id, quantity)], created_by=None)
            session.commit()
            out[key] = {"outcome": "ACCEPTED", "batch_id": batch.id, "error": None, "exception": None}
        except FulfillmentValidationError as e:
            session.rollback()
            out[key] = {"outcome": "REJECTED", "batch_id": None, "error": str(e), "exception": None}
    except Exception as e:  # noqa: BLE001 - 실패 자체를 결과로 보고해야 호스트에서 판정할 수 있다.
        session.rollback()
        out[key] = {"outcome": None, "batch_id": None, "error": None, "exception": f"{type(e).__name__}: {e}"}
    finally:
        session.close()


def scenario_concurrent_batch_creation_same_order_item() -> dict:
    order_item_id, _option_id, warehouse_id = _make_order_item(quantity=10)
    barrier = threading.Barrier(2)
    out: dict = {}
    t1 = threading.Thread(
        target=_worker_create_batch, args=(order_item_id, warehouse_id, 6, barrier, out, "a"), name="worker-A"
    )
    t2 = threading.Thread(
        target=_worker_create_batch, args=(order_item_id, warehouse_id, 6, barrier, out, "b"), name="worker-B"
    )
    t1.start()
    t2.start()
    t1.join(timeout=20)
    t2.join(timeout=20)

    check = SessionLocal()
    order_quantity = check.execute(select(OrderItem.quantity).where(OrderItem.id == order_item_id)).scalar_one()
    check.close()

    return {
        "scenario": "concurrent_batch_creation_same_order_item",
        "order_quantity": order_quantity,
        "a": out.get("a"),
        "b": out.get("b"),
    }


def _walk_to_verified(service: "FulfillmentService", batch_item_id: int, quantity: int) -> None:
    service.start_picking(batch_item_id, "READY", actor=None)
    service.complete_picking(batch_item_id, "PICKING", actor=None, picked_quantity=quantity)
    service.start_verification(batch_item_id, "PICKED", actor=None)
    service.complete_verification(batch_item_id, "VERIFYING", actor=None, verified_quantity=quantity)


def _worker_pack(
    batch_item_id: int, carrier: str, tracking_no: str, barrier: threading.Barrier, out: dict, key: str
) -> None:
    session = SessionLocal()
    try:
        service = FulfillmentService(session)
        barrier.wait(timeout=10)
        outcomes = service.pack_and_register_tracking([batch_item_id], carrier, tracking_no, actor=None)
        session.commit()
        out[key] = {"outcome": outcomes[0].outcome, "error_code": outcomes[0].error_code, "exception": None}
    except Exception as e:  # noqa: BLE001 - 실패 자체를 결과로 보고해야 호스트에서 판정할 수 있다.
        session.rollback()
        out[key] = {"outcome": None, "error_code": None, "exception": f"{type(e).__name__}: {e}"}
    finally:
        session.close()


def scenario_concurrent_pack_same_batch_item() -> dict:
    order_item_id, option_id, warehouse_id = _make_order_item(quantity=5)
    setup = SessionLocal()
    setup_service = FulfillmentService(setup)
    batch = setup_service.create_batch(warehouse_id, [(order_item_id, 5)], created_by=None)
    setup.commit()
    batch_item_id = setup_service.item_repo.list_by_batch(batch.id)[0].id
    _walk_to_verified(setup_service, batch_item_id, 5)
    setup.commit()
    setup.close()

    barrier = threading.Barrier(2)
    out: dict = {}
    t1 = threading.Thread(
        target=_worker_pack, args=(batch_item_id, "CJ_LOGISTICS", "RACE-AAAA", barrier, out, "a"), name="worker-A"
    )
    t2 = threading.Thread(
        target=_worker_pack, args=(batch_item_id, "CJ_LOGISTICS", "RACE-BBBB", barrier, out, "b"), name="worker-B"
    )
    t1.start()
    t2.start()
    t1.join(timeout=20)
    t2.join(timeout=20)

    check = SessionLocal()
    inventory = check.execute(
        select(Inventory).where(Inventory.product_option_id == option_id, Inventory.warehouse_id == warehouse_id)
    ).scalar_one()
    check.close()

    return {
        "scenario": "concurrent_pack_same_batch_item",
        "sellable_stock": inventory.sellable_stock,
        "a": out.get("a"),
        "b": out.get("b"),
    }


SCENARIOS = {
    "concurrent_batch_creation_same_order_item": scenario_concurrent_batch_creation_same_order_item,
    "concurrent_pack_same_batch_item": scenario_concurrent_pack_same_batch_item,
}


def main() -> None:
    fn = SCENARIOS.get(scenario)
    if fn is None:
        raise ValueError(f"알 수 없는 시나리오: {scenario}")
    result = fn()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
