"""
tests/unit/test_exchange_return_service.py
------------------------------------------------
ExchangeService/ReturnService/CancellationService 단위 테스트.
"""

from datetime import date, datetime, timezone

import pytest

from models.order import Order, OrderItem
from services.exchange_return_service import CancellationService, ExchangeService, InvalidStatusError, ReturnService
from services.inventory_service import InventoryService


def _make_order(db_session, platform, product_option, quantity=2, status="DELIVERED", order_no=None):
    order = Order(
        platform_id=platform.id,
        platform_order_no=order_no or f"ER-TEST-{status}-{quantity}",
        status=status,
        order_date=datetime.now(timezone.utc),
        total_amount=10000,
        discount_amount=0,
    )
    db_session.add(order)
    db_session.flush()
    item = OrderItem(
        order_id=order.id,
        product_option_id=product_option.id,
        quantity=quantity,
        unit_price=5000,
        cost_price_snapshot=2000,
        line_amount=5000 * quantity,
    )
    db_session.add(item)
    db_session.flush()
    return order, item


class TestExchangeService:
    def test_create_starts_requested(self, db_session, platform, product_option):
        order, item = _make_order(db_session, platform, product_option)

        exchange = ExchangeService(db_session).create(order.id, item.id, "사이즈 교환")

        assert exchange.status == "REQUESTED"
        assert exchange.order_item_id == item.id

    def test_completed_syncs_order_status(self, db_session, platform, product_option):
        order, item = _make_order(db_session, platform, product_option)
        service = ExchangeService(db_session)
        exchange = service.create(order.id, item.id, "사이즈 교환")

        service.change_status(exchange, "COMPLETED")

        db_session.refresh(order)
        assert order.status == "EXCHANGED"
        assert exchange.completed_at is not None

    def test_rejected_does_not_change_order_status(self, db_session, platform, product_option):
        order, item = _make_order(db_session, platform, product_option)
        service = ExchangeService(db_session)
        exchange = service.create(order.id, item.id, "사이즈 교환")

        service.change_status(exchange, "REJECTED")

        db_session.refresh(order)
        assert order.status == "DELIVERED"
        assert exchange.completed_at is not None

    def test_invalid_status_raises(self, db_session, platform, product_option):
        order, item = _make_order(db_session, platform, product_option)
        service = ExchangeService(db_session)
        exchange = service.create(order.id, item.id, "사이즈 교환")

        with pytest.raises(InvalidStatusError):
            service.change_status(exchange, "UNKNOWN")

    def test_list_filters_by_date_range(self, db_session, platform, product_option):
        order, item = _make_order(db_session, platform, product_option, order_no="ER-DATE-1")
        service = ExchangeService(db_session)
        exchange = service.create(order.id, item.id, "사이즈 교환")
        exchange.requested_at = datetime(2026, 1, 5, tzinfo=timezone.utc)
        db_session.flush()

        in_range, total_in = service.list(start_date=date(2026, 1, 1), end_date=date(2026, 1, 10))
        out_of_range, total_out = service.list(start_date=date(2026, 2, 1), end_date=date(2026, 2, 10))

        assert total_in == 1 and len(in_range) == 1
        assert total_out == 0 and len(out_of_range) == 0


