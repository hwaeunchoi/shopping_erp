"""
tests/unit/test_shipment_service.py
----------------------------------------
ShipmentService 단위 테스트.
"""

from datetime import datetime, timezone

import pytest

from models.order import Order, OrderItem
from services.shipment_service import InvalidShipmentStatusError, ShipmentAlreadyExistsError, ShipmentService


def _make_order(db_session, platform, product_option, quantity=2, status="NEW", order_no=None):
    order = Order(
        platform_id=platform.id,
        platform_order_no=order_no or f"SHIP-TEST-{status}-{quantity}",
        status=status,
        order_date=datetime.now(timezone.utc),
        total_amount=10000,
        discount_amount=0,
    )
    db_session.add(order)
    db_session.flush()
    db_session.add(
        OrderItem(
            order_id=order.id,
            product_option_id=product_option.id,
            quantity=quantity,
            unit_price=5000,
            cost_price_snapshot=2000,
            line_amount=5000 * quantity,
        )
    )
    db_session.flush()
    return order


class TestCreate:
    def test_creates_shipment_with_ready_status(self, db_session, platform, product_option):
        order = _make_order(db_session, platform, product_option)

        service = ShipmentService(db_session)
        shipment = service.create(order.id, carrier="CJ대한통운", tracking_no="TRK-1")

        # 배송↔주문은 shipment_items로 연결된다(합포장/분할배송 지원).
        assert service.shipment_repo.list_orders_of_shipment(shipment.id) == [order.id]
        assert shipment.status == "READY"
        assert shipment.carrier == "CJ대한통운"

    def test_raises_when_shipment_already_exists(self, db_session, platform, product_option):
        order = _make_order(db_session, platform, product_option)
        service = ShipmentService(db_session)
        service.create(order.id, carrier="CJ대한통운", tracking_no="TRK-1")

        with pytest.raises(ShipmentAlreadyExistsError):
            service.create(order.id, carrier="롯데택배", tracking_no="TRK-2")


class TestConsolidate:
    """합포장 - 같은 수취인의 주문 여러 건을 송장 1장으로."""

    def test_consolidates_multiple_orders_into_one_shipment(self, db_session, platform, product_option):
        o1 = _make_order(db_session, platform, product_option, order_no="CONSOL-1")
        o2 = _make_order(db_session, platform, product_option, order_no="CONSOL-2")
        o3 = _make_order(db_session, platform, product_option, order_no="CONSOL-3")
        service = ShipmentService(db_session)

        shipment = service.consolidate([o1.id, o2.id, o3.id], carrier="CJ대한통운", tracking_no="BOX-1")

        linked = service.shipment_repo.list_orders_of_shipment(shipment.id)
        assert sorted(linked) == sorted([o1.id, o2.id, o3.id])
        # 주문 3건 모두 같은 송장 1장을 바라본다
        for o in (o1, o2, o3):
            linked_shipment = service.shipment_repo.get_by_order(o.id)
            assert linked_shipment is not None
            assert linked_shipment.id == shipment.id

    def test_consolidate_rejects_order_that_already_shipped(self, db_session, platform, product_option):
        o1 = _make_order(db_session, platform, product_option, order_no="CONSOL-DUP-1")
        o2 = _make_order(db_session, platform, product_option, order_no="CONSOL-DUP-2")
        service = ShipmentService(db_session)
        service.create(o1.id, carrier="CJ대한통운", tracking_no="TRK-X")

        with pytest.raises(ShipmentAlreadyExistsError):
            service.consolidate([o1.id, o2.id], carrier="CJ대한통운", tracking_no="BOX-2")

    def test_consolidated_status_change_syncs_all_orders(self, db_session, platform, product_option, warehouse):
        o1 = _make_order(db_session, platform, product_option, order_no="CONSOL-ST-1")
        o2 = _make_order(db_session, platform, product_option, order_no="CONSOL-ST-2")
        service = ShipmentService(db_session)
        shipment = service.consolidate([o1.id, o2.id], carrier="CJ대한통운", tracking_no="BOX-3")

        service.change_status(shipment, "SHIPPING", warehouse_id=warehouse.id)

        # 합포장된 주문 전부가 함께 SHIPPING으로 동기화되어야 한다
        assert o1.status == "SHIPPING"
        assert o2.status == "SHIPPING"


