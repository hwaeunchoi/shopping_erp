"""
api/routers/inventory.py
----------------------------
재고 조회 + 수동 조정(입고/실사) + 안전재고 기준값 변경 + 입출고 이력 조회.

조정/안전재고 변경은 product_option_id + warehouse_id를 기준으로 동작한다
(inventory_id 기준이 아님) - 이 SKU×창고 조합에 아직 재고 레코드가 없어도
(예: 신규 상품의 최초 입고) 그대로 생성되며, 있으면 그 레코드를 갱신한다.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.deps import get_db, require_permission
from repositories.inventory_repository import InventoryRepository, InventoryTransactionRepository, WarehouseRepository
from repositories.product_repository import ProductOptionRepository, ProductRepository
from services.inventory_service import InsufficientStockError, InventoryInvariantError, InventoryService

router = APIRouter(
    prefix="/api/inventory", tags=["inventory"], dependencies=[Depends(require_permission("INVENTORY_VIEW"))]
)


class InventoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_option_id: int
    warehouse_id: int
    sellable_stock: int
    reserved_stock: int
    safety_stock: int
    updated_at: datetime
    sku_code: Optional[str] = None
    option_name: Optional[str] = None
    product_name: Optional[str] = None
    warehouse_name: Optional[str] = None


class InventoryAdjustRequest(BaseModel):
    product_option_id: int
    warehouse_id: int
    delta: int
    memo: Optional[str] = None


class InventorySafetyStockRequest(BaseModel):
    product_option_id: int
    warehouse_id: int
    safety_stock: int


class InventoryTransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_option_id: int
    warehouse_id: int
    type: str
    quantity: int
    reference_type: Optional[str]
    reference_id: Optional[int]
    memo: Optional[str]
    created_at: datetime


def _to_inventory_out(inv, db: Session) -> InventoryOut:
    option = ProductOptionRepository(db).get_by_id(inv.product_option_id)
    product = ProductRepository(db).get_by_id(option.product_id) if option else None
    warehouse = WarehouseRepository(db).get_by_id(inv.warehouse_id)
    return InventoryOut(
        id=inv.id,
        product_option_id=inv.product_option_id,
        warehouse_id=inv.warehouse_id,
        sellable_stock=inv.sellable_stock,
        reserved_stock=inv.reserved_stock,
        safety_stock=inv.safety_stock,
        updated_at=inv.updated_at,
        sku_code=option.sku_code if option else None,
        option_name=option.option_name if option else None,
        product_name=product.name if product else None,
        warehouse_name=warehouse.name if warehouse else None,
    )


@router.get(
    "",
    response_model=list[InventoryOut],
    summary="재고 목록 조회",
    description="low_stock_only=true로 지정하면 안전재고(safety_stock) 미만인 항목만 반환한다. "
    "SKU/상품명/창고명을 함께 반환한다.",
)
def list_inventory(low_stock_only: bool = False, db: Session = Depends(get_db)) -> list:
    repo = InventoryRepository(db)
    items = repo.list_below_safety_stock() if low_stock_only else repo.list_all(limit=500)

    option_repo = ProductOptionRepository(db)
    product_repo = ProductRepository(db)
    warehouse_repo = WarehouseRepository(db)
    option_by_id = {o.id: o for o in (option_repo.get_by_id(i.product_option_id) for i in items) if o is not None}
    product_by_id = {
        p.id: p for p in (product_repo.get_by_id(o.product_id) for o in option_by_id.values()) if p is not None
    }
    warehouse_by_id = {w.id: w for w in (warehouse_repo.get_by_id(i.warehouse_id) for i in items) if w is not None}

    results = []
    for i in items:
        option = option_by_id.get(i.product_option_id)
        product = product_by_id.get(option.product_id) if option else None
        warehouse = warehouse_by_id.get(i.warehouse_id)
        results.append(
            InventoryOut(
                id=i.id,
                product_option_id=i.product_option_id,
                warehouse_id=i.warehouse_id,
                sellable_stock=i.sellable_stock,
                reserved_stock=i.reserved_stock,
                safety_stock=i.safety_stock,
                updated_at=i.updated_at,
                sku_code=option.sku_code if option else None,
                option_name=option.option_name if option else None,
                product_name=product.name if product else None,
                warehouse_name=warehouse.name if warehouse else None,
            )
        )
    return results


@router.post(
    "/adjust",
    response_model=InventoryOut,
    summary="재고 수동 조정(입고/실사)",
    description="product_option_id/warehouse_id 조합의 현재재고를 delta만큼 증감한다(음수 가능, 실사 감모 등). "
    "이 조합에 재고 레코드가 아직 없으면(신규 상품 최초 입고 등) 0에서 시작해 새로 만든다. "
    "조정 후 재고가 음수가 되거나 예약 재고보다 적게 남는 조정은 거부한다. "
    "모든 조정은 입출고 이력(InventoryTransaction, type=ADJUST)에 남는다.",
    responses={
        400: {"description": "조정 수량이 0입니다."},
        409: {"description": "조정 후 재고가 음수가 되거나 예약 재고를 밑돕니다."},
    },
)
def adjust_inventory(payload: InventoryAdjustRequest, db: Session = Depends(get_db)):
    try:
        updated = InventoryService(db).manual_adjust(
            payload.product_option_id, payload.warehouse_id, payload.delta, payload.memo
        )
    except (InsufficientStockError, InventoryInvariantError) as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    db.commit()
    return _to_inventory_out(updated, db)


@router.patch(
    "/safety-stock",
    response_model=InventoryOut,
    summary="안전재고 기준값 변경",
    description="product_option_id/warehouse_id 조합의 안전재고 기준값을 변경한다(재고 수량은 바뀌지 않음). "
    "레코드가 아직 없으면 새로 만든다.",
)
def update_safety_stock(payload: InventorySafetyStockRequest, db: Session = Depends(get_db)):
    updated = InventoryService(db).update_safety_stock(
        payload.product_option_id, payload.warehouse_id, payload.safety_stock
    )
    db.commit()
    return _to_inventory_out(updated, db)


@router.get(
    "/{inventory_id}/transactions",
    response_model=list[InventoryTransactionOut],
    summary="재고 입출고 이력 조회",
    description="이 SKU×창고 조합의 입출고/조정/반품입고 이력을 최신순으로 조회한다.",
    responses={404: {"description": "재고 레코드를 찾을 수 없습니다."}},
)
def list_inventory_transactions(inventory_id: int, db: Session = Depends(get_db)) -> list:
    inv = InventoryRepository(db).get_by_id(inventory_id)
    if inv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="재고 레코드를 찾을 수 없습니다.")
    return InventoryTransactionRepository(db).list_by_option_and_warehouse(inv.product_option_id, inv.warehouse_id)
