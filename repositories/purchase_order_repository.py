"""
repositories/purchase_order_repository.py
--------------------------------------------
공급처 발주(purchase_orders, purchase_order_items)에 대한 Repository.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.purchase_order import PurchaseOrder, PurchaseOrderItem
from repositories.base_repository import BaseRepository


class PurchaseOrderRepository(BaseRepository[PurchaseOrder]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, PurchaseOrder)

    def list_by_supplier(self, supplier_id: int) -> list[PurchaseOrder]:
        stmt = select(PurchaseOrder).where(PurchaseOrder.supplier_id == supplier_id)
        return list(self.session.execute(stmt).scalars().all())

    def list_by_status(self, status: str) -> list[PurchaseOrder]:
        stmt = select(PurchaseOrder).where(PurchaseOrder.status == status)
        return list(self.session.execute(stmt).scalars().all())


class PurchaseOrderItemRepository(BaseRepository[PurchaseOrderItem]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, PurchaseOrderItem)

    def list_open_by_product_options(self, product_option_ids: list[int]) -> list[tuple[int, str]]:
        """미완료(발주됨/부분입고) 발주에 걸린 옵션을 (option_id, 발주상태)로 반환한다.

        '발주 필요'와 '입고 대기'를 구분하기 위해 쓴다 - 이미 발주가 걸려 있으면
        발주가 필요한 게 아니라 입고를 기다리는 상태다.
        """
        if not product_option_ids:
            return []
        stmt = (
            select(PurchaseOrderItem.product_option_id, PurchaseOrder.status)
            .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderItem.purchase_order_id)
            .where(
                PurchaseOrderItem.product_option_id.in_(product_option_ids),
                PurchaseOrder.status.in_(("ORDERED", "PARTIALLY_RECEIVED")),
            )
            .distinct()
        )
        return [(row[0], row[1]) for row in self.session.execute(stmt).all()]

    def list_by_purchase_order(self, purchase_order_id: int) -> list[PurchaseOrderItem]:
        stmt = select(PurchaseOrderItem).where(PurchaseOrderItem.purchase_order_id == purchase_order_id)
        return list(self.session.execute(stmt).scalars().all())
