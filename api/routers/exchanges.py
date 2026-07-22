"""
api/routers/exchanges.py
-----------------------------
교환 등록/조회/상태 변경. SRS FR-ORD-03/04 대응.

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
from models.order import Exchange
from repositories.order_repository import ExchangeRepository, OrderRepository
from services.exchange_return_service import ExchangeService, InvalidStatusError
from services.export_service import exchanges_to_excel

router = APIRouter(
    prefix="/api/exchanges", tags=["exchanges"], dependencies=[Depends(require_permission("EXCHANGE_RETURN_MANAGE"))]
)


class ExchangeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    order_item_id: Optional[int]
    reason: Optional[str]
    status: str
    requested_at: datetime
    completed_at: Optional[datetime]


class ExchangeListOut(BaseModel):
    items: list[ExchangeOut]
    total: int
    page: int
    page_size: int


class ExchangeCreate(BaseModel):
    order_id: int
    order_item_id: Optional[int] = None
    reason: Optional[str] = None


class ExchangeStatusUpdate(BaseModel):
    status: Literal["REQUESTED", "APPROVED", "SHIPPED", "COMPLETED", "REJECTED"]
    warehouse_id: Optional[int] = None


@router.get(
    "",
    response_model=ExchangeListOut,
    summary="교환 목록 조회",
    description="status/order_id/기간(start_date~end_date)/search(사유)로 필터링하고 페이지네이션한다.",
)
def list_exchanges(
    status_filter: Optional[str] = None,
    order_id: Optional[int] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    search: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
) -> ExchangeListOut:
    items, total = ExchangeService(db).list(
        status=status_filter,
        order_id=order_id,
        start_date=start_date,
        end_date=end_date,
        search=search,
        page=page,
        page_size=page_size,
    )
    return ExchangeListOut(
        items=[ExchangeOut.model_validate(i) for i in items], total=total, page=page, page_size=page_size
    )


@router.get(
    "/export",
    summary="교환 목록 엑셀 내보내기",
    description="status_filter/order_id/start_date~end_date/search 조건에 맞는 교환 목록을 xlsx 파일로 "
    "다운로드한다(최대 10,000건). SRS FR-REPORT-04 대응.",
)
def export_exchanges(
    status_filter: Optional[str] = None,
    order_id: Optional[int] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    search: Optional[str] = None,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    exchanges = ExchangeRepository(db).list_filtered(
        status=status_filter, order_id=order_id, start_date=start_date, end_date=end_date, search=search, limit=10000
    )
    buffer = exchanges_to_excel(exchanges)
    filename = f"exchanges_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get(
    "/{exchange_id}",
    response_model=ExchangeOut,
    summary="교환 단건 조회",
    responses={404: {"description": "교환 신청을 찾을 수 없습니다."}},
)
def get_exchange(exchange_id: int, db: Session = Depends(get_db)) -> Exchange:
    exchange = ExchangeRepository(db).get_by_id(exchange_id)
    if exchange is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="교환 신청을 찾을 수 없습니다.")
    return exchange


@router.post(
    "",
    response_model=ExchangeOut,
    status_code=status.HTTP_201_CREATED,
    summary="교환 신청 등록",
    responses={404: {"description": "주문을 찾을 수 없습니다."}},
)
def create_exchange(payload: ExchangeCreate, db: Session = Depends(get_db)) -> Exchange:
    if OrderRepository(db).get_by_id(payload.order_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="주문을 찾을 수 없습니다.")
    exchange = ExchangeService(db).create(payload.order_id, payload.order_item_id, payload.reason)
    db.commit()
    return exchange


@router.patch(
    "/{exchange_id}/status",
    response_model=ExchangeOut,
    summary="교환 상태 변경",
    description="REQUESTED -> APPROVED -> SHIPPED -> COMPLETED(또는 REQUESTED/APPROVED에서 REJECTED)로 "
    "상태를 바꾼다. COMPLETED가 되면 연결된 주문 상태도 EXCHANGED로 동기화된다.",
    responses={404: {"description": "교환 신청을 찾을 수 없습니다."}},
)
def change_exchange_status(exchange_id: int, payload: ExchangeStatusUpdate, db: Session = Depends(get_db)) -> Exchange:
    exchange = ExchangeRepository(db).get_by_id(exchange_id)
    if exchange is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="교환 신청을 찾을 수 없습니다.")
    try:
        updated = ExchangeService(db).change_status(exchange, payload.status, payload.warehouse_id)
    except InvalidStatusError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    db.commit()
    return updated
