"""
models/analytics.py
---------------------
ERD 2.9 매출/손익/KPI 그룹: profit_loss_summary, product_performance_summary,
kpi_targets

두 요약 테이블은 orders~costs~ads를 매번 조인하지 않도록 스케줄러 배치가
주기적으로 재계산하여 채우는 집계(비정규화) 테이블이다. 대시보드/보고서는
항상 이 테이블에서 조회한다.

v1.2 권장 개선사항 반영: product_performance_summary에 product_id/platform_id
캐시 컬럼을 추가하여 상품 단위·플랫폼별 집계 조회 시 조인을 줄인다.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class ProfitLossSummary(Base):
    """기간·기준별 손익 요약. basis_type으로 손익 계산 기준 4종을 구분한다."""

    __tablename__ = "profit_loss_summary"
    __table_args__ = (
        UniqueConstraint(
            "period_type", "basis_type", "period_key", "platform_id", "product_option_id", name="uq_profit_loss_summary"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    period_type: Mapped[str] = mapped_column(String(10), nullable=False)  # DAILY/WEEKLY/MONTHLY
    # ORDER_DATE/PAYMENT_DATE/DELIVERY_DATE/SETTLEMENT_DATE
    basis_type: Mapped[str] = mapped_column(String(20), nullable=False)
    period_key: Mapped[str] = mapped_column(String(20), nullable=False)  # 예: "2026-06", "2026-W26", "2026-06-15"
    platform_id: Mapped[Optional[int]] = mapped_column(ForeignKey("platforms.id"), nullable=True)  # NULL=전체
    product_option_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("product_options.id"), nullable=True
    )  # NULL=전체

    gross_revenue: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    net_revenue: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    order_count: Mapped[int] = mapped_column(default=0, nullable=False)
    ad_cost: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    ad_conversion_revenue: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    cost_of_goods: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    platform_fee: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    shipping_cost: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    packaging_cost: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    other_cost: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    total_cost: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    net_profit: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    net_profit_rate: Mapped[float] = mapped_column(Numeric(5, 2), default=0, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ProductPerformanceSummary(Base):
    """기간·상품별 분석 집계 (베스트/저수익/광고효율낮음 판별의 기반 데이터)."""

    __tablename__ = "product_performance_summary"
    __table_args__ = (
        UniqueConstraint("period_type", "period_key", "product_option_id", name="uq_product_perf_summary"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    period_type: Mapped[str] = mapped_column(String(10), nullable=False)
    period_key: Mapped[str] = mapped_column(String(20), nullable=False)
    product_option_id: Mapped[int] = mapped_column(ForeignKey("product_options.id"), nullable=False)
    # v1.2 권장 개선사항: 조인 최소화용 캐시 컬럼
    product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("products.id"), nullable=True)
    platform_id: Mapped[Optional[int]] = mapped_column(ForeignKey("platforms.id"), nullable=True)

    sales_qty: Mapped[int] = mapped_column(default=0, nullable=False)
    revenue: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    net_profit: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    ad_cost: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    roas: Mapped[float] = mapped_column(Numeric(6, 2), default=0, nullable=False)
    return_rate: Mapped[float] = mapped_column(Numeric(5, 2), default=0, nullable=False)
    exchange_rate: Mapped[float] = mapped_column(Numeric(5, 2), default=0, nullable=False)
    cancel_rate: Mapped[float] = mapped_column(Numeric(5, 2), default=0, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class KpiTarget(Base):
    """KPI 목표관리. 달성률은 저장하지 않고 profit_loss_summary와 실시간 비교하여 계산."""

    __tablename__ = "kpi_targets"
    __table_args__ = (UniqueConstraint("period_key", "metric", name="uq_kpi_target"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    period_key: Mapped[str] = mapped_column(String(10), nullable=False)  # 예: "2026-07"
    # REVENUE/NET_PROFIT/AD_COST/ROAS/ORDER_COUNT/AOV
    metric: Mapped[str] = mapped_column(String(30), nullable=False)
    target_value: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
