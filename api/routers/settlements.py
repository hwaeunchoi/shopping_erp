"""
api/routers/settlements.py
------------------------------
정산 조회.
"""

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.deps import get_db, require_permission
from repositories.settlement_repository import SettlementRepository

router = APIRouter(
    prefix="/api/settlements", tags=["settlements"], dependencies=[Depends(require_permission("SETTLEMENT_VIEW"))]
)


class SettlementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    platform_id: int
    settlement_cycle: str
    status: str
    expected_amount: float
    settled_amount: float
    unsettled_amount: float


class SettlementDetailOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    gross_amount: float
    fee_amount: float
    net_amount: float


@router.get(
    "",
    response_model=list[SettlementOut],
    summary="정산 목록 조회",
    description="platform_id를 지정하면 해당 플랫폼의 정산 건만, 지정하지 않으면 최근 순으로 " "최대 200건을 반환한다.",
)
def list_settlements(platform_id: Optional[int] = None, db: Session = Depends(get_db)) -> list:
    repo = SettlementRepository(db)
    return repo.list_by_platform(platform_id) if platform_id is not None else repo.list_all(limit=200)


@router.get(
    "/{settlement_id}/details",
    response_model=list[SettlementDetailOut],
    summary="정산 상세 내역 조회",
    description="정산 건에 포함된 주문별 정산 상세(매출/수수료/실지급액)를 조회한다. "
    "정산 건이 없으면 빈 목록을 반환한다.",
)
def list_settlement_details(settlement_id: int, db: Session = Depends(get_db)) -> list:
    return SettlementRepository(db).list_details(settlement_id)
