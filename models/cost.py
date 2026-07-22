"""
models/cost.py
----------------
ERD 2.7 비용 그룹: costs

플랫폼 수수료는 원칙적으로 platform_fee_rules로 자동 계산되지만, 별도 조정이
필요한 경우 category=PLATFORM_FEE로 이 테이블에 수동 기록할 수 있다.
"""

from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, ForeignKey, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class Cost(Base):
    """비용. SHIPPING/PACKAGING/PLATFORM_FEE/AD_AGENCY_FEE/RETURN_SHIPPING/ETC"""

    __tablename__ = "costs"
    __table_args__ = (Index("idx_costs_incurred_date", "incurred_date"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    cost_type: Mapped[str] = mapped_column(String(10), nullable=False)  # FIXED/VARIABLE
    platform_id: Mapped[Optional[int]] = mapped_column(ForeignKey("platforms.id"), nullable=True)
    product_option_id: Mapped[Optional[int]] = mapped_column(ForeignKey("product_options.id"), nullable=True)
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id"), nullable=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    incurred_date: Mapped[date] = mapped_column(Date, nullable=False)
    memo: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
