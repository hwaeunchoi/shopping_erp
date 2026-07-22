"""
tests/unit/test_inventory_service.py
-----------------------------------------
InventoryService 단위 테스트. 모든 재고 변동에 inventory_transactions
이력이 함께 남는지도 함께 검증한다(모델 설계 원칙 대응).
"""

import pytest

from models.inventory import InventoryTransaction
from services.inventory_service import InsufficientStockError, InventoryService


class TestReserveAndRelease:
    def test_reserve_increases_reserved_stock(self, db_session, inventory_row):
        result = InventoryService(db_session).reserve(inventory_row.product_option_id, inventory_row.warehouse_id, 5)
        assert result.reserved_stock == 5

    def test_release_reservation_floors_at_zero(self, db_session, inventory_row):
        service = InventoryService(db_session)
        service.reserve(inventory_row.product_option_id, inventory_row.warehouse_id, 3)

        result = service.release_reservation(inventory_row.product_option_id, inventory_row.warehouse_id, 10)

        assert result.reserved_stock == 0


class TestDeductOnShipment:
    def test_deducts_stock_releases_reservation_and_logs_transaction(self, db_session, inventory_row):
        service = InventoryService(db_session)
        service.reserve(inventory_row.product_option_id, inventory_row.warehouse_id, 5)

        result = service.deduct_on_shipment(
            inventory_row.product_option_id, inventory_row.warehouse_id, 5, reference_id=123
        )

        assert result.sellable_stock == 95
        assert result.reserved_stock == 0
        txn = db_session.query(InventoryTransaction).filter_by(reference_type="ORDER", reference_id=123).one()
        assert txn.type == "OUT"
        assert txn.quantity == -5

    def test_insufficient_stock_raises_and_does_not_mutate(self, db_session, inventory_row):
        service = InventoryService(db_session)

        with pytest.raises(InsufficientStockError):
            service.deduct_on_shipment(inventory_row.product_option_id, inventory_row.warehouse_id, 999)

        db_session.refresh(inventory_row)
        assert inventory_row.sellable_stock == 100


class TestInspectReturn:
    """정책 5: 반품 재고는 회수 도착이 아니라 **검수 통과** 시점에 반영된다.

    이전의 restock_on_return()(회수 즉시 무조건 판매가능으로 복원)을 대체한다.
    """

    def test_inspection_pass_restocks_and_logs_inspect_transaction(self, db_session, inventory_row):
        result = InventoryService(db_session).inspect_return(
            inventory_row.product_option_id,
            inventory_row.warehouse_id,
            quantity=10,
            result="PASS",
            reason="반품 검수 양품",
            reference_id=5,
        )

        assert result.inventory.sellable_stock == 110
        txn = db_session.query(InventoryTransaction).filter_by(reference_type="RETURN", reference_id=5).one()
        assert txn.type == "INSPECT"
        assert txn.quantity == 10


class TestUnknownInventory:
    def test_operating_on_missing_inventory_raises(self, db_session):
        with pytest.raises(ValueError):
            InventoryService(db_session).reserve(999999, 999999, 1)


class TestManualAdjust:
    def test_positive_delta_increases_stock_and_logs_transaction(self, db_session, inventory_row):
        result = InventoryService(db_session).manual_adjust(
            inventory_row.product_option_id, inventory_row.warehouse_id, 20, memo="입고"
        )

        assert result.sellable_stock == 120
        txn = db_session.query(InventoryTransaction).filter_by(reference_type="MANUAL").one()
        assert txn.type == "ADJUST"
        assert txn.quantity == 20
        assert txn.memo == "입고"

    def test_negative_delta_decreases_stock(self, db_session, inventory_row):
        result = InventoryService(db_session).manual_adjust(
            inventory_row.product_option_id, inventory_row.warehouse_id, -30, memo="실사 감모"
        )

        assert result.sellable_stock == 70

    def test_negative_delta_below_zero_is_rejected(self, db_session, inventory_row):
        service = InventoryService(db_session)

        with pytest.raises(InsufficientStockError):
            service.manual_adjust(inventory_row.product_option_id, inventory_row.warehouse_id, -999)

        db_session.refresh(inventory_row)
        assert inventory_row.sellable_stock == 100

    def test_creates_new_inventory_row_when_missing(self, db_session, product_option, warehouse):
        result = InventoryService(db_session).manual_adjust(product_option.id, warehouse.id, 10, memo="신규 입고")

        assert result.sellable_stock == 10
        assert result.product_option_id == product_option.id
        assert result.warehouse_id == warehouse.id


class TestUpdateSafetyStock:
    def test_updates_safety_stock_without_logging_transaction(self, db_session, inventory_row):
        result = InventoryService(db_session).update_safety_stock(
            inventory_row.product_option_id, inventory_row.warehouse_id, 15
        )

        assert result.safety_stock == 15
        assert result.sellable_stock == 100  # 재고 수량은 그대로
        assert db_session.query(InventoryTransaction).count() == 0  # 이력 남기지 않음
