"""
api/routers/fulfillment.py
---------------------------
출고 배치(피킹/검수/포장) API - 상용 ERP 확장(5단계, A묶음).

기존 배송 메뉴(api/routers/shipments.py)와 같은 권한(SHIPMENT_VIEW)으로
보호한다 - 출고는 배송 업무의 앞단이라 새 권한 코드를 만들지 않고 재사용한다.

이 라우터는 실제 채널 HTTP 호출을 절대 하지 않는다 - 채널 전송은
services.fulfillment_service.FulfillmentService.submit_to_channel()이
services.shipment_dispatch_service.ShipmentDispatchService.enqueue()를 호출해
PENDING ExternalCommand만 만들고, 실제 전송은 기존 outbox_dispatch_job이
비동기로 수행한다(이 라우터가 응답한 뒤에도 계속 진행됨).
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from api.deps import get_current_user, get_db, require_permission
from models.fulfillment import FulfillmentBatchItem
from models.user import User
from repositories.fulfillment_repository import FulfillmentBatchItemHistoryRepository, FulfillmentBatchRepository
from repositories.inventory_repository import WarehouseRepository
from services.fulfillment_carrier import INTERNAL_CARRIERS
from services.fulfillment_service import (
    BatchItemDisplay,
    BatchItemOutcome,
    FulfillmentConflictError,
    FulfillmentService,
    FulfillmentValidationError,
)

router = APIRouter(
    prefix="/api/fulfillment", tags=["fulfillment"], dependencies=[Depends(require_permission("SHIPMENT_VIEW"))]
)


def _validation_error_status(message: str) -> int:
    return status.HTTP_404_NOT_FOUND if "찾을 수 없습니다" in message else status.HTTP_400_BAD_REQUEST


class CarrierOut(BaseModel):
    code: str
    label: str


@router.get("/carriers", response_model=list[CarrierOut], summary="내부 택배사 코드 목록(창고 기록용)")
def list_carriers() -> list[CarrierOut]:
    return [CarrierOut(code=code, label=label) for code, label in INTERNAL_CARRIERS]


class WarehouseOut(BaseModel):
    id: int
    name: str


@router.get("/warehouses", response_model=list[WarehouseOut], summary="출고 배치 생성에 쓸 활성 창고 목록")
def list_warehouses(db=Depends(get_db)) -> list[WarehouseOut]:
    return [WarehouseOut(id=w.id, name=w.name) for w in WarehouseRepository(db).list_active()]


class FulfillableOrderItemOut(BaseModel):
    order_id: int
    order_item_id: int
    platform_order_no: str
    product_option_id: int
    sku_code: str
    product_name: str
    order_quantity: int
    remaining_quantity: int


@router.get(
    "/fulfillable-order-items",
    response_model=list[FulfillableOrderItemOut],
    summary="출고 배치에 담을 수 있는 주문라인 조회",
    description="아직 완전히 이행되지 않은(잔여수량>0) 주문라인만 반환한다. 취소/반품/교환/"
    "환불/배송완료 주문은 제외된다.",
)
def list_fulfillable_order_items(
    platform_id: Optional[int] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    db=Depends(get_db),
) -> list[FulfillableOrderItemOut]:
    items = FulfillmentService(db).list_fulfillable_order_items(
        platform_id=platform_id, search=search, limit=limit, offset=offset
    )
    return [
        FulfillableOrderItemOut(
            order_id=i.order_id,
            order_item_id=i.order_item_id,
            platform_order_no=i.platform_order_no,
            product_option_id=i.product_option_id,
            sku_code=i.sku_code,
            product_name=i.product_name,
            order_quantity=i.order_quantity,
            remaining_quantity=i.remaining_quantity,
        )
        for i in items
    ]


class BatchSelection(BaseModel):
    order_item_id: int
    quantity: int


class CreateBatchRequest(BaseModel):
    warehouse_id: int
    selections: list[BatchSelection]
    note: Optional[str] = None


class BatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    warehouse_id: int
    status: str
    note: Optional[str] = None
    created_by: Optional[int] = None
    created_at: datetime


class BatchItemOut(BaseModel):
    id: int
    batch_id: int
    order_id: int
    order_item_id: int
    product_option_id: int
    platform_order_no: str
    sku_code: str
    product_name: str
    requested_quantity: int
    picked_quantity: Optional[int] = None
    verified_quantity: Optional[int] = None
    status: str
    failure_reason_code: Optional[str] = None
    shipment_id: Optional[int] = None
    picked_at: Optional[datetime] = None
    verified_at: Optional[datetime] = None
    packed_at: Optional[datetime] = None


def _to_batch_item_out(display: BatchItemDisplay) -> BatchItemOut:
    item = display.item
    return BatchItemOut(
        id=item.id,
        batch_id=item.batch_id,
        order_id=item.order_id,
        order_item_id=item.order_item_id,
        product_option_id=item.product_option_id,
        platform_order_no=display.platform_order_no,
        sku_code=display.sku_code,
        product_name=display.product_name,
        requested_quantity=item.requested_quantity,
        picked_quantity=item.picked_quantity,
        verified_quantity=item.verified_quantity,
        status=item.status,
        failure_reason_code=item.failure_reason_code,
        shipment_id=item.shipment_id,
        picked_at=item.picked_at,
        verified_at=item.verified_at,
        packed_at=item.packed_at,
    )


class BatchDetailOut(BaseModel):
    batch: BatchOut
    items: list[BatchItemOut]


@router.post(
    "/batches",
    response_model=BatchDetailOut,
    status_code=status.HTTP_201_CREATED,
    summary="출고 배치 생성",
    description="선택한 주문라인들을 하나의 출고 배치로 묶는다. 이미 다른 배치에 배정된 "
    "수량/이미 발송된 수량을 뺀 잔여수량을 초과하면 400을 반환한다.",
    responses={400: {"description": "잔여수량 초과, 취소/반품 주문, 이미 배송 연결됨 등."}},
)
def create_batch(
    payload: CreateBatchRequest, db=Depends(get_db), current_user: User = Depends(get_current_user)
) -> BatchDetailOut:
    service = FulfillmentService(db)
    try:
        batch = service.create_batch(
            payload.warehouse_id, [(s.order_item_id, s.quantity) for s in payload.selections], current_user.id
        )
    except FulfillmentValidationError as e:
        db.rollback()
        raise HTTPException(status_code=_validation_error_status(str(e)), detail=str(e)) from e
    db.commit()
    items = service.get_batch_items_with_display(batch.id)
    return BatchDetailOut(
        batch=BatchOut.model_validate(batch, from_attributes=True), items=[_to_batch_item_out(i) for i in items]
    )


@router.get("/batches", response_model=list[BatchOut], summary="출고 배치 목록 조회")
def list_batches(
    status_filter: Optional[str] = None,
    warehouse_id: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
    db=Depends(get_db),
) -> list[BatchOut]:
    batches = FulfillmentBatchRepository(db).list_filtered(
        status=status_filter, warehouse_id=warehouse_id, limit=limit, offset=offset
    )
    return [BatchOut.model_validate(b, from_attributes=True) for b in batches]


@router.get(
    "/batches/{batch_id}",
    response_model=BatchDetailOut,
    summary="출고 배치 상세(항목 목록 포함) 조회",
    responses={404: {"description": "배치를 찾을 수 없습니다."}},
)
def get_batch(batch_id: int, db=Depends(get_db)) -> BatchDetailOut:
    batch = FulfillmentBatchRepository(db).get_by_id(batch_id)
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="배치를 찾을 수 없습니다.")
    items = FulfillmentService(db).get_batch_items_with_display(batch_id)
    return BatchDetailOut(
        batch=BatchOut.model_validate(batch, from_attributes=True), items=[_to_batch_item_out(i) for i in items]
    )


class ProgressItemOut(BaseModel):
    batch_item: BatchItemOut
    command_status: Optional[str] = None
    command_id: Optional[int] = None
    command_error_code: Optional[str] = None


@router.get(
    "/batches/{batch_id}/progress",
    response_model=list[ProgressItemOut],
    summary="출고 배치 진행상태 조회(항목별 + 채널 전송 명령 상태)",
    description="command_status는 SHIPMENT_SUBMIT ExternalCommand의 상태다(PENDING/RUNNING/"
    "SUCCESS/FAILED/RETRY_WAIT/UNKNOWN/CANCELLED) - 화면은 SUCCESS를 확인한 뒤에만 "
    "채널 전송 완료로 표시해야 한다.",
)
def get_batch_progress(batch_id: int, db=Depends(get_db)) -> list[ProgressItemOut]:
    service = FulfillmentService(db)
    progress = service.get_batch_progress(batch_id)
    displays = {d.item.id: d for d in service.get_batch_items_with_display(batch_id)}
    result = []
    for row in progress:
        item: FulfillmentBatchItem = row["batch_item"]
        display = displays.get(item.id)
        item_out = _to_batch_item_out(display) if display else None
        if item_out is None:
            continue
        result.append(
            ProgressItemOut(
                batch_item=item_out,
                command_status=row["command_status"],
                command_id=row["command_id"],
                command_error_code=row["command_error_code"],
            )
        )
    return result


class HistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    batch_item_id: int
    from_status: Optional[str] = None
    to_status: str
    changed_by: Optional[int] = None
    changed_at: datetime
    note: Optional[str] = None


@router.get("/batches/{batch_id}/history", response_model=list[HistoryOut], summary="출고 작업 이력 조회")
def get_batch_history(batch_id: int, db=Depends(get_db)) -> list[HistoryOut]:
    rows = FulfillmentBatchItemHistoryRepository(db).list_by_batch(batch_id)
    return [HistoryOut.model_validate(r, from_attributes=True) for r in rows]


class TransitionRequest(BaseModel):
    expected_status: str


class CompletePickingRequest(BaseModel):
    expected_status: str
    picked_quantity: int


class CompleteVerificationRequest(BaseModel):
    expected_status: str
    verified_quantity: int


def _batch_item_out_single(db, item: FulfillmentBatchItem) -> BatchItemOut:
    displays = {d.item.id: d for d in FulfillmentService(db).get_batch_items_with_display(item.batch_id)}
    display = displays.get(item.id)
    assert display is not None
    return _to_batch_item_out(display)


@router.post(
    "/batch-items/{batch_item_id}/start-picking",
    response_model=BatchItemOut,
    summary="피킹 시작",
    responses={409: {"description": "현재 상태가 요청한 expected_status와 다릅니다(다른 사용자가 이미 처리함)."}},
)
def start_picking(
    batch_item_id: int, payload: TransitionRequest, db=Depends(get_db), current_user: User = Depends(get_current_user)
) -> BatchItemOut:
    service = FulfillmentService(db)
    try:
        item = service.start_picking(batch_item_id, payload.expected_status, current_user.id)
    except FulfillmentConflictError as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except FulfillmentValidationError as e:
        db.rollback()
        raise HTTPException(status_code=_validation_error_status(str(e)), detail=str(e)) from e
    db.commit()
    return _batch_item_out_single(db, item)


@router.post(
    "/batch-items/{batch_item_id}/complete-picking",
    response_model=BatchItemOut,
    summary="피킹 완료(실제 수량 입력)",
    responses={
        400: {"description": "피킹 수량이 요청수량 범위를 벗어남."},
        409: {"description": "현재 상태가 요청한 expected_status와 다릅니다."},
    },
)
def complete_picking(
    batch_item_id: int,
    payload: CompletePickingRequest,
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> BatchItemOut:
    service = FulfillmentService(db)
    try:
        item = service.complete_picking(
            batch_item_id, payload.expected_status, current_user.id, payload.picked_quantity
        )
    except FulfillmentConflictError as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except FulfillmentValidationError as e:
        db.rollback()
        raise HTTPException(status_code=_validation_error_status(str(e)), detail=str(e)) from e
    db.commit()
    return _batch_item_out_single(db, item)


@router.post(
    "/batch-items/{batch_item_id}/start-verification",
    response_model=BatchItemOut,
    summary="검수 시작",
    responses={409: {"description": "현재 상태가 요청한 expected_status와 다릅니다."}},
)
def start_verification(
    batch_item_id: int, payload: TransitionRequest, db=Depends(get_db), current_user: User = Depends(get_current_user)
) -> BatchItemOut:
    service = FulfillmentService(db)
    try:
        item = service.start_verification(batch_item_id, payload.expected_status, current_user.id)
    except FulfillmentConflictError as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except FulfillmentValidationError as e:
        db.rollback()
        raise HTTPException(status_code=_validation_error_status(str(e)), detail=str(e)) from e
    db.commit()
    return _batch_item_out_single(db, item)


@router.post(
    "/batch-items/{batch_item_id}/complete-verification",
    response_model=BatchItemOut,
    summary="검수 완료(확정 수량 입력) - 피킹수량과 다르면 BLOCKED로 남는다",
    responses={
        400: {"description": "검수 수량이 피킹수량 범위를 벗어남."},
        409: {"description": "현재 상태가 요청한 expected_status와 다릅니다."},
    },
)
def complete_verification(
    batch_item_id: int,
    payload: CompleteVerificationRequest,
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> BatchItemOut:
    service = FulfillmentService(db)
    try:
        item = service.complete_verification(
            batch_item_id, payload.expected_status, current_user.id, payload.verified_quantity
        )
    except FulfillmentConflictError as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except FulfillmentValidationError as e:
        db.rollback()
        raise HTTPException(status_code=_validation_error_status(str(e)), detail=str(e)) from e
    db.commit()
    return _batch_item_out_single(db, item)


@router.post(
    "/batch-items/{batch_item_id}/cancel",
    response_model=BatchItemOut,
    summary="출고 배치 항목 취소",
    description="READY~PACKED 단계에서만 취소할 수 있다(SUBMIT_PENDING/SUBMITTED는 이미 "
    "채널 전송 명령이 생성되어 취소 경로가 없다). 포장완료(재고 차감) 이후 취소하면 "
    "재고를 자동으로 복원한다.",
    responses={
        400: {"description": "이미 채널 전송 완료됐거나 취소할 수 없는 상태입니다."},
        409: {"description": "현재 상태가 요청한 expected_status와 다릅니다."},
    },
)
def cancel_batch_item(
    batch_item_id: int, payload: TransitionRequest, db=Depends(get_db), current_user: User = Depends(get_current_user)
) -> BatchItemOut:
    service = FulfillmentService(db)
    try:
        item = service.cancel_item(batch_item_id, payload.expected_status, current_user.id)
    except FulfillmentConflictError as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except FulfillmentValidationError as e:
        db.rollback()
        raise HTTPException(status_code=_validation_error_status(str(e)), detail=str(e)) from e
    db.commit()
    return _batch_item_out_single(db, item)


class PackRequest(BaseModel):
    batch_item_ids: list[int]
    carrier: str
    tracking_no: str


class BatchItemOutcomeOut(BaseModel):
    batch_item_id: int
    outcome: str
    error_code: Optional[str] = None


def _to_outcome_out(outcome: BatchItemOutcome) -> BatchItemOutcomeOut:
    return BatchItemOutcomeOut(
        batch_item_id=outcome.batch_item_id, outcome=outcome.outcome, error_code=outcome.error_code
    )


@router.post(
    "/batch-items/pack",
    response_model=list[BatchItemOutcomeOut],
    summary="검수완료 항목 포장완료 + 송장등록(일괄)",
    description="검수완료(VERIFIED)된 항목들을 하나의 송장번호로 묶어 포장완료 처리한다 - "
    "재고를 정확히 한 번 차감하고 배송(Shipment) 레코드를 생성한다. 채널 전송은 하지 않는다"
    "(POST /shipments/submit을 따로 호출해야 한다). 일부 항목이 실패해도 나머지는 유지된다.",
)
def pack_batch_items(
    payload: PackRequest, db=Depends(get_db), current_user: User = Depends(get_current_user)
) -> list[BatchItemOutcomeOut]:
    outcomes = FulfillmentService(db).pack_and_register_tracking(
        payload.batch_item_ids, payload.carrier, payload.tracking_no, current_user.id
    )
    db.commit()
    return [_to_outcome_out(o) for o in outcomes]


class SubmitRequest(BaseModel):
    shipment_ids: list[int]


@router.post(
    "/shipments/submit",
    response_model=list[BatchItemOutcomeOut],
    status_code=status.HTTP_202_ACCEPTED,
    summary="포장완료 항목 채널 송장 전송 접수(일괄)",
    description="실제 채널 HTTP 호출은 하지 않는다 - services.shipment_dispatch_service."
    "ShipmentDispatchService.enqueue()로 PENDING 명령만 생성한다. 실제 전송은 기존 "
    "outbox_dispatch_job이 비동기로 수행하며, 진행상태는 GET /batches/{batch_id}/progress로 "
    "폴링한다.",
)
def submit_shipments_to_channel(
    payload: SubmitRequest, db=Depends(get_db), current_user: User = Depends(get_current_user)
) -> list[BatchItemOutcomeOut]:
    outcomes = FulfillmentService(db).submit_to_channel(payload.shipment_ids, current_user.id)
    db.commit()
    return [_to_outcome_out(o) for o in outcomes]


class RetryRequest(BaseModel):
    batch_item_ids: list[int]


@router.post(
    "/batch-items/retry",
    response_model=list[BatchItemOutcomeOut],
    summary="채널 전송 실패(FAILED) 항목 선택 재처리",
    description="FAILED 상태 명령만 재처리한다. UNKNOWN은 이 API로 재처리되지 않는다"
    "(outcome=BLOCKED, error_code=UNKNOWN_REQUIRES_RESOLUTION) - "
    "POST /api/shipments/commands/{command_id}/resolve로 운영자가 직접 해소해야 한다.",
)
def retry_failed_batch_items(payload: RetryRequest, db=Depends(get_db)) -> list[BatchItemOutcomeOut]:
    outcomes = FulfillmentService(db).retry_failed_items(payload.batch_item_ids)
    db.commit()
    return [_to_outcome_out(o) for o in outcomes]
