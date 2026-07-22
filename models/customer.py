"""
models/customer.py
--------------------
ERD 2.3 고객 그룹: customers

SRS FR-USER 관련 아님, CRM(향후 확장) 최소 구조 - v1.2 6장 반영.
total_purchase_amount/order_count/last_order_at는 조회 성능을 위한
의도적 비정규화 캐시 컬럼이며, 주문 확정/취소 시 서비스 레이어가 갱신한다.
first_order_at/is_dormant는 v1.2에서 재구매율·휴면고객 판별을 위해 추가.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, SoftDeleteMixin, TimestampMixin


class Customer(Base, TimestampMixin, SoftDeleteMixin):
    """플랫폼별 고객. 동일인이라도 플랫폼마다 별도 레코드로 관리한다."""

    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("platform_id", "platform_customer_key", name="uq_customer_platform_key"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    platform_id: Mapped[int] = mapped_column(ForeignKey("platforms.id"), nullable=False)
    platform_customer_key: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    address: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # CRM 캐시/통계 컬럼 (배치가 주기적으로 갱신)
    grade: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # 일반/우수/VIP
    is_vip: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    total_purchase_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    order_count: Mapped[int] = mapped_column(default=0, nullable=False)
    first_order_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # v1.2
    last_order_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_dormant: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # v1.2 휴면고객 배치 갱신

    platform: Mapped["Platform"] = relationship()  # type: ignore[name-defined]  # 순환참조 방지용 지연 문자열 참조 (models.platform.Platform)
