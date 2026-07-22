"""
tests/unit/test_profit_calculation_service.py
--------------------------------------------------
ProfitCalculationService 단위 테스트.
"""

from datetime import date, datetime, timezone

from models.analytics import ProfitLossSummary
from models.cost import Cost
from models.order import Order, OrderItem
from models.platform import Platform
from services.profit_calculation_service import ProfitCalculationService


def _make_order(db_session, platform, customer, order_date, total_amount, status="DELIVERED"):
    order = Order(
        platform_id=platform.id,
        platform_order_no=f"TEST-{order_date.isoformat()}-{total_amount}",
        customer_id=customer.id,
        status=status,
        order_date=order_date,
        payment_date=order_date,
        total_amount=total_amount,
        discount_amount=0,
    )
    db_session.add(order)
    db_session.flush()
    return order


class TestCalculateDaily:
    def test_no_orders_returns_zeroed_summary(self, db_session, platform):
        service = ProfitCalculationService(db_session)

        summary = service.calculate_daily(date(2026, 1, 1))

        assert summary.period_type == "DAILY"
        assert summary.basis_type == "ORDER_DATE"
        assert summary.period_key == "2026-01-01"
        assert summary.order_count == 0
        assert summary.net_revenue == 0
        assert summary.net_profit == 0

    def test_aggregates_orders_costs_and_fee(self, db_session, platform, platform_fee_rule, customer, product_option):
        target_date = date(2026, 1, 5)
        order_date = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)
        order = _make_order(db_session, platform, customer, order_date, total_amount=50000)
        db_session.add(
            OrderItem(
                order_id=order.id,
                product_option_id=product_option.id,
                quantity=2,
                unit_price=25000,
                cost_price_snapshot=10000,
                line_amount=50000,
            )
        )
        db_session.add(
            Cost(
                category="SHIPPING", cost_type="VARIABLE", amount=3000, incurred_date=target_date, created_at=order_date
            )
        )
        db_session.flush()

        summary = ProfitCalculationService(db_session).calculate_daily(target_date)

        assert summary.order_count == 1
        assert summary.net_revenue == 50000
        assert summary.cost_of_goods == 20000  # 10000 * 2
        assert summary.shipping_cost == 3000
        assert summary.platform_fee == 5000  # platform_fee_rules에 시딩된 10% * 50000
        expected_total_cost = 20000 + 5000 + 3000
        assert summary.total_cost == expected_total_cost
        assert summary.net_profit == 50000 - expected_total_cost

    def test_no_fee_rule_falls_back_to_zero_percent(self, db_session, platform, customer):
        """platform_fee_rules에 규칙이 없으면(시딩 안 된 상태) 0% 기본값을 적용하고 계산은 계속 진행된다."""
        target_date = date(2026, 1, 10)
        order_date = datetime(2026, 1, 10, 10, 0, tzinfo=timezone.utc)
        _make_order(db_session, platform, customer, order_date, total_amount=50000)

        summary = ProfitCalculationService(db_session).calculate_daily(target_date)

        assert summary.order_count == 1
        assert summary.platform_fee == 0

    def test_canceled_and_refunded_orders_excluded_from_revenue(self, db_session, platform, customer):
        """FR-PROFIT-03: 취소/반품/환불 주문은 매출/주문건수 집계에서 제외된다."""
        target_date = date(2026, 1, 20)
        order_date = datetime(2026, 1, 20, 10, 0, tzinfo=timezone.utc)
        _make_order(db_session, platform, customer, order_date, 10000, status="DELIVERED")
        _make_order(db_session, platform, customer, order_date, 20000, status="CANCELED")
        _make_order(db_session, platform, customer, order_date, 30000, status="RETURNED")
        _make_order(db_session, platform, customer, order_date, 40000, status="REFUNDED")

        summary = ProfitCalculationService(db_session).calculate_daily(target_date)

        assert summary.order_count == 1
        assert summary.net_revenue == 10000

    def test_exchanged_orders_are_not_excluded_from_revenue(self, db_session, platform, customer):
        """교환(EXCHANGED)은 매출이 유지되는 거래이므로 집계에서 제외하지 않는다."""
        target_date = date(2026, 1, 21)
        order_date = datetime(2026, 1, 21, 10, 0, tzinfo=timezone.utc)
        _make_order(db_session, platform, customer, order_date, 15000, status="EXCHANGED")

        summary = ProfitCalculationService(db_session).calculate_daily(target_date)

        assert summary.order_count == 1
        assert summary.net_revenue == 15000

    def test_orders_outside_target_date_are_excluded(self, db_session, platform, customer):
        _make_order(db_session, platform, customer, datetime(2026, 1, 4, 23, 59, tzinfo=timezone.utc), 10000)
        _make_order(db_session, platform, customer, datetime(2026, 1, 6, 0, 0, tzinfo=timezone.utc), 10000)

        summary = ProfitCalculationService(db_session).calculate_daily(date(2026, 1, 5))

        assert summary.order_count == 0

    def test_upsert_updates_existing_row_without_duplicate(self, db_session, platform):
        service = ProfitCalculationService(db_session)
        target = date(2026, 2, 1)

        first = service.calculate_daily(target)
        second = service.calculate_daily(target)

        assert second.id == first.id
        count = db_session.query(ProfitLossSummary).filter(ProfitLossSummary.period_key == "2026-02-01").count()
        assert count == 1


