"""
tests/unit/test_fulfillment_service.py
------------------------------------------
services.fulfillment_service.FulfillmentService - 출고 배치(피킹/검수/포장)
업무로직 검증. 실제 채널 API는 호출하지 않는다(스텁 커넥터만 사용) - 이
서비스는 채널 전송을 enqueue()까지만 하고 실제 HTTP 호출은 기존
outbox_dispatch_job이 담당한다(여기서 검증 대상이 아니다).

합성(테스트용) 주문번호/상품명만 사용한다 - 실제 주문번호/송장번호/전화번호/
주소는 어디에도 쓰지 않는다.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import pytest
from sqlalchemy import text

from models.inventory import Inventory
from models.order import Order, OrderItem, Shipment, ShipmentItem
from repositories.integration_sync_repository import ExternalCommandRepository
from services.fulfillment_service import FulfillmentConflictError, FulfillmentService, FulfillmentValidationError


class StubShipmentConnector:
    """update_* 자체는 이 서비스가 호출하지 않는다(enqueue()까지만) - 여기서는
    submit_shipment()가 실제로 불리지 않는다는 것 자체가 검증 포인트다."""

    supports_shipment_submit = True
    platform_code = "coupang"

    def __init__(self) -> None:
        self.called = False

    def submit_shipment(self, **kwargs: Any) -> Any:
        self.called = True
        raise AssertionError("이 테스트 범위에서는 실제 채널 전송(submit_shipment)이 호출되면 안 된다.")


def _factory(connector: Any):
    return lambda connector_class, session=None, platform_id=None: connector


@pytest.fixture(autouse=True)
def _enable_shipment_submit(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "shipment_channel_submit_enabled", True)


def _make_order(db_session, platform, status: str = "NEW", order_no: Optional[str] = None) -> Order:
    order = Order(
        platform_id=platform.id,
        platform_order_no=order_no or f"FUL-TEST-{uuid.uuid4().hex[:8]}",
        status=status,
        order_date=datetime.now(timezone.utc),
        total_amount=10000,
    )
    db_session.add(order)
    db_session.flush()
    return order


def _make_order_item(
    db_session, order, product_option, quantity: int, order_item_no: Optional[str] = None
) -> OrderItem:
    item = OrderItem(
        order_id=order.id,
        product_option_id=product_option.id,
        quantity=quantity,
        unit_price=1000,
        line_amount=1000 * quantity,
        platform_order_item_no=order_item_no or f"LINE-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(item)
    db_session.flush()
    return item


def _service(db_session) -> FulfillmentService:
    """enqueue()는 실제로 커넥터를 호출하지 않는다(모듈 docstring 참고) - 그래도
    실수로 호출되면 즉시 실패하도록, 호출 시 예외를 던지는 스텁을 기본으로
    꽂아 둔다(모든 테스트가 이 안전장치를 공유한다)."""
    service = FulfillmentService(db_session)
    service.dispatch_service.connector_factory = _factory(StubShipmentConnector())
    return service


class TestListFulfillableOrderItems:
    def test_new_order_item_is_fulfillable(self, db_session, product_option, platform):
        order = _make_order(db_session, platform)
        _make_order_item(db_session, order, product_option, 5)
        db_session.commit()

        result = _service(db_session).list_fulfillable_order_items(platform_id=platform.id)

        assert len(result) == 1
        assert result[0].remaining_quantity == 5
        assert result[0].order_quantity == 5

    def test_cancelled_order_is_excluded(self, db_session, product_option, platform):
        order = _make_order(db_session, platform, status="CANCELED")
        _make_order_item(db_session, order, product_option, 5)
        db_session.commit()

        result = _service(db_session).list_fulfillable_order_items(platform_id=platform.id)

        assert result == []

    def test_whole_order_shipment_excludes_all_items(self, db_session, product_option, platform):
        order = _make_order(db_session, platform)
        item = _make_order_item(db_session, order, product_option, 5)
        shipment = Shipment(carrier="CJ_LOGISTICS", tracking_no="OLDFLOW1", status="READY")
        db_session.add(shipment)
        db_session.flush()
        db_session.add(ShipmentItem(shipment_id=shipment.id, order_id=order.id, order_item_id=None, quantity=None))
        db_session.commit()

        result = _service(db_session).list_fulfillable_order_items(platform_id=platform.id)

        assert result == []
        assert item.id  # 라인 자체는 존재하지만 목록에는 나오지 않아야 한다.

    def test_partial_allocation_reduces_remaining(self, db_session, product_option, platform, warehouse):
        order = _make_order(db_session, platform)
        item = _make_order_item(db_session, order, product_option, 10)
        db_session.commit()
        service = _service(db_session)
        service.create_batch(warehouse.id, [(item.id, 4)], created_by=None)
        db_session.commit()

        result = service.list_fulfillable_order_items(platform_id=platform.id)

        assert result[0].remaining_quantity == 6


class TestCreateBatch:
    def test_happy_path(self, db_session, product_option, second_product_option, platform, warehouse):
        order = _make_order(db_session, platform)
        item1 = _make_order_item(db_session, order, product_option, 5)
        item2 = _make_order_item(db_session, order, second_product_option, 3)
        db_session.commit()

        batch = _service(db_session).create_batch(warehouse.id, [(item1.id, 5), (item2.id, 3)], created_by=None)

        assert batch.id is not None
        assert batch.warehouse_id == warehouse.id
        items = _service(db_session).item_repo.list_by_batch(batch.id)
        assert {i.order_item_id: i.requested_quantity for i in items} == {item1.id: 5, item2.id: 3}
        assert all(i.status == "READY" for i in items)

    def test_duplicate_order_item_in_same_request_rejected(self, db_session, product_option, platform, warehouse):
        order = _make_order(db_session, platform)
        item = _make_order_item(db_session, order, product_option, 5)
        db_session.commit()

        with pytest.raises(FulfillmentValidationError):
            _service(db_session).create_batch(warehouse.id, [(item.id, 2), (item.id, 3)], created_by=None)

    def test_zero_quantity_rejected(self, db_session, product_option, platform, warehouse):
        order = _make_order(db_session, platform)
        item = _make_order_item(db_session, order, product_option, 5)
        db_session.commit()

        with pytest.raises(FulfillmentValidationError):
            _service(db_session).create_batch(warehouse.id, [(item.id, 0)], created_by=None)

    def test_negative_quantity_rejected(self, db_session, product_option, platform, warehouse):
        order = _make_order(db_session, platform)
        item = _make_order_item(db_session, order, product_option, 5)
        db_session.commit()

        with pytest.raises(FulfillmentValidationError):
            _service(db_session).create_batch(warehouse.id, [(item.id, -1)], created_by=None)

    def test_quantity_exceeding_remaining_rejected(self, db_session, product_option, platform, warehouse):
        order = _make_order(db_session, platform)
        item = _make_order_item(db_session, order, product_option, 5)
        db_session.commit()

        with pytest.raises(FulfillmentValidationError):
            _service(db_session).create_batch(warehouse.id, [(item.id, 6)], created_by=None)

    def test_cancelled_order_rejected(self, db_session, product_option, platform, warehouse):
        order = _make_order(db_session, platform, status="CANCELED")
        item = _make_order_item(db_session, order, product_option, 5)
        db_session.commit()

        with pytest.raises(FulfillmentValidationError):
            _service(db_session).create_batch(warehouse.id, [(item.id, 1)], created_by=None)

    def test_second_batch_cannot_exceed_remaining_after_first(self, db_session, product_option, platform, warehouse):
        order = _make_order(db_session, platform)
        item = _make_order_item(db_session, order, product_option, 5)
        db_session.commit()
        service = _service(db_session)
        service.create_batch(warehouse.id, [(item.id, 4)], created_by=None)
        db_session.commit()

        with pytest.raises(FulfillmentValidationError):
            service.create_batch(warehouse.id, [(item.id, 2)], created_by=None)


class TestPickingAndVerification:
    def _create_ready_item(self, db_session, product_option, platform, warehouse, quantity=5):
        order = _make_order(db_session, platform)
        item = _make_order_item(db_session, order, product_option, quantity)
        db_session.commit()
        service = _service(db_session)
        batch = service.create_batch(warehouse.id, [(item.id, quantity)], created_by=None)
        db_session.commit()
        batch_item = service.item_repo.list_by_batch(batch.id)[0]
        return service, batch_item

    def test_start_picking(self, db_session, product_option, platform, warehouse):
        service, batch_item = self._create_ready_item(db_session, product_option, platform, warehouse)
        updated = service.start_picking(batch_item.id, "READY", actor=None)
        assert updated.status == "PICKING"
        assert updated.picked_by is None

    def test_complete_picking(self, db_session, product_option, platform, warehouse):
        service, batch_item = self._create_ready_item(db_session, product_option, platform, warehouse, quantity=5)
        service.start_picking(batch_item.id, "READY", actor=None)
        updated = service.complete_picking(batch_item.id, "PICKING", actor=None, picked_quantity=5)
        assert updated.status == "PICKED"
        assert updated.picked_quantity == 5

    def test_complete_picking_over_requested_rejected(self, db_session, product_option, platform, warehouse):
        service, batch_item = self._create_ready_item(db_session, product_option, platform, warehouse, quantity=5)
        service.start_picking(batch_item.id, "READY", actor=None)
        with pytest.raises(FulfillmentValidationError):
            service.complete_picking(batch_item.id, "PICKING", actor=None, picked_quantity=6)

    def test_verify_matching_quantity_succeeds(self, db_session, product_option, platform, warehouse):
        service, batch_item = self._create_ready_item(db_session, product_option, platform, warehouse, quantity=5)
        service.start_picking(batch_item.id, "READY", actor=None)
        service.complete_picking(batch_item.id, "PICKING", actor=None, picked_quantity=5)
        service.start_verification(batch_item.id, "PICKED", actor=None)
        updated = service.complete_verification(batch_item.id, "VERIFYING", actor=None, verified_quantity=5)
        assert updated.status == "VERIFIED"
        assert updated.verified_quantity == 5

    def test_verify_mismatched_quantity_blocks(self, db_session, product_option, platform, warehouse):
        service, batch_item = self._create_ready_item(db_session, product_option, platform, warehouse, quantity=5)
        service.start_picking(batch_item.id, "READY", actor=None)
        service.complete_picking(batch_item.id, "PICKING", actor=None, picked_quantity=5)
        service.start_verification(batch_item.id, "PICKED", actor=None)
        updated = service.complete_verification(batch_item.id, "VERIFYING", actor=None, verified_quantity=3)
        assert updated.status == "BLOCKED"
        assert updated.failure_reason_code == "QUANTITY_MISMATCH"

    def test_stale_expected_status_raises_conflict(self, db_session, product_option, platform, warehouse):
        service, batch_item = self._create_ready_item(db_session, product_option, platform, warehouse)
        service.start_picking(batch_item.id, "READY", actor=None)
        with pytest.raises(FulfillmentConflictError):
            service.start_picking(batch_item.id, "READY", actor=None)  # 이미 PICKING인데 READY를 기대함

    def test_invalid_requested_transition_is_validation_error_not_500(
        self, db_session, product_option, platform, warehouse
    ):
        service, batch_item = self._create_ready_item(db_session, product_option, platform, warehouse)
        # READY에서 곧바로 VERIFYING을 기대하는 것 자체가 상태머신에 없는 전이다.
        with pytest.raises(FulfillmentValidationError):
            service.start_verification(batch_item.id, "READY", actor=None)


class TestPackAndRegisterTracking:
    def _verified_item(self, db_session, product_option, platform, warehouse, quantity=5, verified_quantity=None):
        order = _make_order(db_session, platform)
        item = _make_order_item(db_session, order, product_option, quantity)
        db_session.commit()
        service = _service(db_session)
        batch = service.create_batch(warehouse.id, [(item.id, quantity)], created_by=None)
        db_session.commit()
        batch_item = service.item_repo.list_by_batch(batch.id)[0]
        service.start_picking(batch_item.id, "READY", actor=None)
        service.complete_picking(batch_item.id, "PICKING", actor=None, picked_quantity=quantity)
        service.start_verification(batch_item.id, "PICKED", actor=None)
        vq = verified_quantity if verified_quantity is not None else quantity
        service.complete_verification(batch_item.id, "VERIFYING", actor=None, verified_quantity=vq)
        return service, batch_item

    def test_happy_path_deducts_inventory_and_creates_shipment(
        self, db_session, product_option, platform, warehouse, inventory_row
    ):
        service, batch_item = self._verified_item(db_session, product_option, platform, warehouse, quantity=5)
        before = inventory_row.sellable_stock

        outcomes = service.pack_and_register_tracking([batch_item.id], "CJ_LOGISTICS", "1234-5678", actor=None)

        assert outcomes[0].outcome == "ACCEPTED"
        db_session.refresh(batch_item)
        assert batch_item.status == "PACKED"
        assert batch_item.inventory_deducted_at is not None
        assert batch_item.shipment_id is not None
        db_session.refresh(inventory_row)
        assert inventory_row.sellable_stock == before - 5

        shipment = db_session.get(Shipment, batch_item.shipment_id)
        assert shipment.tracking_no == "12345678"  # 하이픈 제거 정규화
        shipment_items = [si for si in shipment.items if si.order_item_id == batch_item.order_item_id]
        assert len(shipment_items) == 1
        assert shipment_items[0].quantity == 5

    def test_multiple_items_share_one_shipment(
        self, db_session, product_option, second_product_option, platform, warehouse, inventory_row
    ):
        from models.inventory import Inventory as InventoryModel

        inv2 = InventoryModel(
            product_option_id=second_product_option.id,
            warehouse_id=warehouse.id,
            sellable_stock=50,
            reserved_stock=0,
            safety_stock=0,
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(inv2)
        db_session.commit()

        order = _make_order(db_session, platform)
        item1 = _make_order_item(db_session, order, product_option, 3)
        item2 = _make_order_item(db_session, order, second_product_option, 2)
        db_session.commit()
        service = _service(db_session)
        batch = service.create_batch(warehouse.id, [(item1.id, 3), (item2.id, 2)], created_by=None)
        db_session.commit()
        batch_items = service.item_repo.list_by_batch(batch.id)
        for bi in batch_items:
            service.start_picking(bi.id, "READY", actor=None)
            service.complete_picking(bi.id, "PICKING", actor=None, picked_quantity=bi.requested_quantity)
            service.start_verification(bi.id, "PICKED", actor=None)
            service.complete_verification(bi.id, "VERIFYING", actor=None, verified_quantity=bi.requested_quantity)

        outcomes = service.pack_and_register_tracking(
            [bi.id for bi in batch_items], "CJ_LOGISTICS", "999888777", actor=None
        )

        assert all(o.outcome == "ACCEPTED" for o in outcomes)
        refreshed = [service.item_repo.get_by_id(bi.id) for bi in batch_items]
        shipment_ids = {r.shipment_id for r in refreshed if r is not None}
        assert len(shipment_ids) == 1

    def test_invalid_carrier_rejected_without_state_change(
        self, db_session, product_option, platform, warehouse, inventory_row
    ):
        service, batch_item = self._verified_item(db_session, product_option, platform, warehouse)
        outcomes = service.pack_and_register_tracking([batch_item.id], "DHL", "12345", actor=None)
        assert outcomes[0].outcome == "VALIDATION_FAILED"
        db_session.refresh(batch_item)
        assert batch_item.status == "VERIFIED"

    def test_empty_tracking_rejected(self, db_session, product_option, platform, warehouse, inventory_row):
        service, batch_item = self._verified_item(db_session, product_option, platform, warehouse)
        outcomes = service.pack_and_register_tracking([batch_item.id], "CJ_LOGISTICS", "   ", actor=None)
        assert outcomes[0].outcome == "VALIDATION_FAILED"

    def test_duplicate_tracking_rejected(
        self, db_session, product_option, second_product_option, platform, warehouse, inventory_row
    ):
        from models.inventory import Inventory as InventoryModel

        inv2 = InventoryModel(
            product_option_id=second_product_option.id,
            warehouse_id=warehouse.id,
            sellable_stock=50,
            reserved_stock=0,
            safety_stock=0,
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(inv2)
        db_session.commit()
        service, item1 = self._verified_item(db_session, product_option, platform, warehouse, quantity=2)
        service.pack_and_register_tracking([item1.id], "CJ_LOGISTICS", "555-666", actor=None)
        db_session.commit()

        order2 = _make_order(db_session, platform)
        item2_order = _make_order_item(db_session, order2, second_product_option, 2)
        db_session.commit()
        batch2 = service.create_batch(warehouse.id, [(item2_order.id, 2)], created_by=None)
        db_session.commit()
        bi2 = service.item_repo.list_by_batch(batch2.id)[0]
        service.start_picking(bi2.id, "READY", actor=None)
        service.complete_picking(bi2.id, "PICKING", actor=None, picked_quantity=2)
        service.start_verification(bi2.id, "PICKED", actor=None)
        service.complete_verification(bi2.id, "VERIFYING", actor=None, verified_quantity=2)

        outcomes = service.pack_and_register_tracking([bi2.id], "CJ_LOGISTICS", "555666", actor=None)  # 하이픈만 다름

        assert outcomes[0].outcome == "VALIDATION_FAILED"

    def test_insufficient_stock_blocks_item(self, db_session, product_option, platform, warehouse):
        inv = Inventory(
            product_option_id=product_option.id,
            warehouse_id=warehouse.id,
            sellable_stock=2,
            reserved_stock=0,
            safety_stock=0,
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(inv)
        db_session.commit()
        service, batch_item = self._verified_item(
            db_session, product_option, platform, warehouse, quantity=2, verified_quantity=2
        )
        # 검수 확정 이후 다른 곳에서 재고가 줄어든 상황을 흉내낸다(예: 다른 채널 판매).
        inv.sellable_stock = 0
        db_session.commit()

        outcomes = service.pack_and_register_tracking([batch_item.id], "CJ_LOGISTICS", "777", actor=None)

        assert outcomes[0].outcome == "BLOCKED"
        db_session.refresh(batch_item)
        assert batch_item.status == "BLOCKED"
        assert batch_item.failure_reason_code == "INSUFFICIENT_STOCK"

    def test_already_packed_item_is_not_reprocessed(
        self, db_session, product_option, platform, warehouse, inventory_row
    ):
        service, batch_item = self._verified_item(db_session, product_option, platform, warehouse, quantity=5)
        service.pack_and_register_tracking([batch_item.id], "CJ_LOGISTICS", "111222", actor=None)
        db_session.commit()
        stock_after_first = db_session.get(type(inventory_row), inventory_row.id).sellable_stock

        outcomes = service.pack_and_register_tracking([batch_item.id], "CJ_LOGISTICS", "333444", actor=None)

        assert outcomes[0].outcome == "VALIDATION_FAILED"
        db_session.refresh(inventory_row)
        assert inventory_row.sellable_stock == stock_after_first  # 두 번째 시도로 재차감되지 않았다.

    def test_concurrent_winner_commit_during_lock_wait_is_not_double_deducted(
        self, db_session, product_option, platform, warehouse, inventory_row
    ):
        """실제 두 커넥션의 경합(잠금 대기 자체)은 SQLite로 재현할 수 없다 - 그건
        tests/integration/test_fulfillment_concurrency_pg.py가 격리 PostgreSQL로
        검증한다. 여기서는 그 경합의 결과(잠금을 얻었더니 다른 트랜잭션이 이미
        커밋해 PACKED로 바뀌어 있는 상황)에서 재확인(refresh) 로직이 실제로
        재차감을 건너뛰는지를 acquire_target_lock을 가로채 흉내내 고정한다."""
        service, batch_item = self._verified_item(db_session, product_option, platform, warehouse, quantity=5)
        before = inventory_row.sellable_stock
        original_acquire = service.command_repo.acquire_target_lock

        def _acquire_then_simulate_concurrent_winner(target_type: str, target_id: int) -> None:
            original_acquire(target_type, target_id)
            db_session.execute(
                text("UPDATE fulfillment_batch_items SET status = 'PACKED' WHERE id = :id"), {"id": batch_item.id}
            )

        service.command_repo.acquire_target_lock = _acquire_then_simulate_concurrent_winner

        outcomes = service.pack_and_register_tracking([batch_item.id], "CJ_LOGISTICS", "RACE-0001", actor=None)

        assert outcomes[0].outcome == "ALREADY_PROCESSED"
        db_session.refresh(inventory_row)
        assert inventory_row.sellable_stock == before  # 재고가 두 번째로 차감되지 않았다.


class TestSubmitToChannel:
    def _packed_item(self, db_session, product_option, platform, warehouse, inventory_row):
        order = _make_order(db_session, platform)
        item = _make_order_item(db_session, order, product_option, 4)
        db_session.commit()
        service = _service(db_session)
        batch = service.create_batch(warehouse.id, [(item.id, 4)], created_by=None)
        db_session.commit()
        bi = service.item_repo.list_by_batch(batch.id)[0]
        service.start_picking(bi.id, "READY", actor=None)
        service.complete_picking(bi.id, "PICKING", actor=None, picked_quantity=4)
        service.start_verification(bi.id, "PICKED", actor=None)
        service.complete_verification(bi.id, "VERIFYING", actor=None, verified_quantity=4)
        service.pack_and_register_tracking([bi.id], "CJ_LOGISTICS", "SUBMIT-TEST-1", actor=None)
        db_session.commit()
        refreshed = service.item_repo.get_by_id(bi.id)
        assert refreshed is not None
        return service, refreshed

    def test_submit_creates_pending_command(self, db_session, product_option, platform, warehouse, inventory_row):
        service, bi = self._packed_item(db_session, product_option, platform, warehouse, inventory_row)

        outcomes = service.submit_to_channel([bi.shipment_id], actor=None)

        assert outcomes[0].outcome == "ACCEPTED"
        db_session.refresh(bi)
        assert bi.status == "SUBMITTED"
        commands = ExternalCommandRepository(db_session).list_for_targets("SHIPMENT_SUBMIT", "SHIPMENT", bi.shipment_id)
        assert len(commands) == 1
        assert commands[0].status == "PENDING"

    def test_submit_disabled_flag_reverts_to_packed(
        self, db_session, product_option, platform, warehouse, inventory_row, monkeypatch
    ):
        from config.settings import settings

        service, bi = self._packed_item(db_session, product_option, platform, warehouse, inventory_row)
        monkeypatch.setattr(settings, "shipment_channel_submit_enabled", False)

        outcomes = service.submit_to_channel([bi.shipment_id], actor=None)

        assert outcomes[0].outcome == "FAILED_TO_ENQUEUE"
        db_session.refresh(bi)
        assert bi.status == "PACKED"

    def test_retry_only_failed_commands(self, db_session, product_option, platform, warehouse, inventory_row):
        service, bi = self._packed_item(db_session, product_option, platform, warehouse, inventory_row)
        service.submit_to_channel([bi.shipment_id], actor=None)
        db_session.commit()
        command = ExternalCommandRepository(db_session).list_for_targets("SHIPMENT_SUBMIT", "SHIPMENT", bi.shipment_id)[
            0
        ]
        command.status = "FAILED"
        db_session.commit()

        outcomes = service.retry_failed_items([bi.id])

        assert outcomes[0].outcome == "ACCEPTED"
        db_session.refresh(command)
        assert command.status == "PENDING"

    def test_retry_blocks_unknown(self, db_session, product_option, platform, warehouse, inventory_row):
        service, bi = self._packed_item(db_session, product_option, platform, warehouse, inventory_row)
        service.submit_to_channel([bi.shipment_id], actor=None)
        db_session.commit()
        command = ExternalCommandRepository(db_session).list_for_targets("SHIPMENT_SUBMIT", "SHIPMENT", bi.shipment_id)[
            0
        ]
        command.status = "UNKNOWN"
        db_session.commit()

        outcomes = service.retry_failed_items([bi.id])

        assert outcomes[0].outcome == "BLOCKED"
        assert outcomes[0].error_code == "UNKNOWN_REQUIRES_RESOLUTION"
        db_session.refresh(command)
        assert command.status == "UNKNOWN"

    def test_retry_rejects_non_failed_status(self, db_session, product_option, platform, warehouse, inventory_row):
        service, bi = self._packed_item(db_session, product_option, platform, warehouse, inventory_row)
        service.submit_to_channel([bi.shipment_id], actor=None)
        db_session.commit()

        outcomes = service.retry_failed_items([bi.id])  # 아직 PENDING인 상태

        assert outcomes[0].outcome == "VALIDATION_FAILED"


class TestCancelItem:
    def test_cancel_before_packing_needs_no_restock(self, db_session, product_option, platform, warehouse):
        order = _make_order(db_session, platform)
        item = _make_order_item(db_session, order, product_option, 5)
        db_session.commit()
        service = _service(db_session)
        batch = service.create_batch(warehouse.id, [(item.id, 5)], created_by=None)
        db_session.commit()
        bi = service.item_repo.list_by_batch(batch.id)[0]

        cancelled = service.cancel_item(bi.id, "READY", actor=None)

        assert cancelled.status == "CANCELLED"

    def test_cancel_after_packing_restores_inventory(
        self, db_session, product_option, platform, warehouse, inventory_row
    ):
        order = _make_order(db_session, platform)
        item = _make_order_item(db_session, order, product_option, 5)
        db_session.commit()
        service = _service(db_session)
        batch = service.create_batch(warehouse.id, [(item.id, 5)], created_by=None)
        db_session.commit()
        bi = service.item_repo.list_by_batch(batch.id)[0]
        service.start_picking(bi.id, "READY", actor=None)
        service.complete_picking(bi.id, "PICKING", actor=None, picked_quantity=5)
        service.start_verification(bi.id, "PICKED", actor=None)
        service.complete_verification(bi.id, "VERIFYING", actor=None, verified_quantity=5)
        service.pack_and_register_tracking([bi.id], "CJ_LOGISTICS", "CANCEL-TEST-1", actor=None)
        db_session.commit()
        after_pack = db_session.get(type(inventory_row), inventory_row.id).sellable_stock

        cancelled = service.cancel_item(bi.id, "PACKED", actor=None)

        assert cancelled.status == "CANCELLED"
        db_session.refresh(inventory_row)
        assert inventory_row.sellable_stock == after_pack + 5

    def test_cancel_after_submitted_is_rejected(self, db_session, product_option, platform, warehouse, inventory_row):
        order = _make_order(db_session, platform)
        item = _make_order_item(db_session, order, product_option, 4)
        db_session.commit()
        service = _service(db_session)
        batch = service.create_batch(warehouse.id, [(item.id, 4)], created_by=None)
        db_session.commit()
        bi = service.item_repo.list_by_batch(batch.id)[0]
        service.start_picking(bi.id, "READY", actor=None)
        service.complete_picking(bi.id, "PICKING", actor=None, picked_quantity=4)
        service.start_verification(bi.id, "PICKED", actor=None)
        service.complete_verification(bi.id, "VERIFYING", actor=None, verified_quantity=4)
        service.pack_and_register_tracking([bi.id], "CJ_LOGISTICS", "SUBMITTED-CANCEL-1", actor=None)
        db_session.commit()
        refreshed = service.item_repo.get_by_id(bi.id)
        assert refreshed is not None and refreshed.shipment_id is not None
        service.submit_to_channel([refreshed.shipment_id], actor=None)
        db_session.commit()
        refreshed = service.item_repo.get_by_id(bi.id)
        assert refreshed is not None
        assert refreshed.status == "SUBMITTED"

        with pytest.raises(FulfillmentValidationError):
            service.cancel_item(refreshed.id, "SUBMITTED", actor=None)

    def test_stale_expected_status_raises_conflict(self, db_session, product_option, platform, warehouse):
        order = _make_order(db_session, platform)
        item = _make_order_item(db_session, order, product_option, 5)
        db_session.commit()
        service = _service(db_session)
        batch = service.create_batch(warehouse.id, [(item.id, 5)], created_by=None)
        db_session.commit()
        bi = service.item_repo.list_by_batch(batch.id)[0]
        service.start_picking(bi.id, "READY", actor=None)

        with pytest.raises(FulfillmentConflictError):
            service.cancel_item(bi.id, "READY", actor=None)  # 이미 PICKING이다
