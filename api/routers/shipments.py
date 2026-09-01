"""
api/routers/shipments.py
-----------------------------
배송 등록/조회/상태 변경. SRS FR-ORD-01/02 대응.

이 메뉴는 scripts/init_db.py의 DEFAULT_PERMISSIONS에서 SHIPMENT_VIEW 하나만
정의되어 있다(다른 화면들도 costs.py/ads.py처럼 조회/쓰기를 하나의 권한으로
묶는 경우가 있어 이를 따른다) - 조회뿐 아니라 등록/수정/상태변경도 전부
SHIPMENT_VIEW로 보호한다.
"""

from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.deps import get_current_user, get_db, require_permission
from models.order import Shipment
from models.user import User
from repositories.integration_sync_repository import ExternalCommandRepository
from repositories.order_repository import OrderRepository, ShipmentRepository
from services.shipment_dispatch_service import (
    ShipmentChannelSubmitDisabledError,
    ShipmentDispatchService,
    ShipmentNotReadyError,
    ShipmentPlatformMismatchError,
)
from services.shipment_service import InvalidShipmentStatusError, ShipmentAlreadyExistsError, ShipmentService

router = APIRouter(
    prefix="/api/shipments", tags=["shipments"], dependencies=[Depends(require_permission("SHIPMENT_VIEW"))]
)


class ShipmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    # 합포장(주문 여러 건 → 송장 1장)을 지원하므로 단일 order_id가 아니라 목록이다.
    order_ids: list[int]
    carrier: Optional[str]
    tracking_no: Optional[str]
    shipped_at: Optional[datetime]
    delivered_at: Optional[datetime]
    status: str


def _to_shipment_out(shipment: Shipment, repo: ShipmentRepository) -> ShipmentOut:
    return ShipmentOut(
        id=shipment.id,
        order_ids=repo.list_orders_of_shipment(shipment.id),
        carrier=shipment.carrier,
        tracking_no=shipment.tracking_no,
        shipped_at=shipment.shipped_at,
        delivered_at=shipment.delivered_at,
        status=shipment.status,
    )


class ShipmentListOut(BaseModel):
    items: list[ShipmentOut]
    total: int
    page: int
    page_size: int


class ShipmentCreate(BaseModel):
    order_id: int
    carrier: Optional[str] = None
    tracking_no: Optional[str] = None


class ShipmentInfoUpdate(BaseModel):
    carrier: Optional[str] = None
    tracking_no: Optional[str] = None


class ShipmentConsolidateRequest(BaseModel):
    """합포장 - 같은 수취인의 주문 여러 건을 송장 1장으로 묶는다."""

    order_ids: list[int]
    carrier: Optional[str] = None
    tracking_no: Optional[str] = None


class ShipmentStatusUpdate(BaseModel):
    status: Literal["READY", "SHIPPING", "DELIVERED"]
    warehouse_id: Optional[int] = None


@router.get(
    "",
    response_model=ShipmentListOut,
    summary="배송 목록 조회",
    description="status/order_id/search(운송사·송장번호)로 필터링하고 page/page_size로 페이지네이션한다.",
)
def list_shipments(
    status_filter: Optional[str] = None,
    order_id: Optional[int] = None,
    search: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
) -> ShipmentListOut:
    items, total = ShipmentService(db).list(
        status=status_filter, order_id=order_id, search=search, page=page, page_size=page_size
    )
    repo = ShipmentRepository(db)
    return ShipmentListOut(
        items=[_to_shipment_out(i, repo) for i in items], total=total, page=page, page_size=page_size
    )


@router.get(
    "/{shipment_id}",
    response_model=ShipmentOut,
    summary="배송 단건 조회",
    responses={404: {"description": "배송 정보를 찾을 수 없습니다."}},
)
def get_shipment(shipment_id: int, db: Session = Depends(get_db)) -> ShipmentOut:
    repo = ShipmentRepository(db)
    shipment = repo.get_by_id(shipment_id)
    if shipment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="배송 정보를 찾을 수 없습니다.")
    return _to_shipment_out(shipment, repo)


