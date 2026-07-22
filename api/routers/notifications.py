"""
api/routers/notifications.py
---------------------------------
UI v1.0 알림센터. 알림 벨(🔔)의 배지 숫자는 notifications.is_read=0 기준.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.deps import get_db, require_permission
from repositories.system_repository import NotificationRepository
from services.notification_service import NotificationService

router = APIRouter(
    prefix="/api/notifications", tags=["notifications"], dependencies=[Depends(require_permission("NOTIFICATION_VIEW"))]
)


class NotificationOut(BaseModel):
    id: int
    rule_id: Optional[int]
    type: str
    severity: str
    message: str
    is_read: bool
    created_at: datetime


class UnreadCountOut(BaseModel):
    unread_count: int


class MarkAllReadOut(BaseModel):
    marked_count: int


@router.get(
    "",
    response_model=list[NotificationOut],
    summary="알림 목록 조회",
    description="unread_only=true면 읽지 않은 알림만, 아니면 최신순 최대 50건을 반환한다.",
)
def list_notifications(unread_only: bool = False, db: Session = Depends(get_db)) -> list:
    return NotificationService(db).list_recent(unread_only=unread_only)


@router.get(
    "/unread-count",
    response_model=UnreadCountOut,
    summary="읽지 않은 알림 수 조회",
    description="상단바 알림 벨(🔔)의 배지 숫자로 사용한다.",
)
def get_unread_count(db: Session = Depends(get_db)) -> UnreadCountOut:
    return UnreadCountOut(unread_count=NotificationService(db).unread_count())


@router.patch(
    "/{notification_id}/read",
    response_model=NotificationOut,
    summary="알림 읽음 처리",
    responses={404: {"description": "알림을 찾을 수 없습니다."}},
)
def mark_notification_read(notification_id: int, db: Session = Depends(get_db)):
    notification = NotificationRepository(db).get_by_id(notification_id)
    if notification is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="알림을 찾을 수 없습니다.")
    NotificationService(db).mark_read(notification)
    db.commit()
    return notification


@router.post("/read-all", response_model=MarkAllReadOut, summary="전체 알림 읽음 처리")
def mark_all_notifications_read(db: Session = Depends(get_db)) -> MarkAllReadOut:
    count = NotificationService(db).mark_all_read()
    db.commit()
    return MarkAllReadOut(marked_count=count)