class TestSplitShipment:
    """분할배송 - 주문 1건을 여러 송장으로 나눠 발송."""

    def test_allow_split_creates_second_shipment_for_same_order(self, db_session, platform, product_option):
        order = _make_order(db_session, platform, product_option, order_no="SPLIT-1")
        service = ShipmentService(db_session)
        first = service.create(order.id, carrier="CJ대한통운", tracking_no="SPLIT-A")

        second = service.create(order.id, carrier="CJ대한통운", tracking_no="SPLIT-B", allow_split=True)

        assert first.id != second.id
        shipments = service.shipment_repo.list_by_order(order.id)
        assert len(shipments) == 2
        assert {s.tracking_no for s in shipments} == {"SPLIT-A", "SPLIT-B"}


class TestUpdateInfo:
    def test_updates_carrier_and_tracking_no(self, db_session, platform, product_option):
        order = _make_order(db_session, platform, product_option)
        service = ShipmentService(db_session)
        shipment = service.create(order.id, carrier="CJ대한통운", tracking_no="TRK-1")

        updated = service.update_info(shipment, carrier="롯데택배", tracking_no="TRK-9")

        assert updated.carrier == "롯데택배"
        assert updated.tracking_no == "TRK-9"


class TestChangeStatus:
    def test_shipping_deducts_inventory_and_syncs_order_status(
        self, db_session, platform, warehouse, product_option, inventory_row
    ):
        order = _make_order(db_session, platform, product_option, quantity=2)
        service = ShipmentService(db_session)
        shipment = service.create(order.id, carrier="CJ대한통운", tracking_no="TRK-1")

        service.change_status(shipment, "SHIPPING", warehouse_id=warehouse.id)

        db_session.refresh(order)
        db_session.refresh(inventory_row)
        assert order.status == "SHIPPING"
        assert shipment.shipped_at is not None
        assert inventory_row.sellable_stock == 98  # 100 - 2

    def test_delivered_sets_timestamp_and_order_status(
        self, db_session, platform, warehouse, product_option, inventory_row
    ):
        order = _make_order(db_session, platform, product_option, quantity=1)
        service = ShipmentService(db_session)
        shipment = service.create(order.id, carrier="CJ대한통운", tracking_no="TRK-1")
        service.change_status(shipment, "SHIPPING", warehouse_id=warehouse.id)

        service.change_status(shipment, "DELIVERED", warehouse_id=warehouse.id)

        db_session.refresh(order)
        assert order.status == "DELIVERED"
        assert shipment.delivered_at is not None
        assert order.delivery_completed_date is not None

    def test_invalid_status_raises(self, db_session, platform, product_option):
        order = _make_order(db_session, platform, product_option)
        service = ShipmentService(db_session)
        shipment = service.create(order.id, carrier="CJ대한통운", tracking_no="TRK-1")

        with pytest.raises(InvalidShipmentStatusError):
            service.change_status(shipment, "UNKNOWN")

    def test_without_warehouse_id_skips_inventory_but_still_updates_status(
        self, db_session, platform, product_option, inventory_row
    ):
        order = _make_order(db_session, platform, product_option, quantity=2)
        service = ShipmentService(db_session)
        shipment = service.create(order.id, carrier="CJ대한통운", tracking_no="TRK-1")

        service.change_status(shipment, "SHIPPING", warehouse_id=None)

        db_session.refresh(order)
        db_session.refresh(inventory_row)
        assert order.status == "SHIPPING"
        assert inventory_row.sellable_stock == 100  # 반영 안 됨(경고 로그만)


class TestList:
    def test_filters_by_status_and_paginates(self, db_session, platform, product_option):
        service = ShipmentService(db_session)
        for i in range(3):
            order = _make_order(db_session, platform, product_option, quantity=1, order_no=f"SHIP-LIST-{i}")
            service.create(order.id, carrier="CJ대한통운", tracking_no=f"TRK-{i}")

        items, total = service.list(page=1, page_size=2)

        assert total == 3
        assert len(items) == 2
