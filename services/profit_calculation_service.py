"""
services/profit_calculation_service.py
------------------------------------------
매출/손익 요약(profit_loss_summary) 계산엔진.

orders/order_items/costs/ad_performance_daily를 조회해 하루 단위 손익을
계산하고 profit_loss_summary에 upsert한다. 스키마의
UniqueConstraint(period_type, basis_type, period_key, platform_id,
product_option_id)를 기준으로 기존 행이 있으면 갱신하고, 없으면 새로 만든다.

platform_id를 지정하지 않으면 platform_id/product_option_id가 모두 NULL인
"전체" 집계를 계산한다(SRS FR-PROFIT-02 플랫폼 단위 세분화 조회 대응으로
platform_id를 지정하면 해당 플랫폼의 주문만으로 별도 집계 행을 만든다).
광고비는 ad_campaigns가 쇼핑몰 플랫폼이 아닌 광고 플랫폼(ad_platform_code)
단위로만 연결되어 있어 특정 쇼핑몰 플랫폼으로 귀속시킬 근거가 없으므로,
플랫폼별 집계에서는 0으로 둔다(상품별 세부 집계는 필요해지면 동일한
방식으로 확장한다).

플랫폼 수수료율은 platform_fee_rules 테이블(PlatformFeeRuleRepository)에서
조회한다. 적용 가능한 규칙이 없으면 시스템 전체가 중단되지 않도록 경고
로그만 남기고 기본값 0%를 적용한다(자세한 선택 규칙은
PlatformFeeRuleRepository.get_effective_rule() 문서 참고).

SRS FR-PROFIT-03: 취소·반품·환불이 발생한 주문(orders.status가
CANCELED/RETURNED/REFUNDED)은 매출/원가/수수료/주문건수 집계에서 전부
제외한다 - 교환(EXCHANGED)은 매출이 그대로 유지되는 거래이므로 제외하지
않는다.
"""

import calendar
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from models.analytics import ProfitLossSummary
from models.order import Order
from models.platform import Platform
from repositories.ad_repository import AdPerformanceRepository
from repositories.analytics_repository import ProfitLossSummaryRepository
from repositories.cost_repository import CostRepository
from repositories.order_repository import OrderRepository
from repositories.platform_repository import PlatformFeeRuleRepository, PlatformRepository

logger = logging.getLogger(__name__)

DEFAULT_FEE_RATE = 0.0

# FR-PROFIT-03: 취소/반품/환불된 주문은 매출 집계에서 제외한다.
REVENUE_EXCLUDED_STATUSES = {"CANCELED", "RETURNED", "REFUNDED"}


