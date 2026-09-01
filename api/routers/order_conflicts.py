"""
api/routers/order_conflicts.py
------------------------------------
내부 주문상태와 채널 주문상태가 허용된 전이로 설명되지 않을 때(OrderStatusConflict)
운영자가 확인하고 해소하는 화면용 API. 상용 ERP 확장(1단계) 참고.
"""

from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.deps import get_current_user, get_db, require_permission
from models.user import User
from repositories.integration_sync_repository import OrderStatusConflictRepository
from services.order_channel_sync_service import OrderChannelSyncService

router = APIRouter(
    prefix="/api/order-conflicts", tags=["order-conflicts"], dependencies=[Depends(require_permission("ORDER_EDIT"))]
)


class OrderStatusConflictOut(BaseModel):
    id: int
    order_id: int
    internal_status: str
    channel_status: str
    detected_at: datetime
    resolved_at: Optional[datetime]
    resolution: Optional[str]


class ResolveConflictRequest(BaseModel):
    resolution: Literal["ACCEPT_CHANNEL", "KEEP_INTERNAL"]


@router.get("", response_model=list[OrderStatusConflictOut], summary="미해소 주문상태 충돌 목록")
def list_conflicts(order_id: Optional[int] = None, db: Session = Depends(get_db)) -> list[OrderStatusConflictOut]:
    items = OrderStatusConflictRepository(db).list_unresolved(order_id=order_id)
    return [OrderStatusConflictOut.model_validate(i, from_attributes=True) for i in items]


@router.post(
    "/{conflict_id}/resolve",
    response_model=OrderStatusConflictOut,
    summary="충돌 해소",
    description="ACCEPT_CHANNEL은 채널 상태를 내부에 강제 반영(상태머신 우회, 감사로그에 명시), "
    "KEEP_INTERNAL은 내부 상태를 유지하고 채널 값을 폐기한다.",
    responses={404: {"description": "충돌 기록을 찾을 수 없습니다."}, 400: {"description": "이미 해소된 충돌입니다."}},
)
def resolve_conflict(
    conflict_id: int,
    payload: ResolveConflictRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> OrderStatusConflictOut:
    try:
        conflict = OrderChannelSyncService(db).resolve_conflict(conflict_id, payload.resolution, current_user.id)
    except ValueError as e:
        db.rollback()
        code = status.HTTP_404_NOT_FOUND if "찾을 수 없습니다" in str(e) else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=code, detail=str(e)) from e
    db.commit()
    return OrderStatusConflictOut.model_validate(conflict, from_attributes=True)
