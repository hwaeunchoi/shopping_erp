"""
services/product_performance_service.py
---------------------------------------------
상품별 매출/순이익 집계 및 베스트/워스트 순위 계산엔진. SRS FR-PRD-04 대응.

profit_calculation_service.py와 동일하게 하루 단위로 계산해
product_performance_summary에 upsert한다. UniqueConstraint(period_type,
period_key, product_option_id) 기준으로 기존 행이 있으면 갱신한다.

취소/반품/환불된 주문은 profit_calculation_service.py의 FR-PROFIT-03 처리와
동일하게 집계에서 제외한다. 순이익은 매출에서 매출원가(order_items.
cost_price_snapshot)와 상품에 연결된 광고비(ad_campaigns.product_option_id
경유)를 뺀 값이다 - 플랫폼 수수료/배송비 등 주문 전체에 걸친 비용은 상품
단위로 정확히 배분할 근거가 없어 이번 계산에는 포함하지 않는다(전체 손익은
profit_loss_summary가 별도로 정확히 계산함).
"""

import calendar
from datetime import date, datetime, timedelta, timezone

from models.analytics import ProductPerformanceSummary
from repositories.ad_repository import AdCampaignRepository, AdPerformanceRepository
from repositories.analytics_repository import ProductPerformanceSummaryRepository
from repositories.order_repository import OrderRepository
from repositories.product_repository import ProductOptionRepository
from services.profit_calculation_service import REVENUE_EXCLUDED_STATUSES


class ProductPerformanceService:
    def __init__(self, session) -> None:
        self.session = session
        self.order_repo = OrderRepository(session)
        self.option_repo = ProductOptionRepository(session)
        self.summary_repo = ProductPerformanceSummaryRepository(session)
        self.ad_campaign_repo = AdCampaignRepository(session)
        self.ad_perf_repo = AdPerformanceRepository(session)

    def calculate_daily(self, target_date: date) -> list[ProductPerformanceSummary]:
        """지정한 날짜의 상품(SKU)별 판매수량/매출/순이익/광고비/ROAS를 계산해 upsert한다."""
        day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
        day_end = day_start + timedelta(days=1)
        orders = [
            o
            for o in self.order_repo.list_by_date_range(day_start, day_end)
            if o.status not in REVENUE_EXCLUDED_STATUSES
        ]

        aggregates: dict[int, dict[str, float]] = {}
        for order in orders:
            for item in self.order_repo.list_items(order.id):
                bucket = aggregates.setdefault(
                    item.product_option_id, {"sales_qty": 0.0, "revenue": 0.0, "cost_of_goods": 0.0}
                )
                bucket["sales_qty"] += item.quantity
                bucket["revenue"] += float(item.line_amount)
                bucket["cost_of_goods"] += float(item.cost_price_snapshot or 0) * item.quantity

        summaries = []
        for product_option_id, agg in aggregates.items():
            ad_cost = self._sum_ad_cost(product_option_id, target_date)
            net_profit = round(agg["revenue"] - agg["cost_of_goods"] - ad_cost, 2)
            roas = round(agg["revenue"] / ad_cost * 100, 2) if ad_cost > 0 else 0.0

            summary = self._get_or_create(target_date.isoformat(), product_option_id)
            summary.sales_qty = int(agg["sales_qty"])
            summary.revenue = round(agg["revenue"], 2)
            summary.net_profit = net_profit
            summary.ad_cost = round(ad_cost, 2)
            summary.roas = roas
            summary.generated_at = datetime.now(timezone.utc)
            summaries.append(summary)

        self.session.flush()
        return summaries

    def calculate_monthly(self, year: int, month: int) -> list[ProductPerformanceSummary]:
        """지정한 연/월의 상품(SKU)별 판매수량/매출/순이익/광고비/ROAS를 계산해 upsert한다.

        해당 월의 일별 집계(period_type="DAILY")를 먼저 보장한 뒤 상품별로 합산한다
        (profit_calculation_service.calculate_monthly()와 동일한 방식). SRS FR-REPORT-03 대응.
        """
        month_start = date(year, month, 1)
        days_in_month = calendar.monthrange(year, month)[1]

        aggregates: dict[int, dict[str, float]] = {}
        cursor = month_start
        for _ in range(days_in_month):
            for daily in self.calculate_daily(cursor):
                bucket = aggregates.setdefault(
                    daily.product_option_id, {"sales_qty": 0.0, "revenue": 0.0, "net_profit": 0.0, "ad_cost": 0.0}
                )
                bucket["sales_qty"] += daily.sales_qty
                bucket["revenue"] += float(daily.revenue)
                bucket["net_profit"] += float(daily.net_profit)
                bucket["ad_cost"] += float(daily.ad_cost)
            cursor += timedelta(days=1)

        period_key = f"{year:04d}-{month:02d}"
        summaries = []
        for product_option_id, agg in aggregates.items():
            roas = round(agg["revenue"] / agg["ad_cost"] * 100, 2) if agg["ad_cost"] > 0 else 0.0
            summary = self._get_or_create_monthly(period_key, product_option_id)
            summary.sales_qty = int(agg["sales_qty"])
            summary.revenue = round(agg["revenue"], 2)
            summary.net_profit = round(agg["net_profit"], 2)
            summary.ad_cost = round(agg["ad_cost"], 2)
            summary.roas = roas
            summary.generated_at = datetime.now(timezone.utc)
            summaries.append(summary)

        self.session.flush()
        return summaries

    def _get_or_create_monthly(self, period_key: str, product_option_id: int) -> ProductPerformanceSummary:
        existing = self.summary_repo.get_by_key("MONTHLY", period_key, product_option_id)
        if existing:
            return existing
        option = self.option_repo.get_by_id(product_option_id)
        summary = ProductPerformanceSummary(
            period_type="MONTHLY",
            period_key=period_key,
            product_option_id=product_option_id,
            product_id=option.product_id if option else None,
            platform_id=None,
            generated_at=datetime.now(timezone.utc),
        )
        return self.summary_repo.add(summary)

    def _sum_ad_cost(self, product_option_id: int, target_date: date) -> float:
        campaigns = self.ad_campaign_repo.list_by_product_option(product_option_id)
        total = 0.0
        for campaign in campaigns:
            perf = self.ad_perf_repo.list_by_campaign_and_range(
                campaign.id, target_date, target_date + timedelta(days=1)
            )
            total += sum(float(p.cost) for p in perf)
        return total

    def _get_or_create(self, period_key: str, product_option_id: int) -> ProductPerformanceSummary:
        existing = self.summary_repo.get_by_key("DAILY", period_key, product_option_id)
        if existing:
            return existing
        option = self.option_repo.get_by_id(product_option_id)
        summary = ProductPerformanceSummary(
            period_type="DAILY",
            period_key=period_key,
            product_option_id=product_option_id,
            product_id=option.product_id if option else None,
            platform_id=None,
            generated_at=datetime.now(timezone.utc),
        )
        return self.summary_repo.add(summary)

    def list_ranked(
        self, period_key: str, order: str = "desc", limit: int = 10, period_type: str = "DAILY"
    ) -> list[ProductPerformanceSummary]:
        return self.summary_repo.list_ranked(period_type, period_key, order=order, limit=limit)
