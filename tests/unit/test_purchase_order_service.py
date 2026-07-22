"""
tests/unit/test_purchase_order_service.py
---------------------------------------------
PurchaseOrderService 단위 테스트: DRAFT 작성 -> 품목 추가 -> 확정(ORDERED)
-> 입고 처리(부분/전량, 재고 반영 확인) -> 취소.
"""

import pytest

from models.inventory import Inventory, InventoryTransaction
from services.purchase_order_service import PurchaseOrderService, PurchaseOrderStateError


class TestCreateDraftAndAddItem:
    def test_create_draft_defaults_to_draft_status(self, db_session, supplier):
        po = PurchaseOrderService(db_session).create_draft(supplier.id, memo="1분기 정기발주")
        assert po.status == "DRAFT"
        assert po.memo == "1분기 정기발주"
        assert po.order_date is None

    def test_add_item_to_draft_succeeds(self, db_session, supplier, product_option):
        service = PurchaseOrderService(db_session)
        po = service.create_draft(supplier.id)
        item = service.add_item(po.id, product_option.id, quantity=50, unit_cost=1000)
        assert item.quantity == 50
        assert item.received_quantity == 0

    def test_add_item_to_non_draft_raises(self, db_session, supplier, product_option):
        service = PurchaseOrderService(db_session)
        po = service.create_draft(supplier.id)
        service.add_item(po.id, product_option.id, quantity=10, unit_cost=1000)
        service.confirm(po.id)

        with pytest.raises(PurchaseOrderStateError):
            service.add_item(po.id, product_option.id, quantity=5, unit_cost=1000)


class TestConfirm:
    def test_confirm_sets_ordered_status_and_order_date(self, db_session, supplier, product_option):
        service = PurchaseOrderService(db_session)
        po = service.create_draft(supplier.id)
        service.add_item(po.id, product_option.id, quantity=10, unit_cost=1000)

        confirmed = service.confirm(po.id)
        assert confirmed.status == "ORDERED"
        assert confirmed.order_date is not None

    def test_confirm_without_items_raises(self, db_session, supplier):
        service = PurchaseOrderService(db_session)
        po = service.create_draft(supplier.id)

        with pytest.raises(PurchaseOrderStateError):
            service.confirm(po.id)

    def test_confirm_already_ordered_raises(self, db_session, supplier, product_option):
        service = PurchaseOrderService(db_session)
        po = service.create_draft(supplier.id)
        service.add_item(po.id, product_option.id, quantity=10, unit_cost=1000)
        service.confirm(po.id)

        with pytest.raises(PurchaseOrderStateError):
            service.confirm(po.id)


class TestCancel:
    def test_cancel_draft_succeeds(self, db_session, supplier):
        service = PurchaseOrderService(db_session)
        po = service.create_draft(supplier.id)
        cancelled = service.cancel(po.id)
        assert cancelled.status == "CANCELLED"

    def test_cancel_received_raises(self, db_session, supplier, product_option, warehouse):
        service = PurchaseOrderService(db_session)
        po = service.create_draft(supplier.id)
        item = service.add_item(po.id, product_option.id, quantity=10, unit_cost=1000)
        service.confirm(po.id)
        service.receive_item(po.id, item.id, quantity=10, warehouse_id=warehouse.id)

        with pytest.raises(PurchaseOrderStateError):
            service.cancel(po.id)


class TestReceiveItem:
    def test_partial_receive_sets_partially_received_and_increases_stock(
        self, db_session, supplier, product_option, warehouse
    ):
        service = PurchaseOrderService(db_session)
        po = service.create_draft(supplier.id)
        item = service.add_item(po.id, product_option.id, quantity=100, unit_cost=1000)
        service.confirm(po.id)

        service.receive_item(po.id, item.id, quantity=40, warehouse_id=warehouse.id)

        db_session.refresh(po)
        assert po.status == "PARTIALLY_RECEIVED"
        assert item.received_quantity == 40
        inv = (
            db_session.query(Inventory).filter_by(product_option_id=product_option.id, warehouse_id=warehouse.id).one()
        )
        assert inv.sellable_stock == 40
        txn = db_session.query(InventoryTransaction).filter_by(reference_type="PURCHASE_ORDER").one()
        assert txn.type == "IN"
        assert txn.quantity == 40
        assert txn.reference_id == po.id

    def test_full_receive_across_two_calls_sets_received(self, db_session, supplier, product_option, warehouse):
        service = PurchaseOrderService(db_session)
        po = service.create_draft(supplier.id)
        item = service.add_item(po.id, product_option.id, quantity=30, unit_cost=1000)
        service.confirm(po.id)

        service.receive_item(po.id, item.id, quantity=20, warehouse_id=warehouse.id)
        service.receive_item(po.id, item.id, quantity=10, warehouse_id=warehouse.id)

        db_session.refresh(po)
        assert po.status == "RECEIVED"
        assert item.received_quantity == 30

    def test_receive_more_than_remaining_raises(self, db_session, supplier, product_option, warehouse):
        service = PurchaseOrderService(db_session)
        po = service.create_draft(supplier.id)
        item = service.add_item(po.id, product_option.id, quantity=10, unit_cost=1000)
        service.confirm(po.id)

        with pytest.raises(PurchaseOrderStateError):
            service.receive_item(po.id, item.id, quantity=11, warehouse_id=warehouse.id)

    def test_receive_on_draft_raises(self, db_session, supplier, product_option, warehouse):
        service = PurchaseOrderService(db_session)
        po = service.create_draft(supplier.id)
        item = service.add_item(po.id, product_option.id, quantity=10, unit_cost=1000)

        with pytest.raises(PurchaseOrderStateError):
            service.receive_item(po.id, item.id, quantity=5, warehouse_id=warehouse.id)