@router.post(
    "",
    response_model=ShipmentOut,
    status_code=status.HTTP_201_CREATED,
    summary="배송 등록",
    description="주문 1건당 배송은 1건만 등록할 수 있다(이미 있으면 409).",
    responses={
        404: {"description": "주문을 찾을 수 없습니다."},
        409: {"description": "이미 배송 레코드가 있는 주문입니다."},
    },
)
def create_shipment(payload: ShipmentCreate, db: Session = Depends(get_db)) -> ShipmentOut:
    if OrderRepository(db).get_by_id(payload.order_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="주문을 찾을 수 없습니다.")
    try:
        shipment = ShipmentService(db).create(payload.order_id, payload.carrier, payload.tracking_no)
    except ShipmentAlreadyExistsError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    db.commit()
    return _to_shipment_out(shipment, ShipmentRepository(db))


@router.post(
    "/consolidate",
    response_model=ShipmentOut,
    status_code=status.HTTP_201_CREATED,
    summary="합포장(묶음배송) 생성",
    description="같은 수취인의 주문 여러 건을 배송 1건(송장 1장)으로 묶는다. 배송비 절감용. "
    "이미 배송이 있는 주문이 포함되면 409.",
    responses={
        404: {"description": "주문을 찾을 수 없습니다."},
        409: {"description": "이미 배송이 있는 주문이 포함되어 있습니다."},
        400: {"description": "합포장할 주문이 없습니다."},
    },
)
def consolidate_shipment(payload: ShipmentConsolidateRequest, db: Session = Depends(get_db)) -> ShipmentOut:
    order_repo = OrderRepository(db)
    for oid in payload.order_ids:
        if order_repo.get_by_id(oid) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"주문을 찾을 수 없습니다: {oid}")
    try:
        shipment = ShipmentService(db).consolidate(payload.order_ids, payload.carrier, payload.tracking_no)
    except ShipmentAlreadyExistsError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    db.commit()
    return _to_shipment_out(shipment, ShipmentRepository(db))


@router.patch(
    "/{shipment_id}",
    response_model=ShipmentOut,
    summary="배송 정보 수정",
    description="운송사/송장번호를 수정한다(상태 변경은 PATCH /{shipment_id}/status 사용).",
    responses={404: {"description": "배송 정보를 찾을 수 없습니다."}},
)
def update_shipment(shipment_id: int, payload: ShipmentInfoUpdate, db: Session = Depends(get_db)) -> ShipmentOut:
    repo = ShipmentRepository(db)
    shipment = repo.get_by_id(shipment_id)
    if shipment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="배송 정보를 찾을 수 없습니다.")
    updated = ShipmentService(db).update_info(shipment, payload.carrier, payload.tracking_no)
    db.commit()
    return _to_shipment_out(updated, repo)


@router.patch(
    "/{shipment_id}/status",
    response_model=ShipmentOut,
    summary="배송 상태 변경",
    description="READY -> SHIPPING -> DELIVERED로 상태를 바꾼다. SHIPPING/DELIVERED로 바뀔 때는 "
    "연결된 주문 상태도 함께 동기화되며, warehouse_id를 지정하면 재고도 함께 반영된다.",
    responses={404: {"description": "배송 정보를 찾을 수 없습니다."}},
)
def change_shipment_status(
    shipment_id: int, payload: ShipmentStatusUpdate, db: Session = Depends(get_db)
) -> ShipmentOut:
    repo = ShipmentRepository(db)
    shipment = repo.get_by_id(shipment_id)
    if shipment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="배송 정보를 찾을 수 없습니다.")
    try:
        updated = ShipmentService(db).change_status(shipment, payload.status, payload.warehouse_id)
    except InvalidShipmentStatusError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    db.commit()
    return _to_shipment_out(updated, repo)


class ShipmentSubmitOut(BaseModel):
    command_id: int
    status: str
    already_processed: bool
    error_code: Optional[str] = None


