"""
tests/unit/test_product_performance_service.py
------------------------------------------------------
ProductPerformanceService 단위 테스트. SRS FR-PRD-04(베스트/워스트 상품).
"""

from datetime import date, datetime, timezone

from models.ad import AdCampaign, AdPerformanceDaily
from models.order import Order, OrderItem
from services.product_performance_service import ProductPerformanceService


def _make_order(db_session, platform, customer, order_date, status="DELIVERED", order_no="PERF-ORDER"):
    order = Order(
        platform_id=platform.id,
        platform_order_no=order_no,
        customer_id=customer.id,
        status=status,
        order_date=order_date,
        total_amount=10000,
        discount_amount=0,
    )
    db_session.add(order)
    db_session.flush()
    return order


class TestCalculateDaily:
    def test_aggregates_sales_and_profit_per_option(self, db_session, platform, customer, product_option):
        target = date(2026, 4, 1)
        order_date = datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc)
        order = _make_order(db_session, platform, customer, order_date)
        db_session.add(
            OrderItem(
                order_id=order.id,
                product_option_id=product_option.id,
                quantity=3,
                unit_price=10000,
                cost_price_snapshot=4000,
                line_amount=30000,
            )
        )
        db_session.flush()

        summaries = ProductPerformanceService(db_session).calculate_daily(target)

        assert len(summaries) == 1
        summary = summaries[0]
        assert summary.product_option_id == product_option.id
        assert summary.sales_qty == 3
        assert summary.revenue == 30000
        assert summary.net_profit == 30000 - 4000 * 3  # 광고비 없음
        assert summary.ad_cost == 0
        assert summary.roas == 0.0

    def test_includes_ad_cost_and_roas(self, db_session, platform, customer, product_option):
        target = date(2026, 4, 2)
        order_date = datetime(2026, 4, 2, 10, 0, tzinfo=timezone.utc)
        order = _make_order(db_session, platform, customer, order_date, order_no="PERF-AD")
        db_session.add(
            OrderItem(
                order_id=order.id,
                product_option_id=product_option.id,
                quantity=1,
                unit_price=20000,
                cost_price_snapshot=8000,
                line_amount=20000,
            )
        )
        campaign = AdCampaign(
            ad_platform_code="naver_search_ad",
            platform_campaign_id="CAMPAIGN-1",
            product_option_id=product_option.id,
            is_active=True,
        )
        db_session.add(campaign)
        db_session.flush()
        db_session.add(AdPerformanceDaily(campaign_id=campaign.id, stat_date=target, cost=5000))
        db_session.flush()

        summaries = ProductPerformanceService(db_session).calculate_daily(target)

        summary = summaries[0]
        assert summary.ad_cost == 5000
        assert summary.net_profit == 20000 - 8000 - 5000
        assert summary.roas == round(20000 / 5000 * 100, 2)

    def test_excludes_canceled_orders(self, db_session, platform, customer, product_option):
        target = date(2026, 4, 3)
        order_date = datetime(2026, 4, 3, 10, 0, tzinfo=timezone.utc)
        order = _make_order(db_session, platform, customer, order_date, status="CANCELED", order_no="PERF-CANCEL")
        db_session.add(
            OrderItem(
                order_id=order.id,
                product_option_id=product_option.id,
                quantity=1,
                unit_price=10000,
                cost_price_snapshot=4000,
                line_amount=10000,
            )
        )
        db_session.flush()

        summaries = ProductPerformanceService(db_session).calculate_daily(target)

        assert summaries == []

    def test_upsert_updates_existing_row(self, db_session, platform, customer, product_option):
        target = date(2026, 4, 4)
        order_date = datetime(2026, 4, 4, 10, 0, tzinfo=timezone.utc)
        order = _make_order(db_session, platform, customer, order_date, order_no="PERF-UPSERT")
        db_session.add(
            OrderItem(
                order_id=order.id,
                product_option_id=product_option.id,
                quantity=1,
                unit_price=10000,
                cost_price_snapshot=4000,
                line_amount=10000,
            )
        )
        db_session.flush()
        service = ProductPerformanceService(db_session)

        first = service.calculate_daily(target)
        second = service.calculate_daily(target)

        assert first[0].id == second[0].id


class TestListRanked:
    def test_orders_by_net_profit(self, db_session, platform, customer, product_option):
        from models.product import ProductOption

        option2 = ProductOption(product_id=product_option.product_id, sku_code="PERF-SKU-2", is_active=True)
        db_session.add(option2)
        db_session.flush()

        target = date(2026, 4, 5)
        order_date = datetime(2026, 4, 5, 10, 0, tzinfo=timezone.utc)
        order = _make_order(db_session, platform, customer, order_date, order_no="PERF-RANK")
        db_session.add(
            OrderItem(
                order_id=order.id,
                product_option_id=product_option.id,
                quantity=1,
                unit_price=10000,
                cost_price_snapshot=1000,
                line_amount=10000,
            )
        )
        db_session.add(
            OrderItem(
                order_id=order.id,
                product_option_id=option2.id,
                quantity=1,
                unit_price=5000,
                cost_price_snapshot=4900,
                line_amount=5000,
            )
        )
        db_session.flush()
        service = ProductPerformanceService(db_session)
        service.calculate_daily(target)

        best = service.list_ranked(target.isoformat(), order="desc", limit=10)
        worst = service.list_ranked(target.isoformat(), order="asc", limit=10)

        assert best[0].product_option_id == product_option.id  # 순이익 9000으로 더 높음
        assert worst[0].product_option_id == option2.id  # 순이익 100으로 더 낮음


class TestCalculateMonthly:
    """SRS FR-REPORT-03: 월별 상품 성과가 일별 합산으로 정확히 이루어지는지 검증."""

    def test_aggregates_daily_into_monthly_row(self, db_session, platform, customer, product_option):
        order1 = _make_order(
            db_session, platform, customer, datetime(2026, 6, 3, 10, 0, tzinfo=timezone.utc), order_no="PERF-MON-1"
        )
        db_session.add(
            OrderItem(
                order_id=order1.id,
                product_option_id=product_option.id,
                quantity=2,
                unit_price=10000,
                cost_price_snapshot=4000,
                line_amount=20000,
            )
        )
        order2 = _make_order(
            db_session, platform, customer, datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc), order_no="PERF-MON-2"
        )
        db_session.add(
            OrderItem(
                order_id=order2.id,
                product_option_id=product_option.id,
                quantity=1,
                unit_price=10000,
                cost_price_snapshot=4000,
                line_amount=10000,
            )
        )
        db_session.flush()

        summaries = ProductPerformanceService(db_session).calculate_monthly(2026, 6)

        assert len(summaries) == 1
        summary = summaries[0]
        assert summary.period_type == "MONTHLY"
        assert summary.period_key == "2026-06"
        assert summary.sales_qty == 3
        assert summary.revenue == 30000
        assert summary.net_profit == 30000 - 4000 * 3

    def test_upsert_updates_existing_monthly_row(self, db_session, platform, customer, product_option):
        order = _make_order(
            db_session, platform, customer, datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc), order_no="PERF-MON-3"
        )
        db_session.add(
            OrderItem(
                order_id=order.id,
                product_option_id=product_option.id,
                quantity=1,
                unit_price=10000,
                cost_price_snapshot=4000,
                line_amount=10000,
            )
        )
        db_session.flush()
        service = ProductPerformanceService(db_session)

        first = service.calculate_monthly(2026, 7)
        second = service.calculate_monthly(2026, 7)

        assert first[0].id == second[0].id
