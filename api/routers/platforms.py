"""
api/routers/platforms.py
----------------------------
쇼핑몰 플랫폼 기준정보 조회. 특정 권한 없이 로그인한 사용자면 조회 가능하다
(다른 화면에서 플랫폼 선택 드롭다운 등으로 공통 참조하는 기준정보이기 때문).
"""

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.deps import get_current_user, get_db
from repositories.platform_repository import PlatformRepository

router = APIRouter(prefix="/api/platforms", tags=["platforms"], dependencies=[Depends(get_current_user)])


class PlatformOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    connector_class: str
    settlement_cycle_days: Optional[int]
    is_active: bool


@router.get(
    "",
    response_model=list[PlatformOut],
    summary="쇼핑몰 플랫폼 목록 조회",
    description="활성 상태인 쇼핑몰 플랫폼(네이버 스마트스토어, 쿠팡 등) 기준정보를 조회한다.",
)
def list_platforms(db: Session = Depends(get_db)) -> list:
    return PlatformRepository(db).list_active()
