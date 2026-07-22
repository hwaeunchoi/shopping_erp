"""
api/routers/ads.py
----------------------
광고 캠페인/일별 성과 조회.

SRS FR-AD-03(CPC/CPM/CTR/ROAS 계산): ad_performance_daily는 원시 지표(노출/클릭/
비용/전환/전환매출)만 저장하고, CPC/CPM/CTR/ROAS는 저장하지 않는 파생값이라
조회 시점에 계산해서 내려준다(스키마 변경 없음).
"""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.deps import get_db, require_permission
from models.ad import AdPerformanceDaily
from repositories.ad_repository import AdCampaignRepository, AdPerformanceRepository

router = APIRouter(prefix="/api/ad-campaigns", tags=["ads"], dependencies=[Depends(require_permission("AD_MANAGE"))])


class AdCampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ad_platform_code: str
    platform_campaign_id: str
    name: Optional[str]
    product_option_id: Optional[int]
    is_active: bool


class AdPerformanceOut(BaseModel):
    stat_date: date
    impressions: int
    clicks: int
    cost: float
    conversions: int
    conversion_amount: float
    cpc: float  # 클릭당 비용 = cost / clicks
    cpm: float  # 노출 1000회당 비용 = cost / impressions * 1000
    ctr: float  # 클릭률(%) = clicks / impressions * 100
    roas: float  # 광고비 대비 전환매출(%) = conversion_amount / cost * 100


def _to_performance_out(perf: AdPerformanceDaily) -> AdPerformanceOut:
    cost = float(perf.cost)
    conversion_amount = float(perf.conversion_amount)
    cpc = round(cost / perf.clicks, 2) if perf.clicks > 0 else 0.0
    cpm = round(cost / perf.impressions * 1000, 2) if perf.impressions > 0 else 0.0
    ctr = round(perf.clicks / perf.impressions * 100, 2) if perf.impressions > 0 else 0.0
    roas = round(conversion_amount / cost * 100, 2) if cost > 0 else 0.0
    return AdPerformanceOut(
        stat_date=perf.stat_date,
        impressions=perf.impressions,
        clicks=perf.clicks,
        cost=cost,
        conversions=perf.conversions,
        conversion_amount=conversion_amount,
        cpc=cpc,
        cpm=cpm,
        ctr=ctr,
        roas=roas,
    )


@router.get(
    "",
    response_model=list[AdCampaignOut],
    summary="광고 캠페인 목록 조회",
    description="활성 상태인 광고 캠페인 목록을 조회한다.",
)
def list_campaigns(db: Session = Depends(get_db)) -> list:
    return AdCampaignRepository(db).list_active()


@router.get(
    "/{campaign_id}/performance",
    response_model=list[AdPerformanceOut],
    summary="광고 캠페인 일별 성과 조회",
    description="[start_date, end_date] 구간의 일별 노출/클릭/비용/전환 성과와, 조회 시점에 계산한 "
    "CPC/CPM/CTR/ROAS를 함께 반환한다. 캠페인이 없으면 빈 목록을 반환한다. SRS FR-AD-03 대응.",
)
def list_campaign_performance(
    campaign_id: int, start_date: date, end_date: date, db: Session = Depends(get_db)
) -> list[AdPerformanceOut]:
    perf = AdPerformanceRepository(db).list_by_campaign_and_range(campaign_id, start_date, end_date)
    return [_to_performance_out(p) for p in perf]
