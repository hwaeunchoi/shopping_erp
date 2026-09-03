"""
models/integration_sync.py
------------------------------
상용 ERP(사방넷/플레이오토/셀메이트급) 확장의 공통 기반: 외부 채널 쓰기
명령(outbox)과 내부/채널 주문상태 충돌 기록.

ExternalCommand는 "네이버에 송장을 보낸다"처럼 실패할 수 있고 재시도가
필요한 모든 외부 쓰기 작업의 공통 이력이다. 개별 도메인 테이블(Shipment 등)의
상태만으로는 "몇 번 시도했는지, 다음 재시도가 언제인지, 왜 실패했는지"를
안전하게(Secret/PII 노출 없이) 추적할 수 없어 별도 테이블로 분리했다.

idempotency_key는 호출부가 결정론적으로 만든다(예: f"SHIPMENT_SUBMIT:{shipment_id}:
{tracking_no}") - 같은 키로 다시 요청이 들어오면 새 외부 호출을 만들지 않고
기존 레코드를 재사용한다(동일 송장 재전송 시 중복 API 호출 방지).

결과 불명(UNKNOWN) 처리(1단계 완결 검토 반영): timeout/연결 중 끊김/응답 파싱 실패처럼
"채널이 실제로 처리했는지 알 수 없는" 실패는 RETRY_WAIT으로 자동 재시도하지 않고
UNKNOWN으로 분리한다 - 채널의 공식 멱등성 보장이 확인되지 않은 상태에서 자동
재전송하면 채널이 이미 처리한 요청을 중복 전송할 위험이 있다(services.
shipment_dispatch_service._classify_write_outcome 참고). UNKNOWN은 운영자가 채널을
직접 확인해 resolve_unknown_command()로 해소해야 벗어날 수 있다.

동시 실행 방지(lease): lease_token은 워커가 이 명령을 RUNNING으로 원자적으로 선점
(claim)할 때 발급하는 소유권 토큰이다 - claim/최종상태 반영 모두 "lease_token이
아직 내 것일 때만" DB에서 원자적으로 UPDATE하도록 구현해(ExternalCommandRepository.
claim/try_transition) 두 worker가 같은 명령을 동시에 실행하거나, 소유권을 잃은
worker가 뒤늦게 다른 worker의 결과를 덮어쓰는 것을 막는다. 로컬 idempotency_key는
"같은 요청을 두 번 만들지 않는다"는 보장일 뿐, 채널 쪽의 exactly-once 실행을
보장하지 않는다(실계정 검증 전 필요 조건 참고).
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin

# PENDING: 생성됨, 아직 실행 안 함
# RUNNING: 실행 중(RUNNING인 채 오래 멈춰있으면 UNKNOWN으로 회수 - PENDING으로 되돌리지
#          않는다. 채널에 실제로 도달했는지 알 수 없는 채로 자동 재전송하면 안 되기 때문)
# SUCCESS: 채널이 성공을 확인함
# FAILED: 채널에 반영되지 않았음이 확실한 실패(자격증명 없음/미지원/채널의 명시적 거부/
#         재시도 소진). retryable=False.
# RETRY_WAIT: 채널이 아직 처리하지 않았음이 확실한 실패(예: 429/연결 자체 미성립)로
#             재시도 대기 중(next_retry_at까지) - "확실히 미처리"인 경우만 여기로 온다.
# UNKNOWN: 채널이 처리했는지 확인할 수 없는 실패(timeout/전송 중 오류/응답 파싱 실패/
#          worker 크래시로 인한 RUNNING 회수) - 자동 재시도 금지, 운영자 확인 필요.
# CANCELLED: 사용자가 취소함(재시도 포기)
EXTERNAL_COMMAND_STATUSES = ("PENDING", "RUNNING", "SUCCESS", "FAILED", "RETRY_WAIT", "UNKNOWN", "CANCELLED")


class ExternalCommand(Base, TimestampMixin):
    """채널로 나가는 쓰기 명령의 outbox(이력) 테이블."""

    __tablename__ = "external_commands"
    __table_args__ = (
        Index("uq_external_commands_idempotency_key", "idempotency_key", unique=True),
        Index("idx_external_commands_target", "target_type", "target_id"),
        Index("idx_external_commands_status", "status", "next_retry_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 호출부가 결정론적으로 생성 - 같은 키는 같은 명령을 가리킨다(재전송 idempotency).
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    # SHIPMENT_SUBMIT / ORDER_STATUS_PULL 등. 대량 처리(6단계)에서도 재사용.
    command_type: Mapped[str] = mapped_column(String(30), nullable=False)
    platform_id: Mapped[int] = mapped_column(ForeignKey("platforms.id"), nullable=False)
    platform_code: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    # SHIPMENT/ORDER 등 - target_id가 어느 테이블의 PK인지.
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    retryable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    attempt_count: Mapped[int] = mapped_column(default=0, nullable=False)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # 안전한 요약만 저장한다 - 원본 요청/응답 전문·Secret·PII 금지(모듈 docstring 참고).
    # 예: "carrier=CJGLS items=3" / "resultCode=OK".
    request_summary: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    response_summary: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # 소유권(lease) 토큰 - claim() 성공 시 발급, 최종상태 반영은 이 값이 일치할 때만
    # 허용된다(모듈 docstring "동시 실행 방지" 참고). NULL이면 아무도 선점하지 않은 상태.
    lease_token: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)


class ExternalCommandLineResult(Base, TimestampMixin):
    """ExternalCommand 하나가 여러 라인(OrderItem)을 전송할 때, 라인별 성공/실패를
    기록한다 - 부분성공(일부 라인만 성공) 시 이미 성공한 라인은 재시도에서 제외하기
    위한 근거 테이블이다(services.shipment_dispatch_service._dispatch_all_lines 참고).

    quantity는 이 라인 결과가 커버하는 발송 수량이다(주문 전체 배송이면 OrderItem.
    quantity 전체, 부분출고 배송이면 ShipmentItem.quantity) - 주문의 전체 이행 여부를
    "라인 존재"가 아니라 "발송 수량 합계 >= 주문 수량"으로 집계하기 위해 필요하다
    (같은 OrderItem이 여러 Shipment로 나뉘어 부분 발송되는 경우 대응).
    """

    __tablename__ = "external_command_line_results"
    __table_args__ = (
        UniqueConstraint("command_id", "order_item_id", name="uq_command_line_result"),
        Index("idx_command_line_results_order_item", "order_item_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    command_id: Mapped[int] = mapped_column(ForeignKey("external_commands.id"), nullable=False)
    order_item_id: Mapped[int] = mapped_column(ForeignKey("order_items.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # SUCCESS / FAILED
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    result_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)


class ProductSyncCommandDetail(Base):
    """ExternalCommand(command_type IN (INVENTORY_UPDATE, SALE_STATUS_UPDATE))의 구조화된
    목표값 - 상용 ERP 확장(3단계, 첫 묶음). 명령 접수(enqueue) 시점에 운영자가 입력한
    목표 수량/판매상태를 확정해 저장하고, 재시도 때도 이 값을 그대로 재사용한다(화면에서
    다시 읽은 최신 입력값으로 바꾸지 않는다 - 재시도는 "정확히 같은 요청"의 반복이어야
    한다). 하나의 ExternalCommand에는 정확히 하나의 상세 행만 연결된다(1:1).

    target_sale_status는 채널 무관 내부 값(ON_SALE/SUSPENDED) 두 가지만 쓴다 - 커넥터가
    채널별 표현으로 변환한다(쿠팡: sales/resume·sales/stop 호출, 네이버: statusType=
    SALE·SUSPENSION). "품절(OUTOFSTOCK)"은 두 채널 다 재고 0에 따라 시스템이 계산하는
    파생 상태로 보고(네이버 공식 문서: "재고 수량이 0으로 입력되면 StatusType으로
    전달된 항목은 무시되며 상품 상태는 OUTOFSTOCK으로 저장됩니다") 사용자가 직접
    지정하는 목표값으로 두지 않는다.
    """

    __tablename__ = "product_sync_command_details"
    __table_args__ = (Index("uq_product_sync_command_detail", "command_id", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    command_id: Mapped[int] = mapped_column(ForeignKey("external_commands.id"), nullable=False)
    product_platform_map_id: Mapped[int] = mapped_column(ForeignKey("product_platform_map.id"), nullable=False)
    target_quantity: Mapped[Optional[int]] = mapped_column(nullable=True)  # INVENTORY_UPDATE 전용
    target_sale_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # SALE_STATUS_UPDATE 전용
    # PRODUCT_INFO_UPDATE 전용(상용 ERP 확장 3단계 두 번째 묶음) - 셋 다 Optional인
    # 이유: 상품명/판매가/상세설명 중 실제로 바뀐 값만 채널에 보낸다(안 바뀐 필드는
    # None으로 두어 커넥터가 "채널의 현재값을 그대로 보존"하도록 신호한다 - 전체교체형
    # API에 빈 값/0/null로 덮어써 지우는 것을 방지).
    target_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    target_sale_price: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    target_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class ProductPublishCommandDetail(Base):
    """ExternalCommand(command_type == PRODUCT_CREATE)의 확정된 등록 스냅샷 - 상용
    ERP 확장(3단계, 두 번째 묶음). ProductPublishDraft는 운영자가 계속 편집할 수
    있는 가변 초안이라, 명령 접수(enqueue) 시점의 값을 이 테이블에 얼려 둔다 -
    이후 초안을 편집해도 이미 대기 중인 명령이 보내는 값은 바뀌지 않는다(재시도는
    "정확히 같은 요청"의 반복이어야 한다 - services.product_sync_dispatch_service의
    ProductSyncCommandDetail과 동일 원칙).

    snapshot_json에는 ProductPublishDraft의 모든 필드(name/sale_price/
    description_html/category_code/image_urls_json/stock_quantity/
    channel_fields_json)를 그대로 JSON 직렬화해 담는다 - 필드가 채널마다 크게
    달라 개별 컬럼으로 정규화하지 않는다(draft와 동일한 이유)."""

    __tablename__ = "product_publish_command_details"
    __table_args__ = (Index("uq_product_publish_command_detail", "command_id", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    command_id: Mapped[int] = mapped_column(ForeignKey("external_commands.id"), nullable=False)
    draft_id: Mapped[int] = mapped_column(ForeignKey("product_publish_drafts.id"), nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)


class OrderStatusConflict(Base, TimestampMixin):
    """내부 주문상태와 채널 주문상태가 "허용된 전이"로 설명되지 않을 때의 기록.

    자동으로 어느 한쪽을 정답으로 덮어쓰지 않고, 운영자가 확인 후 명시적으로
    해소(resolve)하게 한다.
    """

    __tablename__ = "order_status_conflicts"
    __table_args__ = (Index("idx_order_status_conflicts_order", "order_id", "resolved_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    internal_status: Mapped[str] = mapped_column(String(20), nullable=False)
    channel_status: Mapped[str] = mapped_column(String(20), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    # ACCEPT_CHANNEL(채널 상태를 내부에 반영) / KEEP_INTERNAL(내부 상태 유지, 채널 값 무시)
    resolution: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
