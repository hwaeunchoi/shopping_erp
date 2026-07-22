"""
tests/unit/test_customer_stats_service.py
--------------------------------------------------
CustomerStatsService 단위 테스트. models/customer.py 설계 주석
("주문 확정/취소 시 서비스 레이어가 갱신한다")의 실제 구현을 검증한다.
"""

from datetime import datetime, timedelta, timezone

import pytest

from config.settings import settings
from models.order import Order
from services.customer_stats_service import CustomerStatsService


def _order(db_session, platform, customer, amount, status, days_ago):
    order_date = datetime.now(timezone.utc) - timedelta(days=days_ago)
    order = Order(
        platform_id=platform.id,
        platform_order_no=f"O-{customer.id}-{days_ago}-{status}",
        customer_id=customer.id,
        status=status,
        order_date=order_date,
        payment_date=order_date,
        total_amount=amount,
        discount_amount=0,
    )
    db_session.add(order)
    db_session.flush()
    return order


class TestRefresh:
    def test_excludes_canceled_orders_from_totals(self, db_session, platform, customer):
        _order(db_session, platform, customer, 10000, "DELIVERED", days_ago=5)
        _order(db_session, platform, customer, 999999, "CANCELED", days_ago=3)

        result = CustomerStatsService(db_session).refresh(customer.id)

        assert result.total_purchase_amount == 10000
        assert result.order_count == 1

    def test_grade_and_vip_thresholds(self, db_session, platform, customer):
        _order(db_session, platform, customer, 600000, "DELIVERED", days_ago=1)

        result = CustomerStatsService(db_session).refresh(customer.id)

        assert result.grade == "VIP"
        assert result.is_vip is True

    def test_premium_grade_below_vip_threshold(self, db_session, platform, customer):
        _order(db_session, platform, customer, 250000, "DELIVERED", days_ago=1)

        result = CustomerStatsService(db_session).refresh(customer.id)

        assert result.grade == "우수"
        assert result.is_vip is False

    def test_dormant_flag_uses_configured_threshold(self, db_session, platform, customer):
        _order(db_session, platform, customer, 10000, "DELIVERED", days_ago=settings.dormant_customer_days + 5)

        result = CustomerStatsService(db_session).refresh(customer.id)

        assert result.is_dormant is True

    def test_recent_order_is_not_dormant(self, db_session, platform, customer):
        _order(db_session, platform, customer, 10000, "DELIVERED", days_ago=1)

        result = CustomerStatsService(db_session).refresh(customer.id)

        assert result.is_dormant is False

    def test_customer_without_orders_stays_at_defaults(self, db_session, customer):
        result = CustomerStatsService(db_session).refresh(customer.id)

        assert result.order_count == 0
        assert result.total_purchase_amount == 0
        assert result.is_dormant is False
        assert result.grade == "일반"

    def test_missing_customer_raises_value_error(self, db_session):
        with pytest.raises(ValueError):
            CustomerStatsService(db_session).refresh(999999)
