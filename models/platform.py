"""
models/platform.py
--------------------
ERD 2.2 플랫폼 그룹: platforms, platform_fee_rules

SRS FR-MALL-01/02, FR-COST-03 대응.
"""

from datetime import date
from typing import Optional

from sqlalchemy import Boolean, Date, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, TimestampMixin


class Platform(Base, TimestampMixin):
    """판매 플랫폼 (네이버 스마트스토어, 쿠팡, ESM, 11번가, 카카오쇼핑 등).

    connector_class: integrations/malls/ 하위 커넥터 클래스명과 매핑되어
    스케줄러가 플러그인을 동적으로 로딩할 때 사용한다.
    """

    __tablename__ = "platforms"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    connector_class: Mapped[str] = mapped_column(String(100), nullable=False)
    settlement_cycle_days: Mapped[Optional[int]] = mapped_column(nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    fee_rules: Mapped[list["PlatformFeeRule"]] = relationship(back_populates="platform")


class PlatformFeeRule(Base):
    """플랫폼 수수료율 이력. effective_from/to로 시점별 요율을 추적한다."""

    __tablename__ = "platform_fee_rules"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    platform_id: Mapped[int] = mapped_column(ForeignKey("platforms.id"), nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    fee_rate: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    platform: Mapped["Platform"] = relationship(back_populates="fee_rules")
