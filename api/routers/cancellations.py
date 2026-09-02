"""
api/routers/cancellations.py
---------------------------------
취소 등록/조회/상태 변경. SRS FR-ORD-03/04 대응.

scripts/init_db.py의 DEFAULT_PERMISSIONS는 교환/반품/취소를 하나의 권한
EXCHANGE_RETURN_MANAGE로 묶어 정의하므로(메뉴 "교환/반품"), 이 라우터도
동일 권한 하나로 조회/등록/수정을 전부 보호한다.
"""

from datetime import date, datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.deps import get_db, require_permission
from models.order import Cancellation
from repositories.order_repository import CancellationRepository, OrderRepository
from services.exchange_return_service import CancellationService, InvalidStatusError
from services.export_service import cancellations_to_excel

router = APIRouter(
    prefix="/api/cancellations",
    tags=["cancellations"],
    dependencies=[Depends(require_permission("EXCHANGE_RETURN_MANAGE"))],
)


class CancellationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    reason: Optional[str]
    refund_amount: Optional[float]
    status: str
    requested_at: datetime
    completed_at: Optional[datetime]
    platform_claim_id: Optional[str] = None
    raw_status: Optional[str] = None
    fault_type: Optional[str] = None
    quantity: Optional[int] = None
    shipping_fee: Optional[float] = None


class CancellationListOut(BaseModel):
    items: list[CancellationOut]
    total: int
    page: int
    page_size: int


class CancellationCreate(BaseModel):
    order_id: int
    reason: Optional[str] = None
    refund_amount: Optional[float] = None


class CancellationStatusUpdate(BaseModel):
    status: Literal["REQUESTED", "COMPLETED"]
    warehouse_id: Optional[int] = None


@router.get(
    "",
    response_model=CancellationListOut,
    summary="취소 목록 조회",
    description="status/order_id/기간(start_date~end_date)/search(사유)로 필터링하고 페이지네이션한다.",
)
def list_cancellations(
    status_filter: Optional[str] = None,
    order_id: Optional[int] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    search: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
) -> CancellationListOut:
    items, total = CancellationService(db).list(
        status=status_filter,
        order_id=order_id,
        start_date=start_date,
        end_date=end_date,
        search=search,
        page=page,
        page_size=page_size,
    )
    return CancellationListOut(
        items=[CancellationOut.model_validate(i) for i in items], total=total, page=page, page_size=page_size
    )


@router.get(
    "/export",
    summary="취소 목록 엑셀 내보내기",
    description="status_filter/order_id/start_date~end_date/search 조건에 맞는 취소 목록을 xlsx 파일로 "
    "다운로드한다(최대 10,000건). SRS FR-REPORT-04 대응.",
)
def export_cancellations(
    status_filter: Optional[str] = None,
    order_id: Optional[int] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    search: Optional[str] = None,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    cancellations = CancellationRepository(db).list_filtered(
        status=status_filter, order_id=order_id, start_date=start_date, end_date=end_date, search=search, limit=10000
    )
    buffer = cancellations_to_excel(cancellations)
    filename = f"cancellations_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get(
    "/{cancellation_id}",
    response_model=CancellationOut,
    summary="취소 단건 조회",
    responses={404: {"description": "취소 신청을 찾을 수 없습니다."}},
)
def get_cancellation(cancellation_id: int, db: Session = Depends(get_db)) -> Cancellation:
    cancellation = CancellationRepository(db).get_by_id(cancellation_id)
    if cancellation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="취소 신청을 찾을 수 없습니다.")
    return cancellation


@router.post(
    "",
    response_model=CancellationOut,
    status_code=status.HTTP_201_CREATED,
    summary="취소 신청 등록",
    responses={404: {"description": "주문을 찾을 수 없습니다."}},
)
def create_cancellation(payload: CancellationCreate, db: Session = Depends(get_db)) -> Cancellation:
    if OrderRepository(db).get_by_id(payload.order_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="주문을 찾을 수 없습니다.")
    cancellation = CancellationService(db).create(payload.order_id, payload.reason, payload.refund_amount)
    db.commit()
    return cancellation


@router.patch(
    "/{cancellation_id}/status",
    response_model=CancellationOut,
    summary="취소 상태 변경",
    description="REQUESTED -> COMPLETED로 상태를 바꾼다. COMPLETED가 되면 연결된 주문 상태도 CANCELED로 "
    "동기화되며, 주문이 아직 미출고 상태였다면 예약재고도 함께 해제된다(warehouse_id 지정 시).",
    responses={404: {"description": "취소 신청을 찾을 수 없습니다."}},
)
def change_cancellation_status(
    cancellation_id: int, payload: CancellationStatusUpdate, db: Session = Depends(get_db)
) -> Cancellation:
    cancellation = CancellationRepository(db).get_by_id(cancellation_id)
    if cancellation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="취소 신청을 찾을 수 없습니다.")
    try:
        updated = CancellationService(db).change_status(cancellation, payload.status, payload.warehouse_id)
    except InvalidStatusError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    db.commit()
    return updated