@router.post(
    "/{shipment_id}/submit",
    response_model=ShipmentSubmitOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="채널로 송장 전송 요청(비동기)",
    description="READY 상태의 배송을 채널(네이버/쿠팡)에 전송할 명령을 생성한다. "
    "이 API는 실제 채널 호출을 하지 않고 명령만 접수한다(202) - 실제 전송은 스케줄러의 "
    "outbox_dispatch_job이 비동기로 수행하며, 처리 결과는 GET /api/shipments/commands/"
    "{command_id}로 폴링해 확인해야 한다(PENDING -> RUNNING -> SUCCESS/FAILED/RETRY_WAIT). "
    "같은 배송에 이미 생성된 명령이 있으면(같은 송장번호로 재요청/버튼 연타 포함) 새로 "
    "만들지 않고 기존 명령을 그대로 반환한다(idempotent).",
    responses={
        404: {"description": "배송 정보를 찾을 수 없습니다."},
        400: {"description": "배송이 READY 상태가 아니거나 필수 정보가 없습니다."},
        503: {"description": "채널 전송 기능이 비활성화(OFF) 상태입니다(실계정 검증 승인 전)."},
    },
)
def submit_shipment(shipment_id: int, db: Session = Depends(get_db)) -> ShipmentSubmitOut:
    service = ShipmentDispatchService(db)
    try:
        outcome = service.enqueue(shipment_id)
    except ShipmentChannelSubmitDisabledError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except (ShipmentNotReadyError, ShipmentPlatformMismatchError) as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    db.commit()
    return ShipmentSubmitOut(
        command_id=outcome.command.id,
        status=outcome.command.status,
        already_processed=outcome.already_processed,
        error_code=outcome.command.error_code,
    )


class ExternalCommandOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    attempt_count: int
    retryable: bool
    error_code: Optional[str]
    next_retry_at: Optional[datetime]
    completed_at: Optional[datetime]


@router.get(
    "/commands/{command_id}",
    response_model=ExternalCommandOut,
    summary="채널 전송 명령 상태 조회",
    description="POST /{shipment_id}/submit이 반환한 command_id로 처리 상태를 폴링한다. "
    "화면은 status가 SUCCESS로 확인된 뒤에만 성공으로 표시해야 한다(PENDING/RUNNING은 "
    "진행 중, RETRY_WAIT은 재시도 대기, FAILED는 확정 실패).",
    responses={404: {"description": "명령을 찾을 수 없습니다."}},
)
def get_shipment_submit_command(command_id: int, db: Session = Depends(get_db)) -> ExternalCommandOut:
    command = ExternalCommandRepository(db).get_by_id(command_id)
    if command is None or command.target_type != "SHIPMENT":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="명령을 찾을 수 없습니다.")
    return ExternalCommandOut.model_validate(command, from_attributes=True)


class ResolveUnknownCommandRequest(BaseModel):
    resolution: Literal["CONFIRMED_NOT_SENT", "CONFIRMED_SUCCESS", "CONFIRMED_FAILED"]


@router.post(
    "/commands/{command_id}/resolve",
    response_model=ExternalCommandOut,
    summary="결과 확인 필요(UNKNOWN) 명령 수동 해소",
    description="채널이 실제로 처리했는지 알 수 없는(UNKNOWN) 명령을, 운영자가 채널을 직접 "
    "확인한 뒤 해소한다. CONFIRMED_NOT_SENT는 재시도 대상(PENDING)으로 되돌리고, "
    "CONFIRMED_SUCCESS는 SUCCESS로 확정(중복 전송 없이 성공 후 처리 실행), "
    "CONFIRMED_FAILED는 FAILED로 확정한다. 해소 이력은 감사로그에 남는다.",
    responses={
        404: {"description": "명령을 찾을 수 없습니다."},
        400: {"description": "UNKNOWN 상태가 아니거나 알 수 없는 해소 방식입니다."},
    },
)
def resolve_unknown_command(
    command_id: int,
    payload: ResolveUnknownCommandRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ExternalCommandOut:
    existing = ExternalCommandRepository(db).get_by_id(command_id)
    if existing is None or existing.target_type != "SHIPMENT":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="명령을 찾을 수 없습니다.")
    try:
        resolved = ShipmentDispatchService(db).resolve_unknown_command(
            command_id, payload.resolution, resolved_by=current_user.id
        )
    except ValueError as e:
        db.rollback()
        code = status.HTTP_404_NOT_FOUND if "찾을 수 없습니다" in str(e) else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=code, detail=str(e)) from e
    db.commit()
    return ExternalCommandOut.model_validate(resolved, from_attributes=True)
