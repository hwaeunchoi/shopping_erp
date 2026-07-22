"""
tests/unit/test_order_rate_service.py
--------------------------------------------
OrderRateService 단위 테스트. SRS FR-ORD-04(교환율/반품율/취소율).
"""

from datetime import date, datetime, timezone

from models.order import Order
from services.exchange_return_service import CancellationService, ExchangeService, OrderRateService, ReturnService


def _make_order(db_session, platform, customer, order_date, order_no):
    order = Order(
        platform_id=platform.id,
        platform_order_no=order_no,
        customer_id=customer.id,
        status="DELIVERED",
        order_date=order_date,
        total_amount=10000,
        discount_amount=0,
    )
    db_session.add(order)
    db_session.flush()
    return order


class TestCalculateRates:
    def test_returns_zero_rates_when_no_orders(self, db_session):
        result = OrderRateService(db_session).calculate_rates(date(2026, 1, 1), date(2026, 1, 31))

        assert result == {
            "order_count": 0,
            "exchange_count": 0,
            "exchange_rate": 0.0,
            "return_count": 0,
            "return_rate": 0.0,
            "cancellation_count": 0,
            "cancellation_rate": 0.0,
        }

    def test_calculates_rates_against_order_count(self, db_session, platform, customer):
        target = date(2026, 2, 10)
        order_date = datetime(2026, 2, 10, 10, 0, tzinfo=timezone.utc)
        orders = [_make_order(db_session, platform, customer, order_date, f"RATE-{i}") for i in range(4)]

        exchange = ExchangeService(db_session).create(orders[0].id, None, "사이즈 교환")
        ret = ReturnService(db_session).create(orders[1].id, None, "상품 불량", None)
        cancellation = CancellationService(db_session).create(orders[2].id, "단순 변심", None)
        # 신청일(requested_at)을 집계 대상 기간 안으로 맞춘다(기본값은 now()라 테스트 실행 시각이 됨).
        exchange.requested_at = order_date
        ret.requested_at = order_date
        cancellation.requested_at = order_date
        db_session.flush()

        result = OrderRateService(db_session).calculate_rates(target, date(2026, 2, 11))

        assert result["order_count"] == 4
        assert result["exchange_count"] == 1
        assert result["exchange_rate"] == 25.0
        assert result["return_count"] == 1
        assert result["return_rate"] == 25.0
        assert result["cancellation_count"] == 1
        assert result["cancellation_rate"] == 25.0

    def test_without_date_range_covers_all_orders(self, db_session, platform, customer):
        _make_order(db_session, platform, customer, datetime(2020, 1, 1, tzinfo=timezone.utc), "RATE-ALL-1")
        _make_order(db_session, platform, customer, datetime(2030, 1, 1, tzinfo=timezone.utc), "RATE-ALL-2")

        result = OrderRateService(db_session).calculate_rates()

        assert result["order_count"] == 2


class TestPendingAlerts:
    def test_returns_zero_when_nothing_pending(self, db_session):
        result = OrderRateService(db_session).pending_alerts()

        assert result == {
            "unshipped_count": 0,
            "delayed_unshipped_count": 0,
            "exchange_pending_count": 0,
            "return_pending_count": 0,
            "cancellation_pending_count": 0,
        }

    def test_counts_unshipped_orders_and_pending_requests(self, db_session, platform, customer):
        order_date = datetime(2026, 3, 1, tzinfo=timezone.utc)
        new_order = _make_order(db_session, platform, customer, order_date, "ALERT-NEW")
        new_order.status = "NEW"
        preparing_order = _make_order(db_session, platform, customer, order_date, "ALERT-PREP")
        preparing_order.status = "PREPARING"
        delivered_order = _make_order(db_session, platform, customer, order_date, "ALERT-DELIVERED")
        db_session.flush()

        ExchangeService(db_session).create(delivered_order.id, None, "교환 사유")
        ReturnService(db_session).create(delivered_order.id, None, "반품 사유", None)
        cancellation = CancellationService(db_session).create(new_order.id, "취소 사유", None)
        # 승인된(REQUESTED가 아닌) 건은 "대기" 집계에서 빠져야 한다.
        approved_return = ReturnService(db_session).create(delivered_order.id, None, "승인된 반품", None)
        approved_return.status = "APPROVED"
        db_session.flush()

        result = OrderRateService(db_session).pending_alerts()

        assert result["unshipped_count"] == 2  # NEW 1건 + PREPARING 1건
        assert result["delayed_unshipped_count"] == 2  # order_date가 2026-03-01로 임계값(2일)을 훨씬 지남
        assert result["exchange_pending_count"] == 1
        assert result["return_pending_count"] == 1  # APPROVED로 바뀐 건은 제외
        assert result["cancellation_pending_count"] == 1
        assert cancellation.status == "REQUESTED"


class TestDelayedUnshipped:
    def test_recent_unshipped_order_is_not_delayed(self, db_session, platform, customer):
        from datetime import timedelta

        recent_order = _make_order(
            db_session,
            platform,
            customer,
            datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1),
            "RECENT-1",
        )
        recent_order.status = "NEW"
        db_session.flush()

        result = OrderRateService(db_session).pending_alerts()

        assert result["unshipped_count"] == 1
        assert result["delayed_unshipped_count"] == 0
