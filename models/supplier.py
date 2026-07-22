"""
models/supplier.py
--------------------
ERD 2.4 상품/공급처/재고 그룹 중 공급처 관련: suppliers, supplier_contacts,
product_supplier_map

초기 구현 범위는 상품관리에서의 매핑 정도이나, 스키마는 자동발주 확장까지
고려하여 확정한다.
"""

from typing import Optional

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, TimestampMixin


class Supplier(Base, TimestampMixin):
    """공급처(매입처)."""

    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    business_no: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    bank_name: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    bank_account_no: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    bank_account_holder: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    payment_terms: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    contacts: Mapped[list["SupplierContact"]] = relationship(back_populates="supplier", cascade="all, delete-orphan")


class SupplierContact(Base):
    """공급처 담당자. 공급처 1건에 담당자 여러 명 등록 가능."""

    __tablename__ = "supplier_contacts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    supplier: Mapped["Supplier"] = relationship(back_populates="contacts")


class ProductSupplierMap(Base):
    """상품옵션(SKU) ↔ 공급처 매핑 (N:M 해소 테이블). 자동발주 시 대상 공급처 판단 근거."""

    __tablename__ = "product_supplier_map"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_option_id: Mapped[int] = mapped_column(ForeignKey("product_options.id"), nullable=False)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    supplier: Mapped["Supplier"] = relationship()
