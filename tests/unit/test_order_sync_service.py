"""
tests/unit/test_order_sync_service.py
-------------------------------------------
OrderSyncService 단위 테스트. 실제 더미 커넥터 대신, 반환값을 완전히
통제할 수 있는 스텁 커넥터를 사용해 신규생성/중복무시/상태변경(재고
차감·예약해제)/매핑없는 상품 스킵 네 가지 핵심 흐름을 검증한다.

상품 자동등록/자동매칭 로직 자체(4단계 cascade, 네이버 상품 API 동기화)는
services/product_sync_service.py로 분리되어 tests/unit/test_product_sync_service.py
에서 검증한다 - 여기서는 OrderSyncService가 그 결과를 올바르게 반영하고,
매핑이 없으면 절대 새 상품을 만들지 않는다는 점만 확인한다.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import pytest

from integrations.malls.base_mall_connector import BaseMallConnector
from models.order import Order, OrderItem, OrderStatusHistory
from models.product import Product, ProductCostHistory, ProductPlatformMap
from models.system import SystemLog
from services.order_sync_service import OrderSyncService


class StubConnector(BaseMallConnector):
    """OrderSyncService 검증용 스텁. fetch_orders()만 실제로 값을 통제하고,
    나머지 추상 메서드는 이 테스트에서 쓰지 않아 최소 구현만 둔다."""

    platform_code = "stub"

    def __init__(self, order_no: str, platform_option_id: str, status: str, unit_price=10000, quantity=2):
        self.order_no = order_no
        self.platform_option_id = platform_option_id
        self.status = status
        self.unit_price = unit_price
        self.quantity = quantity
        # 매핑이 없을 때 ProductSyncService.match_unmapped_item()의 매칭 재료로
        # 쓰이는 부가정보(판매자상품코드/옵션명/상품명) - 기본값 None.
        self.product_name: Optional[str] = None
        self.option_name: Optional[str] = None
        self.seller_product_code: Optional[str] = None

    def fetch_orders(self, start_date, end_date) -> list[dict[str, Any]]:
        return [
            {
                "platform_order_no": self.order_no,
                "order_date": datetime.now(timezone.utc) - timedelta(hours=1),
                "status": self.status,
                "customer_key": "STUB-CUST",
                "customer_name": "테스트고객",
                "customer_phone": "010-0000-0000",
                "total_amount": self.unit_price * self.quantity,
                "discount_amount": 0.0,
                "items": [
                    {
                        "platform_option_id": self.platform_option_id,
                        "quantity": self.quantity,
                        "unit_price": self.unit_price,
                        "product_name": self.product_name,
                        "option_name": self.option_name,
                        "seller_product_code": self.seller_product_code,
                    }
                ],
            }
        ]

    def fetch_order_detail(self, platform_order_no: str) -> dict[str, Any]:
        raise NotImplementedError

    def update_shipment(self, platform_order_no: str, carrier: str, tracking_no: str) -> bool:
        raise NotImplementedError

    def fetch_settlements(self, start_date, end_date) -> list[dict[str, Any]]:
        raise NotImplementedError


class FailingConnector(BaseMallConnector):
    """fetch_orders()가 항상 예외를 던지는 스텁 - FR-LOG-01 실패 로깅 검증용."""

    platform_code = "stub-failing"

    def fetch_orders(self, start_date, end_date) -> list[dict[str, Any]]:
        raise RuntimeError("커넥터 연결 실패(테스트용)")

    def fetch_order_detail(self, platform_order_no: str) -> dict[str, Any]:
        raise NotImplementedError

    def update_shipment(self, platform_order_no: str, carrier: str, tracking_no: str) -> bool:
        raise NotImplementedError

    def fetch_settlements(self, start_date, end_date) -> list[dict[str, Any]]:
        raise NotImplementedError


SYNC_START = date.today() - timedelta(days=1)
SYNC_END = date.today() + timedelta(days=1)


class TestSyncOrdersCreatesNew:
    def test_creates_order_with_items_and_reserves_inventory(
        self, db_session, platform, warehouse, platform_map, inventory_row
    ):
        connector = StubConnector("ORD-1", platform_map.platform_option_id, status="NEW")

        result = OrderSyncService(db_session).sync_orders(connector, platform.id, warehouse.id, SYNC_START, SYNC_END)

        assert result == {"total": 1, "created": 1, "updated": 0, "skipped_items": 0, "auto_matched_products": 0}
        order = db_session.query(Order).filter_by(platform_order_no="ORD-1").one()
        assert order.status == "NEW"
        db_session.refresh(inventory_row)
        assert inventory_row.reserved_stock == 2

    def test_skips_item_when_connector_gives_no_product_code_at_all(self, db_session, platform, warehouse):
        connector = StubConnector("ORD-2", "", status="NEW")
        service = OrderSyncService(db_session)

        result = service.sync_orders(connector, platform.id, warehouse.id, SYNC_START, SYNC_END)

        assert result["created"] == 1
        assert result["skipped_items"] == 1
        assert result["auto_matched_products"] == 0
        order = db_session.query(Order).filter_by(platform_order_no="ORD-2").one()
        assert service.order_repo.list_items(order.id) == []

    def test_missing_inventory_record_does_not_crash_sync(self, db_session, platform, warehouse, platform_map):
        """실사용에서 확인된 오류(ValueError: 재고 레코드가 없습니다)로 전체 동기화가
        중단되던 문제 재발 방지 - 재고관리에 아직 등록 안 된 SKU라도(inventory_row
        픽스처를 일부러 쓰지 않음) 주문상품 자체는 정상 반영되고 동기화가 계속돼야 한다."""
        connector = StubConnector("ORD-NO-INVENTORY", platform_map.platform_option_id, status="NEW")
        service = OrderSyncService(db_session)

        result = service.sync_orders(connector, platform.id, warehouse.id, SYNC_START, SYNC_END)

        assert result == {"total": 1, "created": 1, "updated": 0, "skipped_items": 0, "auto_matched_products": 0}
        order = db_session.query(Order).filter_by(platform_order_no="ORD-NO-INVENTORY").one()
        items = service.order_repo.list_items(order.id)
        assert len(items) == 1
        assert items[0].product_option_id == platform_map.product_option_id

    def test_unmapped_item_never_creates_a_new_product(self, db_session, platform, warehouse):
        """핵심 정책: 어떤 플랫폼이든 주문 수집에서는 절대 새 상품을 만들지 않는다 -
        매핑이 없고 자동매칭도 실패하면 그냥 건너뛴다(product_sync_service의
        ProductSyncService.match_unmapped_item에 위임, 실패 시 미매칭 상품으로만 기록)."""
        products_before = db_session.query(Product).count()
        connector = StubConnector("ORD-NO-MATCH", "UNKNOWN-CODE-NO-MATCH", status="NEW")
        service = OrderSyncService(db_session)

        result = service.sync_orders(connector, platform.id, warehouse.id, SYNC_START, SYNC_END)

        assert result["skipped_items"] == 1
        assert result["auto_matched_products"] == 0
        assert db_session.query(Product).count() == products_before


class TestSyncOrdersDelegatesMatching:
    """OrderSyncService는 매핑이 없는 상품을 만나면 ProductSyncService.
    match_unmapped_item()에 위임한다 - 여기서는 위임이 실제로 동작하고 결과가
    올바르게 집계되는지만 확인한다(매칭 4단계 자체는 test_product_sync_service.py 참고)."""

    def test_matches_existing_product_by_seller_code_and_fills_order_item(
        self, db_session, platform, naver_platform, product_option, warehouse
    ):
        db_session.add(
            ProductPlatformMap(
                product_option_id=product_option.id,
                platform_id=naver_platform.id,
                platform_option_id="NAVER-ITEM-1",
                seller_product_code="SHARED-SELLER-CODE",
            )
        )
        db_session.flush()
        products_before = db_session.query(Product).count()

        connector = StubConnector("ORD-MATCH-1", "COUPANG-ITEM-1", status="NEW")
        connector.seller_product_code = "SHARED-SELLER-CODE"
        service = OrderSyncService(db_session)

        result = service.sync_orders(connector, platform.id, warehouse.id, SYNC_START, SYNC_END)

        assert result == {"total": 1, "created": 1, "updated": 0, "skipped_items": 0, "auto_matched_products": 1}
        assert db_session.query(Product).count() == products_before  # 새 상품이 생기지 않았다
        order = db_session.query(Order).filter_by(platform_order_no="ORD-MATCH-1").one()
        items = service.order_repo.list_items(order.id)
        assert len(items) == 1
        assert items[0].product_option_id == product_option.id

    def test_registering_mapping_manually_still_works_for_orders_created_before_this_feature(
        self, db_session, platform, warehouse, product_option, inventory_row
    ):
        """이 기능 추가 이전에 이미 상품 없이 수집된 주문(직접 DB에 만들어 재현)도,
        이후 상품관리에서 매핑을 등록하면 재수집 시 정상적으로 채워져야 한다."""
        order = Order(
            platform_id=platform.id,
            platform_order_no="ORD-PRE-EXISTING",
            status="NEW",
            order_date=datetime.now(timezone.utc) - timedelta(hours=2),
            total_amount=20000,
            discount_amount=0,
        )
        db_session.add(order)
        db_session.flush()

        db_session.add(
            ProductPlatformMap(
                product_option_id=product_option.id, platform_id=platform.id, platform_option_id="PRE-EXISTING-CODE"
            )
        )
        db_session.flush()

        connector = StubConnector("ORD-PRE-EXISTING", "PRE-EXISTING-CODE", status="NEW")
        service = OrderSyncService(db_session)

        result = service.sync_orders(connector, platform.id, warehouse.id, SYNC_START, SYNC_END)

        assert result["created"] == 0  # 주문 자체는 이미 있었음
        assert result["auto_matched_products"] == 0  # 이미 등록된 매핑을 그대로 사용(새로 매칭한 게 아님)
        items = service.order_repo.list_items(order.id)
        assert len(items) == 1
        assert items[0].product_option_id == product_option.id
        db_session.refresh(inventory_row)
        assert inventory_row.reserved_stock == 2

    def test_resync_does_not_duplicate_already_mapped_items(
        self, db_session, platform, warehouse, platform_map, inventory_row
    ):
        """이미 반영된 상품은 재수집해도 중복 생성되지 않는다."""
        connector = StubConnector("ORD-3", platform_map.platform_option_id, status="NEW")
        service = OrderSyncService(db_session)
        service.sync_orders(connector, platform.id, warehouse.id, SYNC_START, SYNC_END)

        result = service.sync_orders(connector, platform.id, warehouse.id, SYNC_START, SYNC_END)

        assert result == {"total": 1, "created": 0, "updated": 0, "skipped_items": 0, "auto_matched_products": 0}
        order = db_session.query(Order).filter_by(platform_order_no="ORD-3").one()
        assert len(service.order_repo.list_items(order.id)) == 1


class TestSyncOrdersDedup:
    def test_unchanged_status_is_not_recreated_or_updated(
        self, db_session, platform, warehouse, platform_map, inventory_row
    ):
        connector = StubConnector("ORD-3", platform_map.platform_option_id, status="NEW")
        service = OrderSyncService(db_session)
        service.sync_orders(connector, platform.id, warehouse.id, SYNC_START, SYNC_END)

        result = service.sync_orders(connector, platform.id, warehouse.id, SYNC_START, SYNC_END)

        assert result["created"] == 0
        assert result["updated"] == 0
        assert db_session.query(Order).filter_by(platform_order_no="ORD-3").count() == 1


class TestSyncOrdersStatusChange:
    def test_transition_to_shipped_deducts_inventory_and_logs_history(
        self, db_session, platform, warehouse, platform_map, inventory_row
    ):
        connector = StubConnector("ORD-4", platform_map.platform_option_id, status="NEW")
        service = OrderSyncService(db_session)
        service.sync_orders(connector, platform.id, warehouse.id, SYNC_START, SYNC_END)

        connector.status = "SHIPPING"
        result = service.sync_orders(connector, platform.id, warehouse.id, SYNC_START, SYNC_END)

        assert result["updated"] == 1
        order = db_session.query(Order).filter_by(platform_order_no="ORD-4").one()
        assert order.status == "SHIPPING"

        db_session.refresh(inventory_row)
        assert inventory_row.sellable_stock == 98  # 100 - 2
        assert inventory_row.reserved_stock == 0

        history = (
            db_session.query(OrderStatusHistory).filter_by(order_id=order.id).order_by(OrderStatusHistory.id).all()
        )
        assert [h.to_status for h in history] == ["NEW", "SHIPPING"]

    def test_transition_to_canceled_releases_reservation(
        self, db_session, platform, warehouse, platform_map, inventory_row
    ):
        connector = StubConnector("ORD-5", platform_map.platform_option_id, status="NEW")
        service = OrderSyncService(db_session)
        service.sync_orders(connector, platform.id, warehouse.id, SYNC_START, SYNC_END)
        db_session.refresh(inventory_row)
        assert inventory_row.reserved_stock == 2

        connector.status = "CANCELED"
        service.sync_orders(connector, platform.id, warehouse.id, SYNC_START, SYNC_END)

        db_session.refresh(inventory_row)
        assert inventory_row.reserved_stock == 0
        assert inventory_row.sellable_stock == 100  # 출고 전 취소이므로 실재고는 그대로

    def test_status_change_with_missing_inventory_record_does_not_crash(
        self, db_session, platform, warehouse, platform_map
    ):
        """실사용에서 확인된 오류 재발 방지: 상태 전이(NEW->SHIPPING) 시점에 재고
        레코드가 없어도(inventory_row 픽스처를 일부러 쓰지 않음) 예외 없이 넘어가야
        한다(apply_status_change의 try/except ValueError 가드)."""
        connector = StubConnector("ORD-NO-INV-STATUS", platform_map.platform_option_id, status="NEW")
        service = OrderSyncService(db_session)
        service.sync_orders(connector, platform.id, warehouse.id, SYNC_START, SYNC_END)

        connector.status = "SHIPPING"
        result = service.sync_orders(connector, platform.id, warehouse.id, SYNC_START, SYNC_END)

        assert result["updated"] == 1
        order = db_session.query(Order).filter_by(platform_order_no="ORD-NO-INV-STATUS").one()
        assert order.status == "SHIPPING"


class TestSyncOrdersCostSnapshot:
    def test_new_order_item_snapshots_effective_cost(
        self, db_session, platform, warehouse, platform_map, inventory_row
    ):
        db_session.add(
            ProductCostHistory(
                product_option_id=platform_map.product_option_id,
                cost_price=1234,
                effective_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
                effective_to=None,
            )
        )
        db_session.flush()
        connector = StubConnector("ORD-COST-1", platform_map.platform_option_id, status="NEW")

        OrderSyncService(db_session).sync_orders(connector, platform.id, warehouse.id, SYNC_START, SYNC_END)

        order = db_session.query(Order).filter_by(platform_order_no="ORD-COST-1").one()
        item = db_session.query(OrderItem).filter_by(order_id=order.id).one()
        assert item.cost_price_snapshot == 1234

    def test_new_order_item_without_cost_history_has_no_snapshot(
        self, db_session, platform, warehouse, platform_map, inventory_row
    ):
        connector = StubConnector("ORD-COST-2", platform_map.platform_option_id, status="NEW")

        OrderSyncService(db_session).sync_orders(connector, platform.id, warehouse.id, SYNC_START, SYNC_END)

        order = db_session.query(Order).filter_by(platform_order_no="ORD-COST-2").one()
        item = db_session.query(OrderItem).filter_by(order_id=order.id).one()
        assert item.cost_price_snapshot is None


class TestSyncOrdersLogging:
    """SRS FR-LOG-01: 주문 수집 성공/실패가 system_logs(API_COLLECT)에 남는지 검증."""

    def test_success_writes_info_log(self, db_session, platform, warehouse, platform_map, inventory_row):
        connector = StubConnector("ORD-LOG-1", platform_map.platform_option_id, status="NEW")

        OrderSyncService(db_session).sync_orders(connector, platform.id, warehouse.id, SYNC_START, SYNC_END)

        logs = db_session.query(SystemLog).filter_by(log_type="API_COLLECT").all()
        assert len(logs) == 1
        assert logs[0].level == "INFO"
        assert "SUCCESS" in logs[0].message
        assert "신규 1" in logs[0].message

    def test_failure_writes_error_log_and_reraises(self, db_session, platform, warehouse):
        connector = FailingConnector()

        with pytest.raises(RuntimeError, match="커넥터 연결 실패"):
            OrderSyncService(db_session).sync_orders(connector, platform.id, warehouse.id, SYNC_START, SYNC_END)

        logs = db_session.query(SystemLog).filter_by(log_type="API_COLLECT").all()
        assert len(logs) == 1
        assert logs[0].level == "ERROR"
        assert "FAILED" in logs[0].message
        assert "커넥터 연결 실패" in logs[0].message
        assert db_session.query(Order).count() == 0


class TestSyncOrdersCustomerStats:
    def test_new_order_updates_customer_stats(self, db_session, platform, warehouse, platform_map, inventory_row):
        connector = StubConnector("ORD-6", platform_map.platform_option_id, status="NEW")

        OrderSyncService(db_session).sync_orders(connector, platform.id, warehouse.id, SYNC_START, SYNC_END)

        order = db_session.query(Order).filter_by(platform_order_no="ORD-6").one()
        from models.customer import Customer

        stats_customer = db_session.get(Customer, order.customer_id)
        assert stats_customer.order_count == 1
        assert stats_customer.total_purchase_amount == order.total_amount


class MultiOrderStubConnector(StubConnector):
    """한 회차에 여러 주문을 반환하는 스텁 - 배치 부분 실패 검증용."""

    def __init__(self, order_nos: list[str], platform_option_id: str, status: str, quantity: int):
        super().__init__(order_nos[0], platform_option_id, status, quantity=quantity)
        self.order_nos = order_nos

    def fetch_orders(self, start_date, end_date) -> list[dict[str, Any]]:
        orders = []
        for no in self.order_nos:
            self.order_no = no
            raw = super().fetch_orders(start_date, end_date)[0]
            raw["customer_key"] = f"STUB-CUST-{no}"
            orders.append(raw)
        return orders


class TestSyncOrdersInventoryConflictDoesNotAbortBatch:
    """TD-3 회귀 - 재고 불변조건 위반이 수집 전체를 되돌리면 안 된다.

    수정 전에는 InventoryInvariantError가 sync_orders()의 최상위 except까지
    올라가 rollback을 유발했고, 그 회차에 정상 수집된 주문까지 전부 사라졌다.
    """

    def test_oversell_skips_only_that_item_and_keeps_other_orders(
        self, db_session, platform, warehouse, platform_map, inventory_row
    ):
        # 재고 100. 주문 3건 x 각 40개 = 120개 → 3번째 주문에서 예약이 재고를 넘는다.
        connector = MultiOrderStubConnector(
            ["ORD-B1", "ORD-B2", "ORD-B3"], platform_map.platform_option_id, status="NEW", quantity=40
        )

        result = OrderSyncService(db_session).sync_orders(connector, platform.id, warehouse.id, SYNC_START, SYNC_END)

        assert result["created"] == 3  # 주문은 3건 모두 수집된다
        for no in ("ORD-B1", "ORD-B2", "ORD-B3"):
            assert db_session.query(Order).filter_by(platform_order_no=no).one() is not None

        db_session.refresh(inventory_row)
        assert inventory_row.reserved_stock == 80  # 성공한 2건만 예약
        assert inventory_row.reserved_stock <= inventory_row.sellable_stock  # I1 유지
