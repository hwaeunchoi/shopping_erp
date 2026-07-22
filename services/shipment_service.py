"""
services/shipment_service.py
--------------------------------
배송(shipment) 등록/조회/상태 변경 업무로직. SRS FR-ORD-01/02 대응.

배송은 shipment_items를 통해 주문과 N:M으로 연결된다(합포장/분할배송).
- 합포장: consolidate()로 주문 여러 건을 배송 1건(송장 1장)에 묶는다
- 분할배송: 같은 주문에 배송을 여러 건 만든다(allow_split=True)

create()는 기본적으로 주문당 1건만 허용해 실수로 중복 송장이 생기는 것을 막고,
분할배송이 의도된 경우에만 allow_split=True로 우회한다.

배송 상태가 SHIPPING/DELIVERED로 바뀔 때는 연결된 주문의 status도 같은
값으로 맞추고(orders.status에 이미 SHIPPING/DELIVERED 값이 있음), 이 때
재고 차감/주문 상태 이력 기록이 함께 필요하므로 OrderSyncService의
apply_status_change()를 그대로 재사용한다 - 커넥터 수집 경로(sync_orders)와
배송관리 화면에서 수동으로 상태를 바꾸는 경로가 동일한 로직을 타도록 하기
위함이다(로직 중복/드리프트 방지).
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from models.order import Shipment
from repositories.order_repository import OrderRepository, ShipmentRepository
from services.order_sync_service import OrderSyncService

VALID_SHIPMENT_STATUSES = {"READY", "SHIPPING", "DELIVERED"}

# shipments.status -> orders.status로 동기화할 때 대응되는 값.
# READY는 주문이 아직 배송준비 단계일 뿐이라 orders.status를 별도로 바꾸지
# 않는다(신규 주문 생성 시점에 이미 NEW/PREPARING으로 관리되고 있음).
SHIPMENT_TO_ORDER_STATUS = {"SHIPPING": "SHIPPING", "DELIVERED": "DELIVERED"}


class ShipmentAlreadyExistsError(Exception):
    """이미 배송 레코드가 있는 주문에 새로 등록하려 할 때 발생한다."""


class InvalidShipmentStatusError(Exception):
    """허용되지 않는 status 값으로 변경하려 할 때 발생한다."""


class ShipmentService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.order_repo = OrderRepository(session)
        self.shipment_repo = ShipmentRepository(session)
        self.order_sync_service = OrderSyncService(session)

    def create(
        self, order_id: int, carrier: Optional[str], tracking_no: Optional[str], allow_split: bool = False
    ) -> Shipment:
        """주문에 배송(송장)을 만든다. allow_split=True면 분할배송으로 추가 생성한다."""
        if not allow_split and self.shipment_repo.get_by_order(order_id) is not None:
            raise ShipmentAlreadyExistsError(f"이미 배송 레코드가 있는 주문입니다: order_id={order_id}")
        shipment = self.shipment_repo.add(Shipment(carrier=carrier, tracking_no=tracking_no, status="READY"))
        self.shipment_repo.link_order(shipment.id, order_id)
        return shipment

    def consolidate(self, order_ids: list[int], carrier: Optional[str], tracking_no: Optional[str]) -> Shipment:
        """합포장 - 주문 여러 건을 배송 1건(송장 1장)으로 묶는다.

        이미 배송이 있는 주문이 섞여 있으면 거부한다(중복 송장 방지).
        """
        if not order_ids:
            raise ValueError("합포장할 주문이 없습니다.")
        already = [oid for oid in order_ids if self.shipment_repo.get_by_order(oid) is not None]
        if already:
            raise ShipmentAlreadyExistsError(f"이미 배송이 있는 주문이 포함되어 있습니다: {already}")

        shipment = self.shipment_repo.add(Shipment(carrier=carrier, tracking_no=tracking_no, status="READY"))
        for oid in order_ids:
            self.shipment_repo.link_order(shipment.id, oid)
        return shipment

    def update_info(
        self, shipment: Shipment, carrier: Optional[str] = None, tracking_no: Optional[str] = None
    ) -> Shipment:
        """운송사/송장번호만 갱신한다(상태 변경은 change_status()를 사용)."""
        if carrier is not None:
            shipment.carrier = carrier
        if tracking_no is not None:
            shipment.tracking_no = tracking_no
        self.session.flush()
        return shipment

    def change_status(self, shipment: Shipment, new_status: str, warehouse_id: Optional[int] = None) -> Shipment:
        if new_status not in VALID_SHIPMENT_STATUSES:
            raise InvalidShipmentStatusError(f"허용되지 않는 배송 상태입니다: {new_status}")

        now = datetime.now(timezone.utc)
        shipment.status = new_status
        if new_status == "SHIPPING" and shipment.shipped_at is None:
            shipment.shipped_at = now
        if new_status == "DELIVERED" and shipment.delivered_at is None:
            shipment.delivered_at = now

        order_status = SHIPMENT_TO_ORDER_STATUS.get(new_status)
        if order_status is not None:
            # 합포장이면 이 송장에 묶인 주문이 여러 건이므로 전부 동기화한다.
            for order_id in self.shipment_repo.list_orders_of_shipment(shipment.id):
                order = self.order_repo.get_by_id(order_id)
                if order is not None and order.status != order_status:
                    self.order_sync_service.apply_status_change(order, order_status, warehouse_id)

        self.session.flush()
        return shipment

    def get(self, shipment_id: int) -> Optional[Shipment]:
        return self.shipment_repo.get_by_id(shipment_id)

    def list(
        self,
        status: Optional[str] = None,
        order_id: Optional[int] = None,
        search: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Shipment], int]:
        offset = (page - 1) * page_size
        items = self.shipment_repo.list_filtered(
            status=status, order_id=order_id, search=search, limit=page_size, offset=offset
        )
        total = self.shipment_repo.count_filtered(status=status, order_id=order_id, search=search)
        return items, total
