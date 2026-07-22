"""
services/report_service.py
------------------------------
SRS 3.7 보고서(FR-REPORT-01~04) 중 월별 경영보고서(FR-REPORT-03)를 구성하고
XLSX(FR-REPORT-04)/PDF(FR-REPORT-04)로 내보낸다.

월별 손익/상품성과는 이미 계산엔진(ProfitCalculationService/
ProductPerformanceService)의 calculate_monthly()가 담당하므로, 이 서비스는
그 결과와 플랫폼별 매출, 광고성과, 반품/교환/취소율, KPI 달성률을 한 번에
모아 보고서 데이터를 구성하는 조합(assembly) 계층이다. Repository만
사용하고 세션 쿼리를 직접 다루지 않는다.
"""

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from models.analytics import ProductPerformanceSummary, ProfitLossSummary
from repositories.ad_repository import AdCampaignRepository, AdPerformanceRepository
from repositories.analytics_repository import KpiTargetRepository
from repositories.cost_repository import CostRepository
from repositories.order_repository import OrderRepository
from repositories.platform_repository import PlatformRepository
from services.product_performance_service import ProductPerformanceService
from services.profit_calculation_service import ProfitCalculationService

# FR-PROFIT-03과 동일하게 취소/반품/환불 주문은 매출 집계에서 제외한다.
REVENUE_EXCLUDED_STATUSES = {"CANCELED", "RETURNED", "REFUNDED"}


@dataclass
class PlatformBreakdown:
    platform_id: int
    platform_code: str
    platform_name: str
    revenue: float
    order_count: int


@dataclass
class AdPlatformPerformance:
    ad_platform_code: str
    impressions: int
    clicks: int
    cost: float
    conversions: int
    conversion_amount: float
    cpc: float
    cpm: float
    roas: float


@dataclass
class KpiComparison:
    metric: str
    target_value: float
    actual_value: float
    achievement_rate: float  # target_value 대비 actual_value 비율(%)


@dataclass
class CostTypeBreakdown:
    """SRS FR-COST-02: 비용의 고정비/변동비 구분을 손익계산 결과에 반영해 보여준다.

    profit_loss_summary는 배송비/포장비/기타비용으로만 구분해 저장하므로(스키마
    변경 없이), 같은 기간의 원본 costs 행을 cost_type(FIXED/VARIABLE)별로 다시
    합산한 값을 보고서에서만 별도로 계산해 보여준다.
    """

    fixed_cost: float
    variable_cost: float


@dataclass
class MonthlyManagementReport:
    """SRS FR-REPORT-03: 월별 경영보고서 데이터."""

    period_key: str  # "2026-07"
    profit_loss: ProfitLossSummary
    platform_breakdown: list[PlatformBreakdown]
    best_products: list[ProductPerformanceSummary]
    worst_products: list[ProductPerformanceSummary]
    ad_performance: list[AdPlatformPerformance]
    return_rate: float
    exchange_rate: float
    cancel_rate: float
    cost_type_breakdown: CostTypeBreakdown
    kpi_comparisons: list[KpiComparison] = field(default_factory=list)


