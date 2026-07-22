"""
tests/unit/test_search_service.py
----------------------------------------
SearchService(글로벌 통합검색) 단위 테스트. UI 와이어프레임 v1.1 1장 대응.
"""

from datetime import datetime, timezone

from models.order import Order, Shipment, ShipmentItem
from services.search_service import SearchService


def _make_order(db_session, platform, customer, order_no):
    order = Order(
        platform_id=platform.id,
        platform_order_no=order_no,
        customer_id=customer.id,
        status="NEW",
        order_date=datetime.now(timezone.utc),
        total_amount=10000,
        discount_amount=0,
    )
    db_session.add(order)
    db_session.flush()
    return order


class TestSearch:
    def test_empty_keyword_returns_all_empty_lists(self, db_session, platform):
        results = SearchService(db_session).search("   ")

        assert results.orders == []
        assert results.products == []
        assert results.product_options == []
        assert results.customers == []
        assert results.shipments == []

    def test_finds_matches_across_all_categories(self, db_session, platform, customer, product_option):
        order = _make_order(db_session, platform, customer, "SEARCH-ORDER-999")
        shipment = Shipment(carrier="CJ", tracking_no="SEARCH-TRACK-999", status="READY")
        db_session.add(shipment)
        db_session.flush()
        db_session.add(ShipmentItem(shipment_id=shipment.id, order_id=order.id))
        db_session.flush()

        results = SearchService(db_session).search("SEARCH")

        assert any(o.label == "SEARCH-ORDER-999" for o in results.orders)
        assert any(s.label == "SEARCH-TRACK-999" for s in results.shipments)

    def test_product_search_matches_name(self, db_session, product_option):
        results = SearchService(db_session).search("테스트 상품")

        assert len(results.products) == 1
        assert results.products[0].label == "테스트 상품"

    def test_product_option_search_matches_sku(self, db_session, product_option):
        results = SearchService(db_session).search("TEST-SKU")

        assert len(results.product_options) == 1
        assert results.product_options[0].label == "TEST-SKU-001"

    def test_customer_search_matches_name(self, db_session, customer):
        results = SearchService(db_session).search("홍길동")

        assert len(results.customers) == 1
        assert results.customers[0].label == "홍길동"

    def test_no_match_returns_empty_category(self, db_session, platform):
        results = SearchService(db_session).search("존재하지않는검색어")

        assert results.orders == []
        assert results.customers == []
