"""
models/ad.py
--------------
ERD 2.8 광고 그룹: ad_campaigns, ad_performance_daily

CPC/CPM/CTR/ROAS는 저장하지 않고 조회 시 계산한다 (파생값 중복 저장 방지).
"""

from datetime import date
from typing import Optional

from sqlalchemy import Boolean, Date, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base


class AdCampaign(Base):
    """광고 캠페인. product_option_id로 상품별 광고 기여도를 분석한다."""

    __tablename__ = "ad_campaigns"
    __table_args__ = (UniqueConstraint("ad_platform_code", "platform_campaign_id", name="uq_ad_campaign"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # naver_search_ad / naver_shopping_ad / coupang_ad / (향후) meta_ads / google_ads / kakao_ad
    ad_platform_code: Mapped[str] = mapped_column(String(30), nullable=False)
    platform_campaign_id: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    product_option_id: Mapped[Optional[int]] = mapped_column(ForeignKey("product_options.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    performances: Mapped[list["AdPerformanceDaily"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )


class AdPerformanceDaily(Base):
    """일별 광고 성과."""

    __tablename__ = "ad_performance_daily"
    __table_args__ = (UniqueConstraint("campaign_id", "stat_date", name="uq_ad_perf_campaign_date"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("ad_campaigns.id"), nullable=False)
    stat_date: Mapped[date] = mapped_column(Date, nullable=False)
    impressions: Mapped[int] = mapped_column(default=0, nullable=False)
    clicks: Mapped[int] = mapped_column(default=0, nullable=False)
    cost: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    conversions: Mapped[int] = mapped_column(default=0, nullable=False)
    conversion_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)

    campaign: Mapped["AdCampaign"] = relationship(back_populates="performances")
