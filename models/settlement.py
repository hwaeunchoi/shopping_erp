"""
models/settlement.py
----------------------
ERD 2.6 정산 그룹: settlements, settlement_details

settlement_details가 orders와 settlements 사이의 다리 역할을 하여
"정산일 기준 손익" 조회를 가능하게 한다.
"""

from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, ForeignKey, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base


class Settlement(Base):
    """플랫폼 정산 - 정산 회차 단위."""

    __tablename__ = "settlements"
    __table_args__ = (Index("idx_settlements_platform_cycle", "platform_id", "settlement_cycle"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    platform_id: Mapped[int] = mapped_column(ForeignKey("platforms.id"), nullable=False)
    # 예: "2026-06-16~2026-06-30"(21자) - VARCHAR(20)이었다가 실운영 PostgreSQL에서
    # StringDataRightTruncation 오류가 발생해 30으로 늘렸다(SQLite는 길이를 강제하지
    # 않아 개발 중에는 드러나지 않았던 결함).
    settlement_cycle: Mapped[str] = mapped_column(String(30), nullable=False)
    scheduled_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    settled_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    expected_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    settled_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    unsettled_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    discrepancy_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # SCHEDULED/PARTIAL/COMPLETED/DISCREPANCY
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    details: Mapped[list["SettlementDetail"]] = relationship(back_populates="settlement", cascade="all, delete-orphan")


class SettlementDetail(Base):
    """정산-주문 매핑 상세."""

    __tablename__ = "settlement_details"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    settlement_id: Mapped[int] = mapped_column(ForeignKey("settlements.id"), nullable=False)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    order_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("order_items.id"), nullable=True)
    gross_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    fee_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    net_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)

    settlement: Mapped["Settlement"] = relationship(back_populates="details")
