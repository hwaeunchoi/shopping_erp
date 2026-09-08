"""
models/cs_case.py
--------------------
상용 ERP 확장(5단계, B묶음) - 문의·CS 통합 관리(cs_cases/cs_case_history).

CS 케이스는 수기 생성(전화/채팅 등 채널 밖 문의)과 채널 문의 동기화(현재는
쿠팡 콜센터 문의 조회만 공식 계약 확인됨 - services/cs_channel_sync_service.py
참고) 양쪽 다 같은 테이블에 담긴다. 채널에서 들어온 건은 platform_id +
external_inquiry_id가 채워지고, 이 둘의 조합이 유니크해 재수집 시 중복
케이스를 만들지 않는다(수기 생성 건은 external_inquiry_id가 NULL이라
서로 충돌하지 않는다 - SQLite/PostgreSQL 모두 NULL은 유니크 제약에서
서로 다른 값으로 취급).

주문/주문라인/상품/배송/출고와의 연결은 전부 nullable FK다(하나의 CS
케이스가 반드시 주문에 연결될 필요는 없다 - 배송 전 상품 문의 등). 클레임
(교환/반품/취소)은 models.extra의 Memo/Attachment와 같은 다형성
(claim_type/claim_id) 패턴으로 연결한다 - 세 테이블(exchanges/returns/
cancellations)에 FK를 세 개 두는 대신 이 코드베이스에 이미 있는 관례를
그대로 따른다.

내부 메모는 새 테이블을 만들지 않고 기존 models.extra.Memo를
target_type="CS_CASE"로 재사용한다. 첨부파일 메타데이터도 마찬가지로
기존 models.extra.Attachment를 target_type="CS_CASE"로 재사용한다(실제
업로드 저장소는 이 코드베이스 어디에도 아직 없으므로 새로 만들지 않는다 -
services/cs_case_service.py 모듈 docstring 참고).

고객 문의 본문(customer_message)과 답변 초안(reply_draft)/전송
스냅샷(reply_sent_snapshot)은 로그에 출력하지 않는다 - CsCaseHistory.note는
상태 코드 등 안전한 요약만 남기고 본문을 복사하지 않는다(fulfillment.py의
FulfillmentBatchItemHistory와 동일 원칙).
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin

# 최소 상태 6종(요구사항 원문 그대로) + 상태전이 검증은 services/cs_state_machine.py.
CS_CASE_STATUSES = frozenset({"OPEN", "IN_PROGRESS", "WAITING_CUSTOMER", "WAITING_CHANNEL", "RESOLVED", "CLOSED"})
CS_CASE_PRIORITIES = frozenset({"LOW", "NORMAL", "HIGH", "URGENT"})
# 문의유형 - 이 ERP가 이미 다루는 업무 도메인과 맞춘 최소 집합. 채널 동기화 건은
# 매핑 불가 시 ETC로 분류한다(추측으로 세분화하지 않는다).
CS_CASE_INQUIRY_TYPES = frozenset({"DELIVERY", "EXCHANGE_RETURN", "PRODUCT", "PAYMENT", "ETC"})


class CsCase(Base, TimestampMixin):
    """CS(고객문의) 케이스."""

    __tablename__ = "cs_cases"
    __table_args__ = (
        # platform_id+external_inquiry_id 조합 유니크 - 채널 문의 재수집 시 중복 케이스
        # 생성을 막는다. 수기 생성 건(둘 다 NULL)은 여러 건이 있어도 유니크 제약에 걸리지
        # 않는다(NULL은 서로 다른 값으로 취급되는 표준 동작 - order_items의 동일 관례 참고).
        Index("uq_cs_case_external_inquiry", "platform_id", "external_inquiry_id", unique=True),
        Index("idx_cs_cases_status", "status"),
        Index("idx_cs_cases_assignee", "assignee_id"),
        Index("idx_cs_cases_due_at", "due_at"),
        Index("idx_cs_cases_order", "order_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # 채널 연동 식별자(수기 생성 건은 전부 NULL).
    platform_id: Mapped[Optional[int]] = mapped_column(ForeignKey("platforms.id"), nullable=True)
    external_inquiry_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # 채널 문의 종류 - 현재는 "COUPANG_CALL_CENTER"만 실제로 채워진다(공식 계약 확인된
    # 유일한 조회 대상 - services/cs_channel_sync_service.py 참고). 수기 생성 건은 NULL.
    external_source: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    # 채널이 준 원본 상태 코드 그대로(예: 쿠팡 "progress:requestAnswer") - CsCase.status
    # (내부 CS 워크플로우 상태)와는 완전히 다른 값이다. 모르는 원본 상태를 내부
    # 상태로 임의 매핑하지 않고 그대로 보존만 한다(models.order.py의 Return/
    # Exchange/Cancellation.raw_status와 동일 관례).
    external_raw_status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # 연결 식별자(전부 nullable, 앱 레벨 검증 - FK가 있는 것과 다형성인 것이 섞여 있다).
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id"), nullable=True)
    order_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("order_items.id"), nullable=True)
    product_option_id: Mapped[Optional[int]] = mapped_column(ForeignKey("product_options.id"), nullable=True)
    shipment_id: Mapped[Optional[int]] = mapped_column(ForeignKey("shipments.id"), nullable=True)
    fulfillment_batch_item_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("fulfillment_batch_items.id"), nullable=True
    )
    # 클레임(교환/반품/취소) 연결 - 다형성 패턴(models.extra.Memo와 동일 관례).
    claim_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # EXCHANGE/RETURN/CANCELLATION
    claim_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    customer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("customers.id"), nullable=True)

    inquiry_type: Mapped[str] = mapped_column(String(30), nullable=False)
    priority: Mapped[str] = mapped_column(String(10), default="NORMAL", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="OPEN", nullable=False)

    assignee_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    subject: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    customer_message: Mapped[str] = mapped_column(String(4000), nullable=False)
    # 답변 초안 - 언제든 다시 수정될 수 있다. 실제 채널로 전송된 답변은 아래
    # reply_sent_snapshot에 접수 시점 값을 얼려서 별도로 보관한다(초안을 나중에 고쳐도
    # 이미 나간 답변 기록은 바뀌지 않는다).
    reply_draft: Mapped[Optional[str]] = mapped_column(String(4000), nullable=True)
    reply_sent_snapshot: Mapped[Optional[str]] = mapped_column(String(4000), nullable=True)
    reply_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    due_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_customer_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_agent_response_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    reopened_count: Mapped[int] = mapped_column(default=0, nullable=False)

    tags: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)


class CsCaseHistory(Base):
    """CS 케이스 작업 이력(상태변경/배정/메모추가/답변초안수정/답변전송/재오픈) - append-only.

    note는 사유 코드 등 안전한 짧은 요약만 담는다 - 문의 본문/메모 내용/답변
    내용은 여기 복사하지 않는다(위 모듈 docstring 참고)."""

    __tablename__ = "cs_case_history"
    __table_args__ = (Index("idx_cs_case_history", "case_id", "changed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cs_cases.id"), nullable=False)
    # CREATED/STATUS_CHANGE/ASSIGNED/MEMO_ADDED/REPLY_DRAFT_UPDATED/REPLY_SENT/REOPENED
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    from_value: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    to_value: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    changed_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    note: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
