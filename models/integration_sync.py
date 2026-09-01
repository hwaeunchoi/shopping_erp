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
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin

# PENDING: 생성됨, 아직 실행 안 함
# RUNNING: 실행 중(짧게 유지 - 프로세스 크래시 시 복구 로직은 후속 단계)
# SUCCESS: 채널이 성공을 확인함
# FAILED: 실패, retryable=False면 재시도 대상 아님
# RETRY_WAIT: 실패했고 retryable=True, next_retry_at까지 대기
# CANCELLED: 사용자가 취소함(재시도 포기)
EXTERNAL_COMMAND_STATUSES = ("PENDING", "RUNNING", "SUCCESS", "FAILED", "RETRY_WAIT", "CANCELLED")


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
