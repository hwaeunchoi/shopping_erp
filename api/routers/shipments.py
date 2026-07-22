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

from api.deps import get_db, require_permission
from models.order import Shipment
from repositories.order_repository import OrderRepository, ShipmentRepository
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
