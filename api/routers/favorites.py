"""
api/routers/favorites.py
-----------------------------
UI v1.1 5장 즐겨찾기. 상품/보고서 등 target_type+target_id 조합을
즐겨찾기하며, 사용자 본인 소유 항목만 조회/토글할 수 있다.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.deps import get_current_user, get_db
from models.user import User
from repositories.extra_repository import FavoriteRepository

router = APIRouter(prefix="/api/favorites", tags=["favorites"], dependencies=[Depends(get_current_user)])


class FavoriteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    target_type: str
    target_id: int
    created_at: datetime


class FavoriteToggleIn(BaseModel):
    target_type: str
    target_id: int


class FavoriteToggleOut(BaseModel):
    is_favorited: bool


@router.get(
    "",
    response_model=list[FavoriteOut],
    summary="내 즐겨찾기 목록 조회",
    description="target_type을 지정하면 해당 유형(PRODUCT/REPORT 등)만, 지정하지 않으면 전체를 반환한다.",
)
def list_favorites(
    target_type: Optional[str] = None, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list:
    return FavoriteRepository(db).list_by_user(current_user.id, target_type=target_type)


@router.post(
    "/toggle",
    response_model=FavoriteToggleOut,
    summary="즐겨찾기 토글",
    description="이미 즐겨찾기한 대상이면 해제하고, 아니면 추가한다. 응답의 is_favorited로 토글 후 상태를 알 수 있다.",
)
def toggle_favorite(
    payload: FavoriteToggleIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> FavoriteToggleOut:
    is_favorited = FavoriteRepository(db).toggle(current_user.id, payload.target_type, payload.target_id)
    db.commit()
    return FavoriteToggleOut(is_favorited=is_favorited)
