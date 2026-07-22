"""
models/inventory.py
---------------------
ERD 2.4 상품/공급처/재고 그룹 중 재고 관련: warehouses, inventory,
inventory_transactions

SRS 3.11 "향후 확장 기능"에 해당 - 1차 개발 범위는 아니지만 스키마는
지금 확정하여 향후 기능 활성화 시 마이그레이션만으로 대응한다.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class Warehouse(Base):
    """창고. 초기엔 1개만 운영해도 다중 창고 확장을 고려해 별도 테이블로 분리."""

    __tablename__ = "warehouses"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    location: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class InventoryStatus(str, Enum):
    """재고 상태. 물건이 있을 수 있는 곳의 분류.

    주의: Enum 값이 있다고 해서 Inventory에 컬럼이 있는 것은 아니다.
    DISPOSED는 "이동의 목적지"로는 유효하지만 창고에 남지 않으므로 컬럼이 없다
    (설계 원칙: Inventory는 현재 창고에 존재하는 재고만 저장한다).
    """

    SELLABLE = "SELLABLE"  # 판매 가능 - Inventory.sellable_stock
    DEFECTIVE = "DEFECTIVE"  # 불량 보관 - Inventory.defective_stock
    DISPOSED = "DISPOSED"  # 폐기(종착) - 컬럼 없음, 이벤트로만 기록


class Inventory(Base):
    """SKU × 창고 단위 재고 현황.

    설계 원칙(Invariant): **현재 창고에 존재하는 재고만 저장한다.**
    폐기/출고/공급처반송처럼 창고를 떠난 것은 inventory_transactions 이벤트로만 남긴다.

    정의(고정)
      - sellable_stock : 현재 판매 가능한 실재고 총량이며, reserved_stock을 **포함**한다.
      - reserved_stock : sellable_stock 중 이미 주문에 배정된 수량(별도 재고 아님).

    관계식
        0 ≤ reserved_stock ≤ sellable_stock
        즉시 출고 가능 = sellable_stock - reserved_stock - safety_stock
        총 실물재고    = sellable_stock + defective_stock
    """

    __tablename__ = "inventory"
    __table_args__ = (UniqueConstraint("product_option_id", "warehouse_id", name="uq_inventory_option_warehouse"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_option_id: Mapped[int] = mapped_column(ForeignKey("product_options.id"), nullable=False)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), nullable=False)
    # 판매 가능 실재고 총량(reserved_stock 포함). 예약이 잡혀도 이 값은 줄지 않는다.
    sellable_stock: Mapped[int] = mapped_column(default=0, nullable=False)
    # sellable_stock 중 주문에 배정되어 다른 주문이 쓸 수 없는 수량.
    reserved_stock: Mapped[int] = mapped_column(default=0, nullable=False)
    safety_stock: Mapped[int] = mapped_column(default=0, nullable=False)
    # 불량 판정됐으나 아직 창고에 보관 중인 실물.
    defective_stock: Mapped[int] = mapped_column(default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class InventoryTransaction(Base):
    """재고 입출고 이력. 모든 재고 변동은 반드시 이 테이블을 거친다.

    from_status/to_status로 "어느 칸에서 어느 칸으로" 움직였는지를 표현한다.
    NULL은 창고 외부를 뜻한다(입고 유입 / 출고·폐기 유출).
      - to만 있음   : 외부 → 창고 (입고, 검수 유입)
      - from만 있음 : 창고 → 외부 (출고, 공급처 반송)
      - 둘 다 있음  : 창고 내부 칸 이동 (불량↔판매가능, 폐기)
    """

    __tablename__ = "inventory_transactions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_option_id: Mapped[int] = mapped_column(ForeignKey("product_options.id"), nullable=False)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), nullable=False)
    # IN(입고) / OUT(출고) / INSPECT(반품 검수) / ADJUST(수동 조정) / DISPOSE(폐기).
    # InventoryService._derive_type()이 reference_type과 to_status에서 파생하는 값과
    # 일치해야 한다 - 서비스가 만들지 않는 값은 여기에도 적지 않는다.
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[int] = mapped_column(nullable=False)  # 양수/음수
    # InventoryStatus 값 또는 NULL(창고 외부). 값 강제는 서비스 계층에서 수행한다.
    from_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    to_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    reference_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # ORDER/PURCHASE/RETURN/MANUAL
    reference_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    memo: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
