"""
models/order.py
-----------------
ERD 2.5 주문/배송/교환/반품/취소 그룹:
orders, order_items, order_status_history, shipments, exchanges,
returns, cancellations

핵심 제약: orders.(platform_id, platform_order_no) UNIQUE - 중복 수집 방지
(SRS FR-MALL-04). order_date/payment_date/delivery_completed_date 3종을
모두 보유하여 손익 계산 기준 4종(주문일/결제일/배송완료일/정산일) 중
3종의 기반을 제공한다 (정산일은 settlements 테이블에서 관리).
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, SoftDeleteMixin, TimestampMixin


class Order(Base, TimestampMixin, SoftDeleteMixin):
    """주문."""

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("platform_id", "platform_order_no", name="uq_order_platform_no"),
        Index("idx_orders_date", "order_date"),
        Index("idx_orders_status", "status"),
        Index("idx_orders_platform_date", "platform_id", "order_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    platform_id: Mapped[int] = mapped_column(ForeignKey("platforms.id"), nullable=False)
    platform_order_no: Mapped[str] = mapped_column(String(100), nullable=False)
    customer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("customers.id"), nullable=True)
    # NEW/PREPARING/SHIPPING/DELIVERED/CANCELED/EXCHANGED/RETURNED/REFUNDED
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    order_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    payment_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    delivery_completed_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    total_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    discount_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    # 주문 워크벤치(허브) 재설계: 담당자 배정 + 운영 태그(쉼표 구분).
    assignee_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    tags: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    # --- 업무 설계 확정 반영 -------------------------------------------------
    # 통화: 지금은 KRW 고정이지만 컬럼을 미리 둔다. 나중에 넣으면 금액을 다루는
    # 모든 테이블과 계산 로직을 전면 수정해야 한다(구조적 위험 대비).
    currency: Mapped[str] = mapped_column(String(3), default="KRW", nullable=False)
    # 주문 출처: CHANNEL(쇼핑몰 수집) / MANUAL(수기) / B2B.
    # 채널 없는 주문을 나중에 허용하려면 조회·집계 쿼리를 전부 재검토해야 하므로 미리 둔다.
    order_source: Mapped[str] = mapped_column(String(20), default="CHANNEL", nullable=False)

    # 주문확인 단계 - 이 시점에 재고를 선점한다(재고 중복판매 방지).
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    confirmed_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)

    # 수취인 - 구매자와 다를 수 있다(선물 주문). 합포장 판정의 기준이기도 하다.
    receiver_name: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    receiver_phone: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    receiver_zipcode: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    receiver_address: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # 배송 요청 메세지(예: "문 앞에 놓아주세요") - 채널 주문 수집 시 배송지 정보와 함께 저장.
    delivery_message: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan")
    status_history: Mapped[list["OrderStatusHistory"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    # 배송은 shipment_items를 통해 N:M으로 연결된다(합포장/분할배송). 직접 관계를 두지 않고
    # ShipmentRepository.list_by_order()로 조회한다.


class OrderItem(Base):
    """주문상품. cost_price_snapshot은 매출 확정 시점에 원가를 스냅샷으로 고정한 값."""

    __tablename__ = "order_items"
    # 같은 주문 안에서 상품주문번호는 유일하다(NULL은 여러 개 허용 - 상품주문번호 미제공
    # 채널/과거 데이터). 유니크 "인덱스"로 구현: PostgreSQL·SQLite 모두 NULL 다중 허용 +
    # SQLite 테이블 재생성(이름 없는 FK 충돌) 회피 + 마이그레이션과 동일 형태.
    __table_args__ = (Index("uq_order_item_platform_no", "order_id", "platform_order_item_no", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    product_option_id: Mapped[int] = mapped_column(ForeignKey("product_options.id"), nullable=False)
    # 쇼핑몰 상품주문번호(라인 단위 외부 식별자) - 네이버 productOrderId 등. 발주확인·송장·
    # 클레임의 핵심 라인 식별자. 문자열(선행0/형식변경 대비), 빈 문자열은 NULL로 정규화.
    # 유일성: UNIQUE(order_id, platform_order_item_no)(NULL 허용). 상품주문번호를 제공하지
    # 않는 채널/과거 데이터는 NULL이며 SKU 기반 호환 로직으로 처리한다.
    # TODO(SaaS): 멀티테넌시 전환 시 company_id + 판매자 계정 식별자를 포함한 유일성 재설계.
    platform_order_item_no: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    quantity: Mapped[int] = mapped_column(nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    cost_price_snapshot: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    line_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)

    # --- 세트상품 분해 추적 --------------------------------------------------
    # 이 품목이 어느 채널상품에서 분해되어 나왔는가. 세트 "3종세트 2개"는
    # 구성 SKU 3행으로 저장되며, 세 행 모두 같은 channel_product_id를 갖는다.
    channel_product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("channel_products.id"), nullable=True)
    # 채널에서 주문된 세트 개수(구성수량 × set_quantity = quantity)
    set_quantity: Mapped[Optional[int]] = mapped_column(nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="KRW", nullable=False)
    # 쿠팡 배송묶음 ID(shipmentBoxId) - 송장 전송 API에 orderId/vendorItemId와 함께 필요하다.
    # Order가 아니라 OrderItem(라인)에 둔 이유: 쿠팡은 배송묶음(box) 단위로 응답을 주고
    # (한 주문이 여러 배송묶음으로 나뉠 수 있음 - CoupangConnector 모듈 docstring 참고),
    # 각 라인아이템(vendorItemId)은 그 라인이 속한 배송묶음 하나에만 속한다. 주문 전체에
    # 대표값 하나만 저장하면 분할배송 주문의 두 번째 이후 배송묶음 라인은 잘못된(첫 배송묶음의)
    # box id로 송장이 전송되는 오류가 생긴다. 네이버 등 배송묶음 개념이 없는 채널은 NULL.
    platform_shipment_box_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    order: Mapped["Order"] = relationship(back_populates="items")


class OrderStatusHistory(Base):
    """주문 상태 변경 이력."""

    __tablename__ = "order_status_history"
    __table_args__ = (Index("idx_order_status_history", "order_id", "changed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    from_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    order: Mapped["Order"] = relationship(back_populates="status_history")


class Shipment(Base):
    """배송(택배 1건 = 송장 1장).

    이지어드민 수준의 합포장/분할배송을 지원하기 위해 주문과 1:1이 아니라
    shipment_items를 통해 N:M으로 연결한다.

    - 합포장: 같은 수취인의 주문 여러 건 → shipment 1건(송장 1장)
      (shipment_items에 order_id가 여러 개 붙는다)
    - 분할배송: 주문 1건을 나눠 발송 → shipment 여러 건
      (같은 order_id가 여러 shipment의 shipment_items에 붙는다)

    과거에는 shipments.order_id에 UNIQUE 제약이 있어 둘 다 구조적으로 불가능했다.
    """

    __tablename__ = "shipments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    carrier: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    tracking_no: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    shipped_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # READY/SHIPPING/DELIVERED

    items: Mapped[list["ShipmentItem"]] = relationship(back_populates="shipment", cascade="all, delete-orphan")


class ShipmentItem(Base):
    """배송↔주문 연결(N:M 해소). order_item_id가 NULL이면 '주문 전체'를 의미한다.

    분할배송에서 특정 품목만 먼저 보낼 때 order_item_id/quantity를 채운다.
    """

    __tablename__ = "shipment_items"
    __table_args__ = (
        Index("idx_shipment_items_shipment", "shipment_id"),
        Index("idx_shipment_items_order", "order_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    shipment_id: Mapped[int] = mapped_column(ForeignKey("shipments.id"), nullable=False)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    order_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("order_items.id"), nullable=True)
    quantity: Mapped[Optional[int]] = mapped_column(nullable=True)

    shipment: Mapped["Shipment"] = relationship(back_populates="items")


# 상용 ERP 확장(2단계) - 채널 클레임(취소/반품/교환) 수집 공통 필드.
# platform_claim_id: 채널이 발급한 클레임 고유 ID(쿠팡 receiptId/exchangeId 등) -
#   같은 주문에 같은 유형의 클레임이 여러 건이어도(부분 클레임) 구분하고, 재수집 시
#   새 행을 또 만들지 않고 기존 행을 갱신하기 위한 키다. 채널이 고유 ID를 주지 않으면
#   추측해서 채우지 않고 NULL로 둔다(services.claim_sync_service 참고 - 이 경우
#   "주문+유형" 단위의 보수적 중복방지로 폴백한다).
# raw_status: 채널이 준 원본 상태 코드(예: 쿠팡 receiptStatus/exchangeStatus) -
#   내부 정규화 상태(status)와 분리 보관해, 정규화 매핑이 나중에 바뀌어도 원본을
#   다시 참조할 수 있게 한다. status="REVIEW"는 원본 코드가 알려진 매핑에 없어(또는
#   재수집 시 상태가 뒤로 후퇴해) 자동으로 완료/특정 상태로 단정하지 않고 운영자
#   확인이 필요함을 뜻한다(services.claim_state_machine 참고).
# quantity/shipping_fee: 채널이 제공하는 경우에만 채운다(제공하지 않으면 NULL -
#   0이나 임의값으로 추정하지 않는다).
class Exchange(Base):
    """교환."""

    __tablename__ = "exchanges"
    __table_args__ = (Index("uq_exchange_order_claim_id", "order_id", "platform_claim_id", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    order_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("order_items.id"), nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    # REQUESTED/APPROVED/SHIPPED/COMPLETED/REJECTED(+동기화 전용 REVIEW - 위 설명 참고)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    platform_claim_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    raw_status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    quantity: Mapped[Optional[int]] = mapped_column(nullable=True)
    shipping_fee: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    # 귀책 주체 - 채널이 제공하는 경우에만(예: 쿠팡 교환 faultType: COUPANG/VENDOR/
    # CUSTOMER/WMS/GENERAL 원본값 그대로 저장, 내부 재정의 없음).
    fault_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)


class Return(Base):
    """반품."""

    __tablename__ = "returns"
    __table_args__ = (Index("uq_return_order_claim_id", "order_id", "platform_claim_id", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    order_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("order_items.id"), nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    refund_amount: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    # REQUESTED/APPROVED/RECEIVED/REFUNDED/REJECTED(+동기화 전용 REVIEW)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    platform_claim_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    raw_status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    quantity: Mapped[Optional[int]] = mapped_column(nullable=True)
    shipping_fee: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    # 귀책 주체 - 반품도 채널이 제공하는 경우가 있다(예: 쿠팡 반품 응답의 faultByType).
    fault_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)


class Cancellation(Base):
    """취소."""

    __tablename__ = "cancellations"
    __table_args__ = (Index("uq_cancellation_order_claim_id", "order_id", "platform_claim_id", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    # 취소는 기존에 주문 라인 연결이 없었다(주문 전체 취소만 가정) - 상용 ERP 확장(2단계)에서
    # 라인 단위 부분취소를 표현할 수 있도록 반품/교환과 동일하게 추가한다(NULL이면 주문 전체).
    order_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("order_items.id"), nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    refund_amount: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    # REQUESTED/COMPLETED(+동기화 전용 REVIEW)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    platform_claim_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    raw_status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    quantity: Mapped[Optional[int]] = mapped_column(nullable=True)
    shipping_fee: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    fault_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)


class ClaimUnmatched(Base):
    """아직 수집되지 않은 주문(platform_order_no로 매칭 실패)의 클레임을 조용히 버리지
    않고 보존한다 - order_collect_job이 나중에 그 주문을 수집하면, 다음 클레임
    재수집 시(ClaimSyncService._resolve_pending_unmatched) 실제 Cancellation/Return/
    Exchange 행으로 승격되고 이 행은 resolved_at이 채워진다.

    원본 응답 전체는 저장하지 않는다(보존정책 승인 전) - 재매칭에 필요한 최소 필드만
    담는다. reason은 안전하게 잘라 저장한다(개인정보 원문 없음).
    """

    __tablename__ = "claim_unmatched_items"
    __table_args__ = (
        Index(
            "uq_claim_unmatched_key", "platform_id", "claim_type", "platform_claim_id", "platform_order_no", unique=True
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    platform_id: Mapped[int] = mapped_column(ForeignKey("platforms.id"), nullable=False)
    claim_type: Mapped[str] = mapped_column(String(20), nullable=False)  # CANCELLATION/RETURN/EXCHANGE
    platform_claim_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    platform_order_no: Mapped[str] = mapped_column(String(100), nullable=False)
    raw_status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    resolved_entity_id: Mapped[Optional[int]] = mapped_column(nullable=True)


class ClaimCollectionCursor(Base):
    """ "후보 주문" 기반 클레임 조회(예: 쿠팡 취소 - orderId 단건 조회만 가능해 기간
    대량조회가 안 되는 채널)가 매 실행마다 전체 주문을 무제한 순회하지 않도록 진행
    위치를 저장한다. platform_id + claim_type(예: "CANCELLATION_ORDER_LOOKUP") 단위로
    1행 - Order.id 오름차순으로 last_order_id 다음부터 최대 요청 수만큼 조회하고,
    끝에 도달하면 처음(0)부터 다시 순회한다(회전식 - 한 번에 다 못 봐도 시간이
    지나면 결국 전체 후보를 한 바퀴 훑는다). services.claim_sync_service 참고."""

    __tablename__ = "claim_collection_cursors"
    __table_args__ = (Index("uq_claim_collection_cursor", "platform_id", "claim_type", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    platform_id: Mapped[int] = mapped_column(ForeignKey("platforms.id"), nullable=False)
    claim_type: Mapped[str] = mapped_column(String(30), nullable=False)
    last_order_id: Mapped[int] = mapped_column(nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
