"""
api/routers/costs.py
------------------------
비용 조회/등록.
"""

from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.deps import get_db, require_permission
from models.cost import Cost
from repositories.cost_repository import CostRepository

router = APIRouter(prefix="/api/costs", tags=["costs"], dependencies=[Depends(require_permission("COST_MANAGE"))])


class CostOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category: str
    cost_type: str
    platform_id: Optional[int]
    product_option_id: Optional[int]
    order_id: Optional[int]
    amount: float
    incurred_date: date
    memo: Optional[str]


class CostCreate(BaseModel):
    category: str
    cost_type: str
    platform_id: Optional[int] = None
    product_option_id: Optional[int] = None
    order_id: Optional[int] = None
    amount: float
    incurred_date: date
    memo: Optional[str] = None


@router.get(
    "",
    response_model=list[CostOut],
    summary="비용 목록 조회",
    description="category를 지정하면 해당 분류(배송비/포장비/기타 등)의 비용만, 지정하지 않으면 "
    "최근 순으로 최대 200건을 반환한다.",
)
def list_costs(category: Optional[str] = None, db: Session = Depends(get_db)) -> list:
    repo = CostRepository(db)
    return repo.list_by_category(category) if category else repo.list_all(limit=200)


@router.post(
    "",
    response_model=CostOut,
    status_code=status.HTTP_201_CREATED,
    summary="비용 등록",
    description="배송비/포장비/기타 등 개별 비용 1건을 등록한다.",
    responses={409: {"description": "platform_id/product_option_id/order_id 등 참조 값이 유효하지 않습니다."}},
)
def create_cost(payload: CostCreate, db: Session = Depends(get_db)) -> Cost:
    cost = Cost(**payload.model_dump(), created_at=datetime.now(timezone.utc))
    CostRepository(db).add(cost)
    db.commit()
    return cost
