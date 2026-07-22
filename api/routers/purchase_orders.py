"""
api/routers/purchase_orders.py
----------------------------------
공급처 발주 관리: 작성(DRAFT)/품목 추가/확정(ORDERED)/입고 처리/취소.
SUPPLIER_MANAGE 권한을 그대로 재사용한다(발주는 공급처 관리와 밀접한 하위 기능).
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.deps import get_db, require_permission
from repositories.purchase_order_repository import PurchaseOrderRepository
from repositories.supplier_repository import SupplierRepository
from services.purchase_order_service import PurchaseOrderService, PurchaseOrderStateError

router = APIRouter(
    prefix="/api/purchase-orders",
    tags=["purchase-orders"],
    dependencies=[Depends(require_permission("SUPPLIER_MANAGE"))],
)


class PurchaseOrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    purchase_order_id: int
    product_option_id: int
    quantity: int
    unit_cost: float
    received_quantity: int


class PurchaseOrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    supplier_id: int
    status: str
    order_date: Optional[datetime] = None
    memo: Optional[str] = None
    items: list[PurchaseOrderItemOut] = []


class PurchaseOrderCreateRequest(BaseModel):
    supplier_id: int
    memo: Optional[str] = None


class PurchaseOrderItemCreateRequest(BaseModel):
    product_option_id: int
    quantity: int
    unit_cost: float = 0


class PurchaseOrderReceiveRequest(BaseModel):
    quantity: int
    warehouse_id: int


@router.get(
    "",
    response_model=list[PurchaseOrderOut],
    summary="발주 목록 조회",
    description="supplier_id 또는 status로 필터링할 수 있다. 아무 것도 지정하지 않으면 전체를 반환한다.",
)
def list_purchase_orders(
    supplier_id: Optional[int] = None, status_filter: Optional[str] = None, db: Session = Depends(get_db)
) -> list:
    repo = PurchaseOrderRepository(db)
    if supplier_id is not None:
        return repo.list_by_supplier(supplier_id)
    if status_filter is not None:
        return repo.list_by_status(status_filter)
    return repo.list_all(limit=500)


@router.post(
    "",
    response_model=PurchaseOrderOut,
    status_code=status.HTTP_201_CREATED,
    summary="발주 작성(DRAFT)",
    responses={404: {"description": "공급처를 찾을 수 없습니다."}},
)
def create_purchase_order(payload: PurchaseOrderCreateRequest, db: Session = Depends(get_db)):
    if SupplierRepository(db).get_by_id(payload.supplier_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="공급처를 찾을 수 없습니다.")
    po = PurchaseOrderService(db).create_draft(payload.supplier_id, payload.memo)
    db.commit()
    return po


@router.get(
    "/{purchase_order_id}",
    response_model=PurchaseOrderOut,
    summary="발주 단건 조회(품목 포함)",
    responses={404: {"description": "발주를 찾을 수 없습니다."}},
)
def get_purchase_order(purchase_order_id: int, db: Session = Depends(get_db)):
    po = PurchaseOrderRepository(db).get_by_id(purchase_order_id)
    if po is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="발주를 찾을 수 없습니다.")
    return po


@router.post(
    "/{purchase_order_id}/items",
    response_model=PurchaseOrderItemOut,
    status_code=status.HTTP_201_CREATED,
    summary="발주 품목 추가",
    description="DRAFT 상태의 발주에만 품목을 추가할 수 있다.",
    responses={404: {"description": "발주를 찾을 수 없습니다."}, 409: {"description": "DRAFT 상태가 아닙니다."}},
)
def add_purchase_order_item(
    purchase_order_id: int, payload: PurchaseOrderItemCreateRequest, db: Session = Depends(get_db)
):
    try:
        item = PurchaseOrderService(db).add_item(
            purchase_order_id, payload.product_option_id, payload.quantity, payload.unit_cost
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except PurchaseOrderStateError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    db.commit()
    return item


@router.post(
    "/{purchase_order_id}/confirm",
    response_model=PurchaseOrderOut,
    summary="발주 확정(DRAFT → ORDERED)",
    responses={404: {"description": "발주를 찾을 수 없습니다."}, 409: {"description": "확정할 수 없는 상태입니다."}},
)
def confirm_purchase_order(purchase_order_id: int, db: Session = Depends(get_db)):
    try:
        po = PurchaseOrderService(db).confirm(purchase_order_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except PurchaseOrderStateError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    db.commit()
    return po


@router.post(
    "/{purchase_order_id}/cancel",
    response_model=PurchaseOrderOut,
    summary="발주 취소",
    responses={404: {"description": "발주를 찾을 수 없습니다."}, 409: {"description": "취소할 수 없는 상태입니다."}},
)
def cancel_purchase_order(purchase_order_id: int, db: Session = Depends(get_db)):
    try:
        po = PurchaseOrderService(db).cancel(purchase_order_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except PurchaseOrderStateError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    db.commit()
    return po


@router.post(
    "/{purchase_order_id}/items/{item_id}/receive",
    response_model=PurchaseOrderItemOut,
    summary="발주 품목 입고 처리",
    description="입고 수량만큼 실재고를 증가시키고 입출고 이력을 남긴다(부분 입고 가능). "
    "품목 전량이 입고되면 발주 상태가 RECEIVED로, 일부만 입고되면 PARTIALLY_RECEIVED로 바뀐다.",
    responses={
        404: {"description": "발주 또는 품목을 찾을 수 없습니다."},
        409: {"description": "입고 처리할 수 없는 상태이거나 수량이 유효하지 않습니다."},
    },
)
def receive_purchase_order_item(
    purchase_order_id: int, item_id: int, payload: PurchaseOrderReceiveRequest, db: Session = Depends(get_db)
):
    try:
        item = PurchaseOrderService(db).receive_item(purchase_order_id, item_id, payload.quantity, payload.warehouse_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except PurchaseOrderStateError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    db.commit()
    return item