class TestReturnService:
    def test_received_does_not_restock_before_inspection(
        self, db_session, platform, warehouse, product_option, inventory_row
    ):
        """정책 5: RECEIVED는 회수 도착 기록일 뿐 재고를 늘리지 않는다.

        검수를 통과해야 비로소 판매가능 재고가 된다(InventoryService.inspect_return()).
        """
        order, item = _make_order(db_session, platform, product_option, quantity=3)
        service = ReturnService(db_session)
        ret = service.create(order.id, item.id, "상품 불량", refund_amount=15000)

        service.change_status(ret, "RECEIVED", warehouse_id=warehouse.id)

        db_session.refresh(inventory_row)
        assert ret.status == "RECEIVED"
        assert inventory_row.sellable_stock == 100  # 변화 없음
        assert inventory_row.defective_stock == 0

    def test_received_without_warehouse_id_does_not_restock(
        self, db_session, platform, warehouse, product_option, inventory_row
    ):
        order, item = _make_order(db_session, platform, product_option, quantity=3)
        service = ReturnService(db_session)
        ret = service.create(order.id, item.id, "상품 불량", refund_amount=15000)

        service.change_status(ret, "RECEIVED", warehouse_id=None)

        db_session.refresh(inventory_row)
        assert inventory_row.sellable_stock == 100

    def test_refunded_syncs_order_status(self, db_session, platform, warehouse, product_option, inventory_row):
        order, item = _make_order(db_session, platform, product_option, quantity=1)
        service = ReturnService(db_session)
        ret = service.create(order.id, item.id, "상품 불량", refund_amount=5000)
        service.change_status(ret, "RECEIVED", warehouse_id=warehouse.id)

        service.change_status(ret, "REFUNDED", warehouse_id=warehouse.id)

        db_session.refresh(order)
        assert order.status == "REFUNDED"
        assert ret.completed_at is not None

    def test_received_without_inventory_record_succeeds(self, db_session, platform, warehouse, product_option):
        """Inventory 레코드가 없어도 RECEIVED는 성공한다.

        정책 5 이전에는 RECEIVED가 즉시 재고를 복원했기 때문에 재고 레코드가
        없으면 InventoryNotTrackedError를 던져야 했다. 이제 RECEIVED는 재고를
        건드리지 않으므로, 재고 레코드 존재 여부는 회수 도착 기록을 막지 않는다.
        재고 레코드 유무는 검수 시점에 InventoryService가 판단한다.
        """
        order, item = _make_order(db_session, platform, product_option, quantity=1)
        service = ReturnService(db_session)
        ret = service.create(order.id, item.id, "상품 불량", refund_amount=5000)

        service.change_status(ret, "RECEIVED", warehouse_id=warehouse.id)

        assert ret.status == "RECEIVED"

    def test_whole_order_return_does_not_restock_any_item(
        self, db_session, platform, warehouse, product_option, inventory_row
    ):
        """주문 전체 반품도 마찬가지로 검수 전에는 재고를 늘리지 않는다."""
        order, item1 = _make_order(db_session, platform, product_option, quantity=2, order_no="ER-WHOLE-1")
        item2 = OrderItem(
            order_id=order.id,
            product_option_id=product_option.id,
            quantity=1,
            unit_price=5000,
            cost_price_snapshot=2000,
            line_amount=5000,
        )
        db_session.add(item2)
        db_session.flush()

        service = ReturnService(db_session)
        ret = service.create(order.id, order_item_id=None, reason="주문 전체 반품", refund_amount=15000)

        service.change_status(ret, "RECEIVED", warehouse_id=warehouse.id)

        db_session.refresh(inventory_row)
        assert inventory_row.sellable_stock == 100  # 검수 전이므로 그대로


class TestCancellationService:
    def test_completed_releases_reservation_for_unshipped_order(
        self, db_session, platform, warehouse, product_option, inventory_row
    ):
        order, item = _make_order(db_session, platform, product_option, quantity=2, status="NEW")
        InventoryService(db_session).reserve(product_option.id, warehouse.id, 2)

        service = CancellationService(db_session)
        cancellation = service.create(order.id, "단순 변심", refund_amount=10000)
        service.change_status(cancellation, "COMPLETED", warehouse_id=warehouse.id)

        db_session.refresh(order)
        db_session.refresh(inventory_row)
        assert order.status == "CANCELED"
        assert inventory_row.reserved_stock == 0

    def test_invalid_status_raises(self, db_session, platform, product_option):
        order, item = _make_order(db_session, platform, product_option, status="NEW")
        service = CancellationService(db_session)
        cancellation = service.create(order.id, "단순 변심", refund_amount=10000)

        with pytest.raises(InvalidStatusError):
            service.change_status(cancellation, "UNKNOWN")

    def test_list_paginates(self, db_session, platform, product_option):
        service = CancellationService(db_session)
        for i in range(3):
            order, _ = _make_order(db_session, platform, product_option, status="NEW", order_no=f"CANCEL-LIST-{i}")
            service.create(order.id, "단순 변심", refund_amount=1000)

        items, total = service.list(page=2, page_size=2)

        assert total == 3
        assert len(items) == 1
