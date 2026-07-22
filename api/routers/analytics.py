"""
api/routers/analytics.py
----------------------------
매출/손익 요약 조회 및 계산 트리거(services.ProfitCalculationService 호출).
"""

from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.deps import get_db, require_permission
from repositories.analytics_repository import ProductPerformanceSummaryRepository, ProfitLossSummaryRepository
from services.product_performance_service import ProductPerformanceService
from services.profit_calculation_service import ProfitCalculationService

router = APIRouter(
    prefix="/api/analytics", tags=["analytics"], dependencies=[Depends(require_permission("ANALYTICS_VIEW"))]
)


class ProfitLossSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    period_type: str
    basis_type: str
    period_key: str
    platform_id: Optional[int]
    gross_revenue: float
    net_revenue: float
    order_count: int
    ad_cost: float
    cost_of_goods: float
    platform_fee: float
    total_cost: float
    net_profit: float
    net_profit_rate: float


class CalculateDailyRequest(BaseModel):
    target_date: date
    basis_type: str = "ORDER_DATE"
    platform_id: Optional[int] = None


@router.get(
    "/profit-loss",
    response_model=list[ProfitLossSummaryOut],
    summary="매출/손익 요약 조회",
    description="이미 계산되어 저장된 기간별(period_type) 손익 요약을 조회한다. platform_id를 지정하지 "
    "않으면 전체(플랫폼 무관) 집계를, 지정하면 해당 플랫폼만의 집계를 반환한다(SRS FR-PROFIT-02). "
    "아직 계산되지 않은 기간은 결과에 나타나지 않는다 - 먼저 POST /profit-loss/calculate로 계산해야 한다.",
)
def list_profit_loss(
    period_type: str = "DAILY",
    basis_type: str = "ORDER_DATE",
    platform_id: Optional[int] = None,
    db: Session = Depends(get_db),
) -> list:
    return ProfitLossSummaryRepository(db).list_by_period(period_type, basis_type, platform_id=platform_id)


@router.post(
    "/profit-loss/calculate",
    response_model=ProfitLossSummaryOut,
    status_code=status.HTTP_200_OK,
    summary="일별 손익 계산",
    description="target_date 하루치 매출/원가/광고비/수수료를 계산하여 손익 요약을 upsert한다. "
    "platform_id를 지정하면 해당 플랫폼만의 집계를 별도로 계산한다(SRS FR-PROFIT-02). "
    "같은 날짜(및 platform_id)로 다시 호출하면 기존 요약을 최신 값으로 덮어쓴다(멱등).",
)
def calculate_profit_loss(payload: CalculateDailyRequest, db: Session = Depends(get_db)):
    summary = ProfitCalculationService(db).calculate_daily(
        payload.target_date, basis_type=payload.basis_type, platform_id=payload.platform_id
    )
    db.commit()
    return summary


class ProductPerformanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    product_option_id: int
    product_id: Optional[int]
    sales_qty: int
    revenue: float
    net_profit: float
    ad_cost: float
    roas: float


class CalculateProductPerformanceRequest(BaseModel):
    target_date: date


@router.get(
    "/product-performance",
    response_model=list[ProductPerformanceOut],
    summary="상품별 베스트/워스트 순위 조회",
    description="period_key(예: 2026-07-02) 하루치 상품별 집계를 순이익 기준으로 정렬해 반환한다. "
    "order=desc면 베스트(순이익 높은 순), order=asc면 워스트(순이익 낮은 순). "
    "먼저 POST /product-performance/calculate로 계산해야 한다. SRS FR-PRD-04 대응.",
)
def list_product_performance(
    period_key: str, order: Literal["desc", "asc"] = "desc", limit: int = 10, db: Session = Depends(get_db)
) -> list:
    return ProductPerformanceSummaryRepository(db).list_ranked("DAILY", period_key, order=order, limit=limit)


@router.post(
    "/product-performance/calculate",
    response_model=list[ProductPerformanceOut],
    status_code=status.HTTP_200_OK,
    summary="상품별 일별 성과 계산",
    description="target_date 하루치 상품(SKU)별 판매수량/매출/순이익/광고비/ROAS를 계산하여 upsert한다. "
    "취소/반품/환불 주문은 매출 집계와 동일하게 제외한다(FR-PROFIT-03과 일관).",
)
def calculate_product_performance(payload: CalculateProductPerformanceRequest, db: Session = Depends(get_db)):
    summaries = ProductPerformanceService(db).calculate_daily(payload.target_date)
    db.commit()
    return summaries
