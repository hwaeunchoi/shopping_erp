"""
api/routers/customers.py
----------------------------
고객 조회. DEFAULT_PERMISSIONS(scripts/init_db.py)에 CUSTOMER 전용 권한이
없어(설계상 CRM 화면 권한 미정의) 로그인 여부만 검사한다.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.deps import get_current_user, get_db
from repositories.customer_repository import CustomerRepository

router = APIRouter(prefix="/api/customers", tags=["customers"], dependencies=[Depends(get_current_user)])


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    platform_id: int
    name: Optional[str]
    phone: Optional[str]
    grade: Optional[str]
    is_vip: bool
    is_dormant: bool
    total_purchase_amount: float
    order_count: int


@router.get(
    "",
    response_model=list[CustomerOut],
    summary="고객 목록 조회",
    description="vip_only 또는 dormant_only로 필터링할 수 있다(둘 다 지정하면 vip_only가 우선한다). "
    "아무 것도 지정하지 않으면 최근 순으로 최대 200건을 반환한다.",
)
def list_customers(vip_only: bool = False, dormant_only: bool = False, db: Session = Depends(get_db)) -> list:
    repo = CustomerRepository(db)
    if vip_only:
        return repo.list_vip()
    if dormant_only:
        return repo.list_dormant()
    return repo.list_all(limit=200)


@router.get(
    "/{customer_id}",
    response_model=CustomerOut,
    summary="고객 단건 조회",
    responses={404: {"description": "고객을 찾을 수 없습니다."}},
)
def get_customer(customer_id: int, db: Session = Depends(get_db)):
    customer = CustomerRepository(db).get_by_id(customer_id)
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="고객을 찾을 수 없습니다.")
    return customer