class ProfitCalculationService:
    def __init__(self, session) -> None:
        self.session = session
        self.order_repo = OrderRepository(session)
        self.cost_repo = CostRepository(session)
        self.ad_perf_repo = AdPerformanceRepository(session)
        self.summary_repo = ProfitLossSummaryRepository(session)
        self.platform_repo = PlatformRepository(session)
        self.fee_rule_repo = PlatformFeeRuleRepository(session)

    def calculate_daily(
        self, target_date: date, basis_type: str = "ORDER_DATE", platform_id: Optional[int] = None
    ) -> ProfitLossSummary:
        """지정한 날짜의 일별 손익을 계산해 upsert한다.

        platform_id가 None이면 전체(플랫폼 무관) 집계, 지정하면 해당 플랫폼
        주문만으로 별도 집계 행을 만든다(SRS FR-PROFIT-02).
        """
        day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
        day_end = day_start + timedelta(days=1)
        orders = self._revenue_orders(day_start, day_end)
        if platform_id is not None:
            orders = [o for o in orders if o.platform_id == platform_id]

        gross_revenue = 0.0
        net_revenue = 0.0
        cost_of_goods = 0.0
        platform_fee = 0.0
        platform_cache: dict[int, Optional[Platform]] = {}

        items_by_order: dict[int, list] = {}
        for item in self.order_repo.list_items_by_order_ids([o.id for o in orders]):
            items_by_order.setdefault(item.order_id, []).append(item)

        for order in orders:
            gross_revenue += float(order.total_amount) + float(order.discount_amount)
            net_revenue += float(order.total_amount)

            platform = platform_cache.get(order.platform_id)
            if platform is None:
                platform = self.platform_repo.get_by_id(order.platform_id)
                platform_cache[order.platform_id] = platform
            fee_rate = self._get_fee_rate(platform, order.order_date.date())
            platform_fee += float(order.total_amount) * fee_rate

            for item in items_by_order.get(order.id, []):
                cost_of_goods += float(item.cost_price_snapshot or 0) * item.quantity

        shipping_cost, packaging_cost, other_cost = self._sum_costs(target_date, platform_id=platform_id)
        # 광고비는 쇼핑몰 플랫폼에 귀속시킬 근거가 없어 플랫폼별 집계에서는 0으로 둔다(클래스 docstring 참고).
        ad_cost, ad_conversion_revenue = (
            (0.0, 0.0) if platform_id is not None else self._sum_ad_performance(target_date)
        )

        total_cost = round(cost_of_goods + platform_fee + shipping_cost + packaging_cost + other_cost, 2)
        net_profit = round(net_revenue - total_cost - ad_cost, 2)
        net_profit_rate = round(net_profit / net_revenue * 100, 2) if net_revenue > 0 else 0.0

        summary = self._get_or_create(
            period_type="DAILY", basis_type=basis_type, period_key=target_date.isoformat(), platform_id=platform_id
        )
        summary.gross_revenue = round(gross_revenue, 2)
        summary.net_revenue = round(net_revenue, 2)
        summary.order_count = len(orders)
        summary.ad_cost = round(ad_cost, 2)
        summary.ad_conversion_revenue = round(ad_conversion_revenue, 2)
        summary.cost_of_goods = round(cost_of_goods, 2)
        summary.platform_fee = round(platform_fee, 2)
        summary.shipping_cost = shipping_cost
        summary.packaging_cost = packaging_cost
        summary.other_cost = other_cost
        summary.total_cost = total_cost
        summary.net_profit = net_profit
        summary.net_profit_rate = net_profit_rate
        summary.generated_at = datetime.now(timezone.utc)

        self.session.flush()
        return summary

    def calculate_daily_range(
        self, start_date: date, end_date: date, basis_type: str = "ORDER_DATE", platform_id: Optional[int] = None
    ) -> list[ProfitLossSummary]:
        """[start_date, end_date) 구간의 일별 손익을 하루씩 계산한다."""
        summaries = []
        cursor = start_date
        while cursor < end_date:
            summaries.append(self.calculate_daily(cursor, basis_type=basis_type, platform_id=platform_id))
            cursor += timedelta(days=1)
        return summaries

    def calculate_monthly(
        self, year: int, month: int, basis_type: str = "ORDER_DATE", platform_id: Optional[int] = None
    ) -> ProfitLossSummary:
        """지정한 연/월의 월별 손익을 계산해 upsert한다. SRS FR-REPORT-03/FR-PROFIT-02 대응.

        해당 월의 일별 손익(profit_loss_summary, period_type="DAILY")을 먼저
        보장(없으면 calculate_daily로 채움)한 뒤 합산한다 - 계산 로직을
        이중으로 유지하지 않고 이미 검증된 일별 계산 결과를 그대로 재사용한다.
        platform_id를 지정하면 해당 플랫폼만의 월별 집계를 별도 행으로 만든다.
        """
        month_start = date(year, month, 1)
        days_in_month = calendar.monthrange(year, month)[1]
        month_end = month_start + timedelta(days=days_in_month)

        daily_summaries = self.calculate_daily_range(
            month_start, month_end, basis_type=basis_type, platform_id=platform_id
        )

        gross_revenue = sum(float(s.gross_revenue) for s in daily_summaries)
        net_revenue = sum(float(s.net_revenue) for s in daily_summaries)
        order_count = sum(s.order_count for s in daily_summaries)
        ad_cost = sum(float(s.ad_cost) for s in daily_summaries)
        ad_conversion_revenue = sum(float(s.ad_conversion_revenue) for s in daily_summaries)
        cost_of_goods = sum(float(s.cost_of_goods) for s in daily_summaries)
        platform_fee = sum(float(s.platform_fee) for s in daily_summaries)
        shipping_cost = sum(float(s.shipping_cost) for s in daily_summaries)
        packaging_cost = sum(float(s.packaging_cost) for s in daily_summaries)
        other_cost = sum(float(s.other_cost) for s in daily_summaries)
        total_cost = round(cost_of_goods + platform_fee + shipping_cost + packaging_cost + other_cost, 2)
        net_profit = round(net_revenue - total_cost - ad_cost, 2)
        net_profit_rate = round(net_profit / net_revenue * 100, 2) if net_revenue > 0 else 0.0

        period_key = f"{year:04d}-{month:02d}"
        summary = self._get_or_create(
            period_type="MONTHLY", basis_type=basis_type, period_key=period_key, platform_id=platform_id
        )
        summary.gross_revenue = round(gross_revenue, 2)
        summary.net_revenue = round(net_revenue, 2)
        summary.order_count = order_count
        summary.ad_cost = round(ad_cost, 2)
        summary.ad_conversion_revenue = round(ad_conversion_revenue, 2)
        summary.cost_of_goods = round(cost_of_goods, 2)
        summary.platform_fee = round(platform_fee, 2)
        summary.shipping_cost = round(shipping_cost, 2)
        summary.packaging_cost = round(packaging_cost, 2)
        summary.other_cost = round(other_cost, 2)
        summary.total_cost = total_cost
        summary.net_profit = net_profit
        summary.net_profit_rate = net_profit_rate
        summary.generated_at = datetime.now(timezone.utc)

        self.session.flush()
        return summary

    def _revenue_orders(self, day_start: datetime, day_end: datetime) -> list[Order]:
        """매출 집계 대상 주문만 남긴다(취소/반품/환불 제외, FR-PROFIT-03)."""
        orders = self.order_repo.list_by_date_range(day_start, day_end)
        return [o for o in orders if o.status not in REVENUE_EXCLUDED_STATUSES]

    def _get_fee_rate(self, platform: Optional[Platform], order_date: date) -> float:
        """platform_fee_rules에서 order_date 기준 적용 수수료율을 조회한다.

        fee_rate 컬럼은 백분율(예: 3.50 = 3.5%)로 저장되어 있으므로 소수로
        변환해 반환한다. 규칙을 찾지 못하면(플랫폼 정보 자체가 없거나,
        platform_fee_rules에 해당 기간의 규칙이 없는 경우) 시스템이 중단되지
        않도록 경고 로그만 남기고 기본값(0%)을 반환한다.
        """
        if platform is None:
            logger.warning("플랫폼 정보를 찾을 수 없어 기본 수수료율(%.0f%%)을 적용합니다.", DEFAULT_FEE_RATE * 100)
            return DEFAULT_FEE_RATE

        rule = self.fee_rule_repo.get_effective_rule(platform.id, order_date)
        if rule is None:
            logger.warning(
                "적용 가능한 수수료 규칙이 없어 기본 수수료율(%.0f%%)을 적용합니다: "
                "platform_id=%s, platform_code=%s, order_date=%s",
                DEFAULT_FEE_RATE * 100,
                platform.id,
                platform.code,
                order_date,
            )
            return DEFAULT_FEE_RATE

        return float(rule.fee_rate) / 100

    def _sum_costs(self, target_date: date, platform_id: Optional[int] = None) -> tuple[float, float, float]:
        """플랫폼별 집계(platform_id 지정)일 때는 해당 플랫폼으로 명시적으로 태그된 costs 행만 더한다.
        platform_id가 NULL인 비용은 특정 플랫폼에 귀속시킬 근거가 없으므로 전체 집계에서만 포함한다."""
        costs = self.cost_repo.list_by_date_range(target_date, target_date + timedelta(days=1))
        if platform_id is not None:
            costs = [c for c in costs if c.platform_id == platform_id]
        shipping = sum(float(c.amount) for c in costs if c.category == "SHIPPING")
        packaging = sum(float(c.amount) for c in costs if c.category == "PACKAGING")
        other = sum(float(c.amount) for c in costs if c.category not in ("SHIPPING", "PACKAGING"))
        return round(shipping, 2), round(packaging, 2), round(other, 2)

    def _sum_ad_performance(self, target_date: date) -> tuple[float, float]:
        perf = self.ad_perf_repo.list_by_date(target_date)
        cost = sum(float(p.cost) for p in perf)
        conv_revenue = sum(float(p.conversion_amount) for p in perf)
        return round(cost, 2), round(conv_revenue, 2)

    def _get_or_create(
        self, period_type: str, basis_type: str, period_key: str, platform_id: Optional[int] = None
    ) -> ProfitLossSummary:
        existing = self.summary_repo.get_by_key(
            period_type, basis_type, period_key, platform_id=platform_id, product_option_id=None
        )
        if existing:
            return existing
        summary = ProfitLossSummary(
            period_type=period_type,
            basis_type=basis_type,
            period_key=period_key,
            platform_id=platform_id,
            product_option_id=None,
            generated_at=datetime.now(timezone.utc),
        )
        return self.summary_repo.add(summary)
