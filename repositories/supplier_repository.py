"""
repositories/supplier_repository.py
--------------------------------------
ERD 2.4 상품/공급처/재고 그룹 중 공급처 관련(suppliers, supplier_contacts,
product_supplier_map)에 대한 Repository.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.supplier import ProductSupplierMap, Supplier, SupplierContact
from repositories.base_repository import BaseRepository


class SupplierRepository(BaseRepository[Supplier]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Supplier)

    def list_active(self) -> list[Supplier]:
        stmt = select(Supplier).where(Supplier.is_active.is_(True))
        return list(self.session.execute(stmt).scalars().all())


class SupplierContactRepository(BaseRepository[SupplierContact]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, SupplierContact)

    def list_by_supplier(self, supplier_id: int) -> list[SupplierContact]:
        stmt = select(SupplierContact).where(SupplierContact.supplier_id == supplier_id)
        return list(self.session.execute(stmt).scalars().all())


class ProductSupplierMapRepository(BaseRepository[ProductSupplierMap]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, ProductSupplierMap)

    def list_by_product_options(self, product_option_ids: list[int]) -> list[ProductSupplierMap]:
        """여러 옵션의 공급처 매핑을 한 번에 조회한다(막힌 사유 판정 N+1 방지)."""
        if not product_option_ids:
            return []
        stmt = select(ProductSupplierMap).where(ProductSupplierMap.product_option_id.in_(product_option_ids))
        return list(self.session.execute(stmt).scalars().all())

    def list_by_product_option(self, product_option_id: int) -> list[ProductSupplierMap]:
        stmt = select(ProductSupplierMap).where(ProductSupplierMap.product_option_id == product_option_id)
        return list(self.session.execute(stmt).scalars().all())

    def list_by_supplier(self, supplier_id: int) -> list[ProductSupplierMap]:
        stmt = select(ProductSupplierMap).where(ProductSupplierMap.supplier_id == supplier_id)
        return list(self.session.execute(stmt).scalars().all())

    def get_by_option_and_supplier(self, product_option_id: int, supplier_id: int) -> ProductSupplierMap | None:
        stmt = select(ProductSupplierMap).where(
            ProductSupplierMap.product_option_id == product_option_id, ProductSupplierMap.supplier_id == supplier_id
        )
        return self.session.execute(stmt).scalar_one_or_none()
