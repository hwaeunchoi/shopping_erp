"""
repositories/inventory_repository.py
----------------------------------------
ERD 2.4 상품/공급처/재고 그룹 중 재고 관련(warehouses, inventory)에 대한 Repository.
"""

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models.inventory import Inventory, InventoryTransaction, Warehouse
from repositories.base_repository import BaseRepository


class WarehouseRepository(BaseRepository[Warehouse]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Warehouse)

    def list_active(self) -> list[Warehouse]:
        stmt = select(Warehouse).where(Warehouse.is_active.is_(True))
        return list(self.session.execute(stmt).scalars().all())


class InventoryRepository(BaseRepository[Inventory]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Inventory)

    def get_by_option_and_warehouse(self, product_option_id: int, warehouse_id: int) -> Optional[Inventory]:
        stmt = select(Inventory).where(
            Inventory.product_option_id == product_option_id, Inventory.warehouse_id == warehouse_id
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def list_below_safety_stock(self) -> list[Inventory]:
        stmt = select(Inventory).where(Inventory.sellable_stock < Inventory.safety_stock)
        return list(self.session.execute(stmt).scalars().all())

    def list_by_options(self, product_option_ids: list[int]) -> list[Inventory]:
        """여러 옵션의 재고를 한 번에 조회한다(막힌 사유 판정 N+1 방지)."""
        if not product_option_ids:
            return []
        stmt = select(Inventory).where(Inventory.product_option_id.in_(product_option_ids))
        return list(self.session.execute(stmt).scalars().all())

    def list_by_option(self, product_option_id: int) -> list[Inventory]:
        stmt = select(Inventory).where(Inventory.product_option_id == product_option_id)
        return list(self.session.execute(stmt).scalars().all())

    def total_stock_by_option(self, product_option_id: int) -> int:
        """상품 상세 화면 통계용: 창고 전체에 걸친 이 옵션의 현재 재고 합계."""
        stmt = select(func.coalesce(func.sum(Inventory.sellable_stock), 0)).where(
            Inventory.product_option_id == product_option_id
        )
        return int(self.session.execute(stmt).scalar_one())


class InventoryTransactionRepository(BaseRepository[InventoryTransaction]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, InventoryTransaction)

    def list_by_option_and_warehouse(
        self, product_option_id: int, warehouse_id: int, limit: int = 50
    ) -> list[InventoryTransaction]:
        """재고관리 화면의 입출고 이력 조회 - 최신순."""
        stmt = (
            select(InventoryTransaction)
            .where(
                InventoryTransaction.product_option_id == product_option_id,
                InventoryTransaction.warehouse_id == warehouse_id,
            )
            .order_by(InventoryTransaction.created_at.desc())
            .limit(limit)
        )
        return list(self.session.execute(stmt).scalars().all())
