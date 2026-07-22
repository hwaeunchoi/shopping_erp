"""
services/purchase_order_service.py
--------------------------------------
공급처 발주 업무로직: 작성(DRAFT) → 품목 추가 → 확정(ORDERED) → 입고 처리
(PARTIALLY_RECEIVED/RECEIVED) → 취소(CANCELLED).

입고 처리는 InventoryService.receive_purchase_order()를 통해 실재고를 증가시키고
InventoryTransaction(reference_type=PURCHASE_ORDER) 이력을 남긴다.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from models.purchase_order import PurchaseOrder, PurchaseOrderItem
from repositories.purchase_order_repository import PurchaseOrderItemRepository, PurchaseOrderRepository
from services.inventory_service import InventoryService


class PurchaseOrderStateError(Exception):
    """발주 상태상 허용되지 않는 작업을 시도할 때 발생한다."""


class PurchaseOrderService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.po_repo = PurchaseOrderRepository(session)
        self.item_repo = PurchaseOrderItemRepository(session)

    def create_draft(self, supplier_id: int, memo: Optional[str] = None) -> PurchaseOrder:
        return self.po_repo.add(PurchaseOrder(supplier_id=supplier_id, status="DRAFT", memo=memo))

    def add_item(
        self, purchase_order_id: int, product_option_id: int, quantity: int, unit_cost: float
    ) -> PurchaseOrderItem:
        po = self._get_po(purchase_order_id)
        if po.status != "DRAFT":
            raise PurchaseOrderStateError("DRAFT 상태의 발주에만 품목을 추가할 수 있습니다.")
        return self.item_repo.add(
            PurchaseOrderItem(
                purchase_order_id=purchase_order_id,
                product_option_id=product_option_id,
                quantity=quantity,
                unit_cost=unit_cost,
            )
        )

    def confirm(self, purchase_order_id: int) -> PurchaseOrder:
        po = self._get_po(purchase_order_id)
        if po.status != "DRAFT":
            raise PurchaseOrderStateError("DRAFT 상태의 발주만 확정할 수 있습니다.")
        if not self.item_repo.list_by_purchase_order(purchase_order_id):
            raise PurchaseOrderStateError("품목이 없는 발주는 확정할 수 없습니다.")
        po.status = "ORDERED"
        po.order_date = datetime.now(timezone.utc)
        self.session.flush()
        return po

    def cancel(self, purchase_order_id: int) -> PurchaseOrder:
        po = self._get_po(purchase_order_id)
        if po.status not in ("DRAFT", "ORDERED"):
            raise PurchaseOrderStateError("DRAFT 또는 ORDERED 상태의 발주만 취소할 수 있습니다.")
        po.status = "CANCELLED"
        self.session.flush()
        return po

    def receive_item(self, purchase_order_id: int, item_id: int, quantity: int, warehouse_id: int) -> PurchaseOrderItem:
        po = self._get_po(purchase_order_id)
        if po.status not in ("ORDERED", "PARTIALLY_RECEIVED"):
            raise PurchaseOrderStateError("ORDERED 상태의 발주만 입고 처리할 수 있습니다.")

        item = self.item_repo.get_by_id(item_id)
        if item is None or item.purchase_order_id != purchase_order_id:
            raise ValueError(f"발주 품목을 찾을 수 없습니다: purchase_order_id={purchase_order_id}, item_id={item_id}")

        remaining = item.quantity - item.received_quantity
        if quantity <= 0 or quantity > remaining:
            raise PurchaseOrderStateError(f"입고 수량이 유효하지 않습니다: 요청={quantity}, 남은수량={remaining}")

        InventoryService(self.session).receive_purchase_order(
            item.product_option_id, warehouse_id, quantity, reference_id=purchase_order_id
        )
        item.received_quantity += quantity
        self.session.flush()

        all_items = self.item_repo.list_by_purchase_order(purchase_order_id)
        if all(i.received_quantity >= i.quantity for i in all_items):
            po.status = "RECEIVED"
        else:
            po.status = "PARTIALLY_RECEIVED"
        self.session.flush()
        return item

    def _get_po(self, purchase_order_id: int) -> PurchaseOrder:
        po = self.po_repo.get_by_id(purchase_order_id)
        if po is None:
            raise ValueError(f"발주를 찾을 수 없습니다: id={purchase_order_id}")
        return po
