"""
api/routers/recent_views.py
--------------------------------
UI v1.1 6장 최근 본 항목. 주문/상품/고객 상세를 열람할 때마다 프론트엔드가
이 엔드포인트를 호출해 기록한다(동일 대상 재열람 시 viewed_at만 갱신).
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.deps import get_current_user, get_db
from models.user import User
from repositories.extra_repository import RecentViewRepository

router = APIRouter(prefix="/api/recent-views", tags=["recent-views"], dependencies=[Depends(get_current_user)])


class RecentViewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    target_type: str
    target_id: int


class RecentViewRecordIn(BaseModel):
    target_type: str
    target_id: int


@router.get(
    "",
    response_model=list[RecentViewOut],
    summary="최근 본 항목 조회",
    description="현재 사용자가 최근 조회한 주문/상품/고객을 viewed_at 최신순으로 최대 10건 반환한다.",
)
def list_recent_views(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list:
    return RecentViewRepository(db).list_recent(current_user.id, limit=10)


@router.post(
    "",
    response_model=RecentViewOut,
    summary="최근 본 항목 기록",
    description="주문/상품/고객 상세 화면 진입 시 호출한다. 동일 대상을 다시 열람하면 새 행을 만들지 않고 "
    "viewed_at만 갱신한다.",
)
def record_recent_view(
    payload: RecentViewRecordIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    view = RecentViewRepository(db).touch(current_user.id, payload.target_type, payload.target_id)
    db.commit()
    return view
