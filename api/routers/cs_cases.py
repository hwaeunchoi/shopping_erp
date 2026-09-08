"""
api/routers/cs_cases.py
--------------------------
CS(고객문의) 케이스 API - 상용 ERP 확장(5단계, B묶음).

권한은 기능별로 나눈다(요구사항 원문 그대로) - 기존 SHIPMENT_VIEW 재사용
패턴과 달리 CS는 개인정보 노출 위험이 커서 새 권한 6종을 쓴다
(scripts/init_db.py DEFAULT_PERMISSIONS 참고):
  CS_VIEW(조회) / CS_MANAGE(생성·수정·메모·답변초안) / CS_ASSIGN(담당자 배정) /
  CS_CLOSE(종결·재오픈) / CS_REPLY_SUBMIT(외부 답변 접수 - 이번 단계는 구현 자체가
  없어 아직 쓰이지 않는다, 향후 채널 답변 계약이 확인되면 사용할 자리만 예약) /
  CS_PII_DETAIL(개인정보 상세 조회).

CLOSED로의 상태변경은 일반 상태변경 엔드포인트(POST .../status)에서 명시적으로
거부하고 전용 엔드포인트(POST .../close)로만 허용한다 - CS_CLOSE 권한을 별도로
검사하기 위함이다(일반 상태변경은 CS_MANAGE만 있으면 된다). 재오픈도 마찬가지로
CS_CLOSE 전용이다.

개인정보: 목록/이력에는 고객 이름/전화번호를 마스킹해서만 담고(services/
pii_mask.py), 상세 조회에서도 CS_PII_DETAIL 권한이 있는 사용자에게만 원본
전화번호/주소를 추가로 내려준다. 문의 본문/내부 메모/답변 내용 자체는
안전한 문자열이므로 그대로 보이되(업무상 필요), 오류 메시지에는 담지 않는다.

라우트 등록 순서 주의: FastAPI/Starlette는 등록 순서대로 매칭하고 경로
파라미터 타입 변환 실패 시 다음 라우트로 폴백하지 않는다(그대로 422) - 그래서
"/bulk/assign"·"/dashboard"·"/sync"·"/meta/reference"처럼 리터럴 세그먼트로
시작하는 정적 경로를 전부 "/{case_id}"로 시작하는 동적 경로보다 먼저 등록한다
(안 그러면 예: POST /bulk/assign이 POST /{case_id}/assign에 먼저 걸려
case_id="bulk" 정수 변환 실패로 422가 난다).
"""

from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from api.deps import get_current_user, get_db, require_permission
from integrations.malls import get_mall_connector
from integrations.malls.errors import (
    MarketplaceCapabilityUnsupportedError,
    MarketplaceCredentialMissingError,
    MarketplaceExternalAPIError,
)
from models.cs_case import CS_CASE_INQUIRY_TYPES, CS_CASE_PRIORITIES, CsCase
from models.customer import Customer
from models.order import Order
from models.user import User
from repositories.platform_repository import PlatformRepository
from repositories.user_repository import PermissionRepository
from services.cs_case_service import BulkCaseOutcome, CsCaseConflictError, CsCaseService, CsCaseValidationError
from services.cs_channel_sync_service import CsChannelSyncService
from services.pii_mask import mask_name, mask_phone

router = APIRouter(prefix="/api/cs-cases", tags=["cs-cases"])


def _validation_error_status(message: str) -> int:
    return status.HTTP_404_NOT_FOUND if "찾을 수 없습니다" in message else status.HTTP_400_BAD_REQUEST


def _has_permission(db, user: User, code: str) -> bool:
    permissions = PermissionRepository(db).list_by_role(user.role_id)
    return code in {p.code for p in permissions}