class ReportService:
    def __init__(self, session) -> None:
        self.session = session
        self.order_repo = OrderRepository(session)
        self.platform_repo = PlatformRepository(session)
        self.ad_campaign_repo = AdCampaignRepository(session)
        self.ad_perf_repo = AdPerformanceRepository(session)
        self.kpi_repo = KpiTargetRepository(session)
        self.cost_repo = CostRepository(session)
        self.profit_service = ProfitCalculationService(session)
        self.product_perf_service = ProductPerformanceService(session)

    def generate_monthly_report(self, year: int, month: int) -> MonthlyManagementReport:
        period_key = f"{year:04d}-{month:02d}"
        month_start = date(year, month, 1)
        days_in_month = calendar.monthrange(year, month)[1]
        month_end = month_start + timedelta(days=days_in_month)

        profit_loss = self.profit_service.calculate_monthly(year, month)
        self.product_perf_service.calculate_monthly(year, month)

        best_products = self.product_perf_service.list_ranked(period_key, order="desc", limit=5, period_type="MONTHLY")
        worst_products = self.product_perf_service.list_ranked(period_key, order="asc", limit=5, period_type="MONTHLY")

        platform_breakdown = self._platform_breakdown(month_start, month_end)
        ad_performance = self._ad_performance(month_start, month_end)
        return_rate, exchange_rate, cancel_rate = self._status_rates(month_start, month_end)
        cost_type_breakdown = self._cost_type_breakdown(month_start, month_end)
        kpi_comparisons = self._kpi_comparisons(period_key, profit_loss)

        return MonthlyManagementReport(
            period_key=period_key,
            profit_loss=profit_loss,
            platform_breakdown=platform_breakdown,
            best_products=best_products,
            worst_products=worst_products,
            ad_performance=ad_performance,
            return_rate=return_rate,
            exchange_rate=exchange_rate,
            cancel_rate=cancel_rate,
            cost_type_breakdown=cost_type_breakdown,
            kpi_comparisons=kpi_comparisons,
        )

    def _platform_breakdown(self, month_start: date, month_end: date) -> list[PlatformBreakdown]:
        start_dt = datetime.combine(month_start, datetime.min.time(), tzinfo=timezone.utc)
        end_dt = datetime.combine(month_end, datetime.min.time(), tzinfo=timezone.utc)
        orders = [
            o for o in self.order_repo.list_by_date_range(start_dt, end_dt) if o.status not in REVENUE_EXCLUDED_STATUSES
        ]

        aggregates: dict[int, dict[str, float]] = {}
        for order in orders:
            bucket = aggregates.setdefault(order.platform_id, {"revenue": 0.0, "order_count": 0})
            bucket["revenue"] += float(order.total_amount)
            bucket["order_count"] += 1

        breakdown = []
        for platform_id, agg in aggregates.items():
            platform = self.platform_repo.get_by_id(platform_id)
            breakdown.append(
                PlatformBreakdown(
                    platform_id=platform_id,
                    platform_code=platform.code if platform else "UNKNOWN",
                    platform_name=platform.name if platform else "알 수 없음",
                    revenue=round(agg["revenue"], 2),
                    order_count=int(agg["order_count"]),
                )
            )
        breakdown.sort(key=lambda b: b.revenue, reverse=True)
        return breakdown

    def _ad_performance(self, month_start: date, month_end: date) -> list[AdPlatformPerformance]:
        aggregates: dict[str, dict[str, float]] = {}
        for campaign in self.ad_campaign_repo.list_all():
            perf = self.ad_perf_repo.list_by_campaign_and_range(campaign.id, month_start, month_end)
            bucket = aggregates.setdefault(
                campaign.ad_platform_code,
                {"impressions": 0, "clicks": 0, "cost": 0.0, "conversions": 0, "conversion_amount": 0.0},
            )
            for p in perf:
                bucket["impressions"] += p.impressions
                bucket["clicks"] += p.clicks
                bucket["cost"] += float(p.cost)
                bucket["conversions"] += p.conversions
                bucket["conversion_amount"] += float(p.conversion_amount)

        result = []
        for ad_platform_code, agg in aggregates.items():
            cost = agg["cost"]
            impressions = int(agg["impressions"])
            clicks = int(agg["clicks"])
            conversion_amount = agg["conversion_amount"]
            cpc = round(cost / clicks, 2) if clicks > 0 else 0.0
            cpm = round(cost / impressions * 1000, 2) if impressions > 0 else 0.0
            roas = round(conversion_amount / cost * 100, 2) if cost > 0 else 0.0
            result.append(
                AdPlatformPerformance(
                    ad_platform_code=ad_platform_code,
                    impressions=impressions,
                    clicks=clicks,
                    cost=round(cost, 2),
                    conversions=int(agg["conversions"]),
                    conversion_amount=round(conversion_amount, 2),
                    cpc=cpc,
                    cpm=cpm,
                    roas=roas,
                )
            )
        result.sort(key=lambda a: a.cost, reverse=True)
        return result

    def _status_rates(self, month_start: date, month_end: date) -> tuple[float, float, float]:
        """FR-REPORT-03: 반품률/교환률/취소율 = 해당 상태 주문수 / 전체 주문수 * 100."""
        start_dt = datetime.combine(month_start, datetime.min.time(), tzinfo=timezone.utc)
        end_dt = datetime.combine(month_end, datetime.min.time(), tzinfo=timezone.utc)
        orders = self.order_repo.list_by_date_range(start_dt, end_dt)
        total = len(orders)
        if total == 0:
            return 0.0, 0.0, 0.0

        returned = sum(1 for o in orders if o.status in ("RETURNED", "REFUNDED"))
        exchanged = sum(1 for o in orders if o.status == "EXCHANGED")
        canceled = sum(1 for o in orders if o.status == "CANCELED")
        return (round(returned / total * 100, 2), round(exchanged / total * 100, 2), round(canceled / total * 100, 2))

    def _cost_type_breakdown(self, month_start: date, month_end: date) -> CostTypeBreakdown:
        """SRS FR-COST-02: 해당 월 costs를 cost_type(FIXED/VARIABLE)별로 합산한다."""
        costs = self.cost_repo.list_by_date_range(month_start, month_end)
        fixed_cost = sum(float(c.amount) for c in costs if c.cost_type == "FIXED")
        variable_cost = sum(float(c.amount) for c in costs if c.cost_type == "VARIABLE")
        return CostTypeBreakdown(fixed_cost=round(fixed_cost, 2), variable_cost=round(variable_cost, 2))

    def _kpi_comparisons(self, period_key: str, profit_loss: ProfitLossSummary) -> list[KpiComparison]:
        actual_values = {
            "REVENUE": float(profit_loss.net_revenue),
            "NET_PROFIT": float(profit_loss.net_profit),
            "AD_COST": float(profit_loss.ad_cost),
            "ORDER_COUNT": float(profit_loss.order_count),
            "AOV": float(profit_loss.net_revenue) / profit_loss.order_count if profit_loss.order_count > 0 else 0.0,
            "ROAS": (
                float(profit_loss.ad_conversion_revenue) / float(profit_loss.ad_cost) * 100
                if float(profit_loss.ad_cost) > 0
                else 0.0
            ),
        }
        comparisons = []
        for metric, actual in actual_values.items():
            target = self.kpi_repo.get_by_period_and_metric(period_key, metric)
            if target is None:
                continue
            target_value = float(target.target_value)
            achievement_rate = round(actual / target_value * 100, 2) if target_value > 0 else 0.0
            comparisons.append(
                KpiComparison(
                    metric=metric,
                    target_value=target_value,
                    actual_value=round(actual, 2),
                    achievement_rate=achievement_rate,
                )
            )
        return comparisons
