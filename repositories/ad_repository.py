"""
repositories/ad_repository.py
---------------------------------
ERD 2.8 광고 그룹(ad_campaigns, ad_performance_daily)에 대한 Repository.
"""

from datetime import date
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.ad import AdCampaign, AdPerformanceDaily
from repositories.base_repository import BaseRepository


class AdCampaignRepository(BaseRepository[AdCampaign]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, AdCampaign)

    def list_active(self) -> list[AdCampaign]:
        stmt = select(AdCampaign).where(AdCampaign.is_active.is_(True))
        return list(self.session.execute(stmt).scalars().all())

    def list_by_product_option(self, product_option_id: int) -> list[AdCampaign]:
        stmt = select(AdCampaign).where(AdCampaign.product_option_id == product_option_id)
        return list(self.session.execute(stmt).scalars().all())

    def get_by_platform_campaign_id(self, ad_platform_code: str, platform_campaign_id: str) -> Optional[AdCampaign]:
        """스케줄러의 광고 수집 작업이 신규/기존 캠페인을 구분할 때 사용한다."""
        stmt = select(AdCampaign).where(
            AdCampaign.ad_platform_code == ad_platform_code, AdCampaign.platform_campaign_id == platform_campaign_id
        )
        return self.session.execute(stmt).scalar_one_or_none()


class AdPerformanceRepository(BaseRepository[AdPerformanceDaily]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, AdPerformanceDaily)

    def list_by_campaign_and_range(self, campaign_id: int, start: date, end: date) -> list[AdPerformanceDaily]:
        stmt = select(AdPerformanceDaily).where(
            AdPerformanceDaily.campaign_id == campaign_id,
            AdPerformanceDaily.stat_date >= start,
            AdPerformanceDaily.stat_date < end,
        )
        return list(self.session.execute(stmt).scalars().all())

    def list_by_date(self, stat_date: date) -> list[AdPerformanceDaily]:
        """캠페인과 무관하게 특정 날짜의 전체 광고 성과를 조회한다 (손익 계산엔진용)."""
        stmt = select(AdPerformanceDaily).where(AdPerformanceDaily.stat_date == stat_date)
        return list(self.session.execute(stmt).scalars().all())

    def get_by_campaign_and_date(self, campaign_id: int, stat_date: date) -> Optional[AdPerformanceDaily]:
        """UniqueConstraint(campaign_id, stat_date) 기준 조회 (광고 수집 작업의 upsert용)."""
        stmt = select(AdPerformanceDaily).where(
            AdPerformanceDaily.campaign_id == campaign_id, AdPerformanceDaily.stat_date == stat_date
        )
        return self.session.execute(stmt).scalar_one_or_none()
