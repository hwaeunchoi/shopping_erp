"""
models/fulfillment.py
------------------------
상용 ERP 확장(5단계, A묶음) - 출고 배치(피킹/검수/포장) + 기존
Shipment/ShipmentDispatchService 연계.

세 가지를 절대 하나의 상태로 합치지 않는다(그렇게 하면 채널 전송이 실패/
UNKNOWN인데 화면엔 이미 "완료"로 보이는 오인 표시가 생긴다):
1. "창고에서 물리적으로 무엇을 했는가" - 이 모듈(FulfillmentBatchItem.status).
2. "채널에 실제로 전달됐는가" - models.integration_sync.ExternalCommand
   (command_type="SHIPMENT_SUBMIT", target_type="SHIPMENT") - 그대로 재사용.
3. "주문이 배송 중으로 확정됐는가" - models.order.Order.status - 여전히
   services.shipment_dispatch_service.ShipmentDispatchService.
   _sync_channel_status_after_success()가 라인별 발송 수량 합계 기준으로만
   전담한다(부분출고 중인 주문은 여기서 건드리지 않는다) - 이 모듈은
   Order.status를 직접 바꾸지 않는다.

FulfillmentBatchItem은 항상 정확히 하나의 OrderItem(주문 라인)을 가리킨다 -
서로 다른 주문의 라인이 한 항목에 섞이지 않는다. 같은 라인을 여러 배치/여러
shipment로 나눠 부분출고할 수 있다 - 이미 다른 배치(취소되지 않은)에 배정된
수량은 잔여 수량 계산에서 제외한다(services.fulfillment_service 참고).

재고 차감 시점: 검수완료가 아니라 "포장완료"(PACKED 전이) 시점에 정확히
한 번 차감한다 - inventory_deducted_at으로 멱등을 보장하고,
services.inventory_service.InventoryService.deduct_on_shipment()을 그대로
재사용한다(새 재고 이동 로직을 만들지 않는다).
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, TimestampMixin

# --- 배치 전체 상태(요약용 - 개별 진행은 FulfillmentBatchItem.status가 더 정확하다) ---
BATCH_STATUSES = ("READY", "IN_PROGRESS", "COMPLETED", "CANCELLED")

# --- 배치 항목(라인) 상태 - services.fulfillment_state_machine.ALLOWED_TRANSITIONS가
# 허용 전이를 정의한다. 이 모듈에는 값 목록만 둔다(순환 import 방지). ---
BATCH_ITEM_STATUSES = (
    "READY",
    "PICKING",
    "PICKED",
    "VERIFYING",
    "VERIFIED",
    "PACKED",
    "SUBMIT_PENDING",
    "SUBMITTED",
    "BLOCKED",
    "CANCELLED",
)


class FulfillmentBatch(Base, TimestampMixin):
    """출고 배치 - 한 번에 피킹하러 나가는 작업 묶음. 실제 상태 관리는 라인
    단위(FulfillmentBatchItem)에서 하고, 이 테이블의 status는 목록 화면에서
    "이 배치가 대략 어느 단계인가"를 보여주는 요약값이다."""

    __tablename__ = "fulfillment_batches"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="READY", nullable=False)
    note: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)

    items: Mapped[list["FulfillmentBatchItem"]] = relationship(back_populates="batch", cascade="all, delete-orphan")


class FulfillmentBatchItem(Base, TimestampMixin):
    """출고 배치에 속한 라인 하나 - 정확히 하나의 OrderItem(주문상품)과
    정확히 하나의 수량을 담당한다. order_id는 order_item.order_id에서
    파생되며(서비스 계층이 생성 시 검증) 조회 편의를 위해 비정규화해 둔다.

    requested_quantity: 이 배치가 처리하기로 한 수량(생성 시 확정, 이후 불변).
    picked_quantity: 피킹 완료 시 실제로 집어온 수량(검수 대상).
    verified_quantity: 검수 확정 수량 - picked_quantity와 다르면(수량 불일치)
        VERIFIED로 전이하지 않고 BLOCKED로 남긴다(services.fulfillment_service.
        FulfillmentService.verify 참고) - 추측으로 맞다고 넘어가지 않는다.
    inventory_deducted_at: 재고 차감을 정확히 한 번만 수행했는지의 멱등 표식
        (포장완료 전이 시 채워진다) - 재처리/재조회로 두 번 차감되는 것을 막는다.
    shipment_id: 포장완료 시 생성/재사용된 배송(송장) 레코드 - 같은 송장번호로
        묶인 여러 배치 항목이 같은 shipment_id를 공유할 수 있다(합포장과 동일한
        models.order.Shipment 구조를 그대로 쓴다 - 새 송장 테이블을 만들지 않는다).
    """

    __tablename__ = "fulfillment_batch_items"
    __table_args__ = (
        Index("idx_fulfillment_batch_items_batch", "batch_id"),
        Index("idx_fulfillment_batch_items_order_item", "order_item_id"),
        Index("idx_fulfillment_batch_items_shipment", "shipment_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("fulfillment_batches.id"), nullable=False)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    order_item_id: Mapped[int] = mapped_column(ForeignKey("order_items.id"), nullable=False)
    product_option_id: Mapped[int] = mapped_column(ForeignKey("product_options.id"), nullable=False)

    requested_quantity: Mapped[int] = mapped_column(nullable=False)
    picked_quantity: Mapped[Optional[int]] = mapped_column(nullable=True)
    verified_quantity: Mapped[Optional[int]] = mapped_column(nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="READY", nullable=False)
    failure_reason_code: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    picked_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    picked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    verified_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    packed_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    packed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    inventory_deducted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    shipment_id: Mapped[Optional[int]] = mapped_column(ForeignKey("shipments.id"), nullable=True)

    batch: Mapped["FulfillmentBatch"] = relationship(back_populates="items")
    history: Mapped[list["FulfillmentBatchItemHistory"]] = relationship(
        back_populates="batch_item", cascade="all, delete-orphan"
    )


class FulfillmentBatchItemHistory(Base):
    """출고 작업 이력 - models.order.OrderStatusHistory와 동일 원칙(append-only,
    누가/언제/무엇을 근거로 남긴다). note는 안전한 짧은 텍스트만 담는다(원본
    응답/Secret/PII 금지 - 기존 화이트리스트 정책과 동일)."""

    __tablename__ = "fulfillment_batch_item_history"
    __table_args__ = (Index("idx_fulfillment_batch_item_history", "batch_item_id", "changed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_item_id: Mapped[int] = mapped_column(ForeignKey("fulfillment_batch_items.id"), nullable=False)
    from_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    changed_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    note: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)

    batch_item: Mapped["FulfillmentBatchItem"] = relationship(back_populates="history")
