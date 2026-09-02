"""
models/settlement.py
----------------------
ERD 2.6 정산 그룹: settlements, settlement_details

settlement_details가 orders와 settlements 사이의 다리 역할을 하여
"정산일 기준 손익" 조회를 가능하게 한다.

상용 ERP 확장(2단계) - 정산 대사(reconciliation) 확장:
- settlement_type: 채널이 정산 회차를 여러 종류로 나누는 경우(예: 쿠팡
  MONTHLY/WEEKLY/ADDITIONAL/RESERVE) 그 종류를 구분한다. 같은 settlement_cycle
  문자열이라도 종류가 다르면 서로 다른 정산 건이므로, upsert 조회 키에
  (platform_id, settlement_cycle, settlement_type)을 함께 쓴다
  (repositories.settlement_repository.SettlementRepository 참고).
- SettlementDetail.sale_type/recognition_date: 채널의 매출인식 기준 원본 필드를
  그대로 보관한다(SALE/REFUND, 인식일) - REFUND 라인은 금액이 음수일 수 있으며
  임의로 부호를 바꾸지 않는다(채널이 준 값 그대로).
- SettlementDiscrepancy: 주문/정산 매칭 실패나 금액 불일치를 자동으로 추정해
  덮어쓰지 않고 별도 상태로 남긴다 - services.settlement_sync_service 참고.

금액은 전부 Decimal(Numeric 컬럼)로 다룬다 - Python 쪽에서 float 연산을 하지 않는다
(대사 계산은 services.settlement_sync_service에서 decimal.Decimal만 사용).
"""

from datetime import date, datetime
from decimal import Decimal
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
    # 채널의 정산 종류(예: 쿠팡 MONTHLY/WEEKLY/ADDITIONAL/RESERVE) - 미제공 채널은 NULL.
    settlement_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    scheduled_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    settled_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    # Decimal(float 아님) - 2단계 대사 계산이 이 필드들을 직접 다룬다(모듈 docstring 참고).
    expected_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    settled_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    unsettled_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    discrepancy_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # SCHEDULED/PARTIAL/COMPLETED/DISCREPANCY
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    details: Mapped[list["SettlementDetail"]] = relationship(back_populates="settlement", cascade="all, delete-orphan")


class SettlementDetail(Base):
    """정산-주문 매핑 상세(revenue-history류 order-level 정산 라인)."""

    __tablename__ = "settlement_details"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    settlement_id: Mapped[int] = mapped_column(ForeignKey("settlements.id"), nullable=False)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    order_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("order_items.id"), nullable=True)
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    fee_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    # SALE/REFUND - 채널이 준 값 그대로(REFUND는 금액이 음수일 수 있다, 부호 재정의 없음).
    sale_type: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    # 채널의 매출인식일(예: 쿠팡 recognitionDate) - 정산 회차 매칭의 근거.
    recognition_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    settlement: Mapped["Settlement"] = relationship(back_populates="details")


class SettlementDiscrepancy(Base):
    """정산 대사 중 발견된 매칭 실패/금액 불일치 - 자동으로 추정해 덮어쓰지 않고
    운영자 확인 대상으로 남긴다(models.integration_sync.OrderStatusConflict와 동일한
    설계 원칙: detected_at/resolved_at/resolved_by/resolution).

    reason: NO_MATCHING_SETTLEMENT(정산 라인이 속할 정산 회차를 찾지 못함) /
    NO_MATCHING_ORDER(정산 라인의 주문번호를 아직 수집하지 못함) /
    AMOUNT_MISMATCH(정산 회차 합계와 상세 합계가 다름).
    """

    __tablename__ = "settlement_discrepancies"
    __table_args__ = (Index("idx_settlement_discrepancies_platform", "platform_id", "resolved_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    platform_id: Mapped[int] = mapped_column(ForeignKey("platforms.id"), nullable=False)
    settlement_id: Mapped[Optional[int]] = mapped_column(ForeignKey("settlements.id"), nullable=True)
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id"), nullable=True)
    reason: Mapped[str] = mapped_column(String(30), nullable=False)
    expected_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    actual_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    diff_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    # 안전한 요약만(주문번호/vendorItemId 등) - 원본 응답 전문·개인정보 없음.
    detail_summary: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    resolution: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