def _resolve_customer_fields(db, case: CsCase) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(이름, 전화번호, 주소) 원본값을 연결된 Order(수취인) 또는 Customer에서
    가져온다 - 우선순위는 Order.receiver_*(이 CS 건이 실제로 참조한 배송 대상)
    쪽을 먼저 본다."""
    if case.order_id is not None:
        order = db.get(Order, case.order_id)
        if order is not None and (order.receiver_name or order.receiver_phone or order.receiver_address):
            return order.receiver_name, order.receiver_phone, order.receiver_address
    if case.customer_id is not None:
        customer = db.get(Customer, case.customer_id)
        if customer is not None:
            return customer.name, customer.phone, customer.address
    return None, None, None


class CsCaseOut(BaseModel):
    id: int
    platform_id: Optional[int] = None
    external_inquiry_id: Optional[str] = None
    external_source: Optional[str] = None
    external_raw_status: Optional[str] = None
    order_id: Optional[int] = None
    order_item_id: Optional[int] = None
    product_option_id: Optional[int] = None
    shipment_id: Optional[int] = None
    fulfillment_batch_item_id: Optional[int] = None
    claim_type: Optional[str] = None
    claim_id: Optional[int] = None
    inquiry_type: str
    priority: str
    status: str
    assignee_id: Optional[int] = None
    subject: Optional[str] = None
    customer_message: str
    reply_draft: Optional[str] = None
    due_at: Optional[datetime] = None
    last_customer_message_at: Optional[datetime] = None
    last_agent_response_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    reopened_count: int
    tags: Optional[str] = None
    created_by: Optional[int] = None
    created_at: datetime
    customer_name_masked: Optional[str] = None
    customer_phone_masked: Optional[str] = None
    # CS_PII_DETAIL 권한이 있을 때만 채워진다(없으면 항상 None - 목록/상세 공통).
    customer_phone_full: Optional[str] = None
    customer_address_full: Optional[str] = None


def _to_case_out(db, case: CsCase, *, include_pii_detail: bool) -> CsCaseOut:
    name, phone, address = _resolve_customer_fields(db, case)
    return CsCaseOut(
        id=case.id,
        platform_id=case.platform_id,
        external_inquiry_id=case.external_inquiry_id,
        external_source=case.external_source,
        external_raw_status=case.external_raw_status,
        order_id=case.order_id,
        order_item_id=case.order_item_id,
        product_option_id=case.product_option_id,
        shipment_id=case.shipment_id,
        fulfillment_batch_item_id=case.fulfillment_batch_item_id,
        claim_type=case.claim_type,
        claim_id=case.claim_id,
        inquiry_type=case.inquiry_type,
        priority=case.priority,
        status=case.status,
        assignee_id=case.assignee_id,
        subject=case.subject,
        customer_message=case.customer_message,
        reply_draft=case.reply_draft,
        due_at=case.due_at,
        last_customer_message_at=case.last_customer_message_at,
        last_agent_response_at=case.last_agent_response_at,
        resolved_at=case.resolved_at,
        closed_at=case.closed_at,
        reopened_count=case.reopened_count,
        tags=case.tags,
        created_by=case.created_by,
        created_at=case.created_at,
        customer_name_masked=mask_name(name) if name else None,
        customer_phone_masked=mask_phone(phone) if phone else None,
        customer_phone_full=phone if include_pii_detail else None,
        customer_address_full=address if include_pii_detail else None,
    )


# --- 정적 경로 라우트(전부 "/{case_id}"보다 먼저 등록) ---------------------


@router.get("", response_model=list[CsCaseOut], dependencies=[Depends(require_permission("CS_VIEW"))])
def list_cases(
    status_filter: Optional[str] = None,
    platform_id: Optional[int] = None,
    inquiry_type: Optional[str] = None,
    priority: Optional[str] = None,
    assignee_id: Optional[int] = None,
    unassigned_only: bool = False,
    overdue_only: bool = False,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[CsCaseOut]:
    cases = CsCaseService(db).list_cases(
        status=status_filter,
        platform_id=platform_id,
        inquiry_type=inquiry_type,
        priority=priority,
        assignee_id=assignee_id,
        unassigned_only=unassigned_only,
        overdue_only=overdue_only,
        search=search,
        limit=limit,
        offset=offset,
    )
    include_pii = _has_permission(db, current_user, "CS_PII_DETAIL")
    return [_to_case_out(db, c, include_pii_detail=include_pii) for c in cases]


class CreateCaseRequest(BaseModel):
    inquiry_type: str
    customer_message: str
    priority: str = "NORMAL"
    subject: Optional[str] = None
    order_id: Optional[int] = None
    order_item_id: Optional[int] = None
    product_option_id: Optional[int] = None
    shipment_id: Optional[int] = None
    fulfillment_batch_item_id: Optional[int] = None
    claim_type: Optional[str] = None
    claim_id: Optional[int] = None
    customer_id: Optional[int] = None
    due_at: Optional[datetime] = None
    tags: Optional[str] = None
    # status는 의도적으로 받지 않는다 - CLOSED 직접 생성을 스키마 자체로 막는다.


class CreateCaseResponseOut(BaseModel):
    case: CsCaseOut
    duplicate_of_case_id: Optional[int] = None


@router.post(
    "",
    response_model=CreateCaseResponseOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("CS_MANAGE"))],
    responses={400: {"description": "잘못된 문의유형/우선순위/클레임유형 등."}},
)
def create_case(
    payload: CreateCaseRequest, db=Depends(get_db), current_user: User = Depends(get_current_user)
) -> CreateCaseResponseOut:
    service = CsCaseService(db)
    try:
        result = service.create_case(
            inquiry_type=payload.inquiry_type,
            customer_message=payload.customer_message,
            priority=payload.priority,
            subject=payload.subject,
            order_id=payload.order_id,
            order_item_id=payload.order_item_id,
            product_option_id=payload.product_option_id,
            shipment_id=payload.shipment_id,
            fulfillment_batch_item_id=payload.fulfillment_batch_item_id,
            claim_type=payload.claim_type,
            claim_id=payload.claim_id,
            customer_id=payload.customer_id,
            due_at=payload.due_at,
            tags=payload.tags,
            created_by=current_user.id,
        )
    except CsCaseValidationError as e:
        db.rollback()
        raise HTTPException(status_code=_validation_error_status(str(e)), detail=str(e)) from e
    db.commit()
    include_pii = _has_permission(db, current_user, "CS_PII_DETAIL")
    return CreateCaseResponseOut(
        case=_to_case_out(db, result.case, include_pii_detail=include_pii),
        duplicate_of_case_id=result.duplicate_of_case_id,
    )


@router.get("/dashboard", dependencies=[Depends(require_permission("CS_VIEW"))])
def dashboard_summary(db=Depends(get_db)) -> dict:
    return CsCaseService(db).dashboard_summary()


class BulkAssignRequest(BaseModel):
    case_ids: list[int]
    assignee_id: Optional[int] = None


class BulkOutcomeOut(BaseModel):
    case_id: int
    outcome: str
    error_code: Optional[str] = None


def _to_bulk_outcome_out(outcome: BulkCaseOutcome) -> BulkOutcomeOut:
    return BulkOutcomeOut(case_id=outcome.case_id, outcome=outcome.outcome, error_code=outcome.error_code)


@router.post(
    "/bulk/assign", response_model=list[BulkOutcomeOut], dependencies=[Depends(require_permission("CS_ASSIGN"))]
)
def bulk_assign(
    payload: BulkAssignRequest, db=Depends(get_db), current_user: User = Depends(get_current_user)
) -> list[BulkOutcomeOut]:
    outcomes = CsCaseService(db).bulk_assign(payload.case_ids, payload.assignee_id, current_user.id)
    db.commit()
    return [_to_bulk_outcome_out(o) for o in outcomes]


class BulkStatusChangeRequest(BaseModel):
    case_ids: list[int]
    new_status: str


@router.post(
    "/bulk/status", response_model=list[BulkOutcomeOut], dependencies=[Depends(require_permission("CS_MANAGE"))]
)
def bulk_change_status(
    payload: BulkStatusChangeRequest, db=Depends(get_db), current_user: User = Depends(get_current_user)
) -> list[BulkOutcomeOut]:
    """CLOSED로의 대량 전환은 지원하지 않는다 - 종결은 CS_CLOSE 권한으로 건별
    확인 후 처리하는 POST /{case_id}/close 전용이다(대량 종결의 오조작 위험을
    피하기 위함)."""
    if payload.new_status == "CLOSED":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="대량 종결은 지원하지 않습니다 - 건별로 POST /{case_id}/close를 사용하세요.",
        )
    outcomes = CsCaseService(db).bulk_change_status(payload.case_ids, payload.new_status, current_user.id)
    db.commit()
    return [_to_bulk_outcome_out(o) for o in outcomes]


class SyncRequest(BaseModel):
    platform_id: int
    days: int = 7


class SyncResultOut(BaseModel):
    platform_code: str
    status: str
    created: int = 0
    updated: int = 0
    failed: int = 0
    reason_code: Optional[str] = None


@router.post(
    "/sync",
    response_model=SyncResultOut,
    dependencies=[Depends(require_permission("CS_MANAGE"))],
    summary="채널 CS(고객문의) 수동 동기화 - 공식 계약이 확인된 채널만 실제로 조회한다",
    description="현재는 쿠팡 콜센터 문의만 실제로 동기화된다(공식 계약 확인됨) - 다른"
    " 채널/문의유형은 UNSUPPORTED로 반환된다. settings.cs_inquiry_sync_enabled가"
    " False(기본값)면 DISABLED를 반환하고 외부 채널을 호출하지 않는다. 실제 채널"
    " 답변 전송은 이 엔드포인트를 포함해 어디에도 없다(조회 전용).",
    responses={404: {"description": "플랫폼을 찾을 수 없습니다."}},
)
def sync_channel_inquiries(payload: SyncRequest, db=Depends(get_db)) -> SyncResultOut:
    platform = PlatformRepository(db).get_by_id(payload.platform_id)
    if platform is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="플랫폼을 찾을 수 없습니다.")

    end_date = date.today()
    start_date = end_date - timedelta(days=payload.days)
    try:
        connector = get_mall_connector(platform.connector_class, session=db, platform_id=platform.id)
        result = CsChannelSyncService(db).sync_inquiries(connector, platform.id, start_date, end_date)
    except MarketplaceCapabilityUnsupportedError:
        db.rollback()
        return SyncResultOut(platform_code=platform.code, status="UNSUPPORTED")
    except MarketplaceCredentialMissingError:
        db.rollback()
        return SyncResultOut(platform_code=platform.code, status="FAILED", reason_code="CREDENTIAL_MISSING")
    except MarketplaceExternalAPIError as e:
        db.rollback()
        return SyncResultOut(platform_code=platform.code, status="FAILED", reason_code=e.reason_code)
    db.commit()
    return SyncResultOut(platform_code=platform.code, **result)


class ReferenceOut(BaseModel):
    inquiry_types: list[str]
    priorities: list[str]


@router.get("/meta/reference", response_model=ReferenceOut, dependencies=[Depends(require_permission("CS_VIEW"))])
def get_reference() -> ReferenceOut:
    return ReferenceOut(inquiry_types=sorted(CS_CASE_INQUIRY_TYPES), priorities=sorted(CS_CASE_PRIORITIES))


# --- 동적 경로 라우트("/{case_id}...") - 반드시 위 정적 경로들 다음에 등록 ---


@router.get("/{case_id}", response_model=CsCaseOut, dependencies=[Depends(require_permission("CS_VIEW"))])
def get_case(case_id: int, db=Depends(get_db), current_user: User = Depends(get_current_user)) -> CsCaseOut:
    case = CsCaseService(db).get_case(case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="CS 케이스를 찾을 수 없습니다.")
    include_pii = _has_permission(db, current_user, "CS_PII_DETAIL")
    return _to_case_out(db, case, include_pii_detail=include_pii)


class AssignRequest(BaseModel):
    assignee_id: Optional[int] = None
    expected_assignee_id: Optional[int] = None


@router.post(
    "/{case_id}/assign",
    response_model=CsCaseOut,
    dependencies=[Depends(require_permission("CS_ASSIGN"))],
    responses={409: {"description": "현재 담당자가 예상과 다릅니다(다른 사용자가 이미 배정함)."}},
)
def assign_case(
    case_id: int, payload: AssignRequest, db=Depends(get_db), current_user: User = Depends(get_current_user)
) -> CsCaseOut:
    service = CsCaseService(db)
    try:
        case = service.assign(case_id, payload.assignee_id, payload.expected_assignee_id, current_user.id)
    except CsCaseConflictError as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except CsCaseValidationError as e:
        db.rollback()
        raise HTTPException(status_code=_validation_error_status(str(e)), detail=str(e)) from e
    db.commit()
    include_pii = _has_permission(db, current_user, "CS_PII_DETAIL")
    return _to_case_out(db, case, include_pii_detail=include_pii)


class StatusChangeRequest(BaseModel):
    new_status: str
    expected_status: str


@router.post(
    "/{case_id}/status",
    response_model=CsCaseOut,
    dependencies=[Depends(require_permission("CS_MANAGE"))],
    responses={
        400: {"description": "허용되지 않는 전이, 또는 CLOSED는 이 API로 지정할 수 없음(POST .../close 사용)."},
        409: {"description": "현재 상태가 예상과 다릅니다."},
    },
)
def change_status(
    case_id: int, payload: StatusChangeRequest, db=Depends(get_db), current_user: User = Depends(get_current_user)
) -> CsCaseOut:
    if payload.new_status == "CLOSED":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="종결은 POST /api/cs-cases/{id}/close를 사용하세요."
        )
    service = CsCaseService(db)
    try:
        case = service.change_status(case_id, payload.new_status, payload.expected_status, current_user.id)
    except CsCaseConflictError as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except CsCaseValidationError as e:
        db.rollback()
        raise HTTPException(status_code=_validation_error_status(str(e)), detail=str(e)) from e
    db.commit()
    include_pii = _has_permission(db, current_user, "CS_PII_DETAIL")
    return _to_case_out(db, case, include_pii_detail=include_pii)


class CloseRequest(BaseModel):
    expected_status: str = "RESOLVED"


@router.post(
    "/{case_id}/close",
    response_model=CsCaseOut,
    dependencies=[Depends(require_permission("CS_CLOSE"))],
    responses={
        400: {"description": "해결완료(RESOLVED) 상태에서만 종결할 수 있습니다."},
        409: {"description": "현재 상태가 예상과 다릅니다."},
    },
)
def close_case(
    case_id: int, payload: CloseRequest, db=Depends(get_db), current_user: User = Depends(get_current_user)
) -> CsCaseOut:
    service = CsCaseService(db)
    try:
        case = service.change_status(case_id, "CLOSED", payload.expected_status, current_user.id)
    except CsCaseConflictError as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except CsCaseValidationError as e:
        db.rollback()
        raise HTTPException(status_code=_validation_error_status(str(e)), detail=str(e)) from e
    db.commit()
    include_pii = _has_permission(db, current_user, "CS_PII_DETAIL")
    return _to_case_out(db, case, include_pii_detail=include_pii)


class ReopenRequest(BaseModel):
    expected_status: str = "CLOSED"


@router.post(
    "/{case_id}/reopen",
    response_model=CsCaseOut,
    dependencies=[Depends(require_permission("CS_CLOSE"))],
    responses={409: {"description": "현재 상태가 CLOSED가 아니거나 이미 변경되었습니다."}},
)
def reopen_case(
    case_id: int, payload: ReopenRequest, db=Depends(get_db), current_user: User = Depends(get_current_user)
) -> CsCaseOut:
    service = CsCaseService(db)
    try:
        case = service.reopen_case(case_id, payload.expected_status, current_user.id)
    except CsCaseConflictError as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except CsCaseValidationError as e:
        db.rollback()
        raise HTTPException(status_code=_validation_error_status(str(e)), detail=str(e)) from e
    db.commit()
    include_pii = _has_permission(db, current_user, "CS_PII_DETAIL")
    return _to_case_out(db, case, include_pii_detail=include_pii)


class MemoCreateRequest(BaseModel):
    content: str


class MemoOut(BaseModel):
    id: int
    content: str
    created_by: Optional[int] = None
    created_at: datetime


@router.get("/{case_id}/memos", response_model=list[MemoOut], dependencies=[Depends(require_permission("CS_VIEW"))])
def list_memos(case_id: int, db=Depends(get_db)) -> list[MemoOut]:
    memos = CsCaseService(db).list_memos(case_id)
    return [MemoOut(id=m.id, content=m.content, created_by=m.created_by, created_at=m.created_at) for m in memos]


@router.post(
    "/{case_id}/memos",
    response_model=MemoOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("CS_MANAGE"))],
)
def add_memo(
    case_id: int, payload: MemoCreateRequest, db=Depends(get_db), current_user: User = Depends(get_current_user)
) -> MemoOut:
    service = CsCaseService(db)
    try:
        memo = service.add_memo(case_id, payload.content, current_user.id)
    except CsCaseValidationError as e:
        db.rollback()
        raise HTTPException(status_code=_validation_error_status(str(e)), detail=str(e)) from e
    db.commit()
    return MemoOut(id=memo.id, content=memo.content, created_by=memo.created_by, created_at=memo.created_at)


class ReplyDraftRequest(BaseModel):
    reply_draft: str
    expected_status: str


@router.post(
    "/{case_id}/reply-draft",
    response_model=CsCaseOut,
    dependencies=[Depends(require_permission("CS_MANAGE"))],
    responses={409: {"description": "현재 상태가 예상과 다릅니다."}},
)
def update_reply_draft(
    case_id: int, payload: ReplyDraftRequest, db=Depends(get_db), current_user: User = Depends(get_current_user)
) -> CsCaseOut:
    service = CsCaseService(db)
    try:
        case = service.update_reply_draft(case_id, payload.reply_draft, payload.expected_status, current_user.id)
    except CsCaseConflictError as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except CsCaseValidationError as e:
        db.rollback()
        raise HTTPException(status_code=_validation_error_status(str(e)), detail=str(e)) from e
    db.commit()
    include_pii = _has_permission(db, current_user, "CS_PII_DETAIL")
    return _to_case_out(db, case, include_pii_detail=include_pii)


class HistoryOut(BaseModel):
    id: int
    action: str
    from_value: Optional[str] = None
    to_value: Optional[str] = None
    changed_by: Optional[int] = None
    changed_at: datetime
    note: Optional[str] = None


@router.get(
    "/{case_id}/history", response_model=list[HistoryOut], dependencies=[Depends(require_permission("CS_VIEW"))]
)
def get_history(case_id: int, db=Depends(get_db)) -> list[HistoryOut]:
    history = CsCaseService(db).list_history(case_id)
    return [
        HistoryOut(
            id=h.id,
            action=h.action,
            from_value=h.from_value,
            to_value=h.to_value,
            changed_by=h.changed_by,
            changed_at=h.changed_at,
            note=h.note,
        )
        for h in history
    ]
