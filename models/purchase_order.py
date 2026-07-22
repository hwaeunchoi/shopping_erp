"""
models/purchase_order.py
--------------------------
공급처 발주(자동발주 확장 대상) 관리: purchase_orders, purchase_order_items.

상태 흐름: DRAFT(작성중, 품목 추가/삭제 가능) → ORDERED(발주 확정, 품목 변경 불가)
→ 입고 처리에 따라 PARTIALLY_RECEIVED → RECEIVED(전량 입고 완료).
CANCELLED는 ORDERED 상태에서만 가능(DRAFT는 그냥 삭제).
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, TimestampMixin
from models.supplier import Supplier


class PurchaseOrder(Base, TimestampMixin):
    """공급처 발주 헤더."""

    __tablename__ = "purchase_orders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="DRAFT")
    order_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    memo: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    supplier: Mapped["Supplier"] = relationship()
    items: Mapped[list["PurchaseOrderItem"]] = relationship(
        back_populates="purchase_order", cascade="all, delete-orphan"
    )


class PurchaseOrderItem(Base):
    """발주 품목(상품옵션 단위)."""

    __tablename__ = "purchase_order_items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    purchase_order_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id"), nullable=False)
    product_option_id: Mapped[int] = mapped_column(ForeignKey("product_options.id"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_cost: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    received_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    purchase_order: Mapped["PurchaseOrder"] = relationship(back_populates="items")