class TestCalculateDailyRange:
    def test_creates_one_summary_per_day_in_half_open_range(self, db_session, platform):
        summaries = ProfitCalculationService(db_session).calculate_daily_range(date(2026, 3, 1), date(2026, 3, 4))

        assert [s.period_key for s in summaries] == ["2026-03-01", "2026-03-02", "2026-03-03"]


class TestCalculateMonthly:
    """SRS FR-REPORT-03: 월별 손익 계산이 일별 합산으로 정확히 이루어지는지 검증."""

    def test_sums_daily_summaries_into_monthly_row(self, db_session, platform, customer):
        _make_order(db_session, platform, customer, datetime(2026, 4, 5, 10, 0, tzinfo=timezone.utc), 10000)
        _make_order(db_session, platform, customer, datetime(2026, 4, 20, 10, 0, tzinfo=timezone.utc), 30000)

        summary = ProfitCalculationService(db_session).calculate_monthly(2026, 4)

        assert summary.period_type == "MONTHLY"
        assert summary.period_key == "2026-04"
        assert summary.order_count == 2
        assert summary.net_revenue == 40000

    def test_upsert_updates_existing_monthly_row_without_duplicate(self, db_session, platform):
        service = ProfitCalculationService(db_session)

        first = service.calculate_monthly(2026, 5)
        second = service.calculate_monthly(2026, 5)

        assert second.id == first.id
        count = (
            db_session.query(ProfitLossSummary)
            .filter(ProfitLossSummary.period_key == "2026-05", ProfitLossSummary.period_type == "MONTHLY")
            .count()
        )
        assert count == 1

    def test_february_uses_correct_day_count(self, db_session, platform, customer):
        """윤년이 아닌 2월(28일)의 마지막 날 주문도 월별 집계에 포함되는지 확인한다."""
        _make_order(db_session, platform, customer, datetime(2026, 2, 28, 23, 0, tzinfo=timezone.utc), 5000)

        summary = ProfitCalculationService(db_session).calculate_monthly(2026, 2)

        assert summary.order_count == 1
        assert summary.net_revenue == 5000


class TestCalculateByPlatform:
    """SRS FR-PROFIT-02: 플랫폼 단위로 세분화한 손익 조회가 전체 집계와 별도 행으로 저장되는지 검증."""

    def test_calculate_daily_with_platform_id_only_includes_that_platform(self, db_session, platform, customer):
        other_platform = Platform(
            code="naver_smartstore2",
            name="테스트 플랫폼2",
            connector_class="X",
            settlement_cycle_days=15,
            is_active=True,
        )
        db_session.add(other_platform)
        db_session.flush()
        target_date = date(2026, 6, 1)
        order_date = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
        _make_order(db_session, platform, customer, order_date, 10000)

        other_customer = customer.__class__(
            platform_id=other_platform.id, platform_customer_key="OTHER-CUST", name="다른플랫폼고객"
        )
        db_session.add(other_customer)
        db_session.flush()
        _make_order(db_session, other_platform, other_customer, order_date, 99000)

        service = ProfitCalculationService(db_session)
        platform_summary = service.calculate_daily(target_date, platform_id=platform.id)

        assert platform_summary.platform_id == platform.id
        assert platform_summary.order_count == 1
        assert platform_summary.net_revenue == 10000

    def test_calculate_daily_with_platform_id_does_not_affect_overall_row(self, db_session, platform, customer):
        target_date = date(2026, 6, 2)
        order_date = datetime(2026, 6, 2, 10, 0, tzinfo=timezone.utc)
        _make_order(db_session, platform, customer, order_date, 10000)
        service = ProfitCalculationService(db_session)

        overall_before = service.calculate_daily(target_date)
        service.calculate_daily(target_date, platform_id=platform.id)
        overall_after = service.calculate_daily(target_date)

        assert overall_before.id == overall_after.id
        assert overall_after.platform_id is None
        assert overall_after.order_count == 1

        count = (
            db_session.query(ProfitLossSummary)
            .filter(ProfitLossSummary.period_key == "2026-06-02", ProfitLossSummary.period_type == "DAILY")
            .count()
        )
        assert count == 2  # 전체 1행 + 플랫폼별 1행

    def test_calculate_daily_by_platform_excludes_ad_cost(self, db_session, platform, customer):
        """광고비는 쇼핑몰 플랫폼에 귀속시킬 근거가 없어 플랫폼별 집계에서는 0으로 둔다."""
        target_date = date(2026, 6, 3)
        order_date = datetime(2026, 6, 3, 10, 0, tzinfo=timezone.utc)
        _make_order(db_session, platform, customer, order_date, 10000)

        summary = ProfitCalculationService(db_session).calculate_daily(target_date, platform_id=platform.id)

        assert summary.ad_cost == 0

    def test_calculate_monthly_with_platform_id_creates_separate_row(self, db_session, platform, customer):
        _make_order(db_session, platform, customer, datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc), 20000)

        summary = ProfitCalculationService(db_session).calculate_monthly(2026, 6, platform_id=platform.id)

        assert summary.platform_id == platform.id
        assert summary.period_type == "MONTHLY"
        assert summary.order_count == 1
        assert summary.net_revenue == 20000
