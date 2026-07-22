"""
api/routers/auth.py
-----------------------
로그인/내 정보 조회. SRS FR-USER-01~04 대응.

FR-USER-04(로그인/활동 로그): 로그인 성공/실패를 system_logs에
log_type="USER_ACTIVITY"로 기록한다.

UI 와이어프레임 v1.1 1장(다크모드 토글): users.theme_preference 컬럼은
이미 있었지만 저장 API가 없었다 - PATCH /me/theme로 조회뿐 아니라 저장도
가능하게 한다.
"""

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.deps import get_current_user, get_db
from core.security import create_access_token, verify_password
from models.system import SystemLog
from models.user import User
from repositories.system_repository import SystemLogRepository
from repositories.user_repository import PermissionRepository, UserRepository

router = APIRouter(prefix="/api/auth", tags=["auth"])


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    name: str
    role_id: int
    is_active: bool
    theme_preference: str
    permissions: list[str] = []


def _to_user_profile(user: User, db: Session) -> UserProfile:
    """프론트엔드가 role 기반으로 메뉴/버튼을 숨길 수 있도록 권한 코드 목록을 함께 내려준다."""
    codes = [p.code for p in PermissionRepository(db).list_by_role(user.role_id)]
    return UserProfile(**UserProfile.model_validate(user).model_dump(exclude={"permissions"}), permissions=codes)


@router.post(
    "/login",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="로그인",
    description="아이디/비밀번호로 로그인하여 JWT 액세스 토큰을 발급한다.",
    responses={401: {"description": "아이디 또는 비밀번호가 올바르지 않거나 계정이 비활성 상태입니다."}},
)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)) -> TokenResponse:
    log_repo = SystemLogRepository(db)
    user = UserRepository(db).get_by_username(form_data.username)
    if user is None or not user.is_active or not verify_password(form_data.password, user.password_hash):
        log_repo.add(
            SystemLog(
                log_type="USER_ACTIVITY",
                source="auth.login",
                level="WARN",
                message=f"로그인 실패: username={form_data.username}",
                user_id=user.id if user is not None else None,
                created_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="아이디 또는 비밀번호가 올바르지 않습니다."
        )
    token = create_access_token(subject=user.username, extra_claims={"user_id": user.id, "role_id": user.role_id})
    log_repo.add(
        SystemLog(
            log_type="USER_ACTIVITY",
            source="auth.login",
            level="INFO",
            message=f"로그인 성공: username={user.username}",
            user_id=user.id,
            created_at=datetime.now(timezone.utc),
        )
    )
    db.commit()
    return TokenResponse(access_token=token)


@router.get(
    "/me",
    response_model=UserProfile,
    summary="내 정보 조회",
    description="현재 로그인한 사용자(Authorization 헤더의 토큰 소유자)의 프로필을 반환한다. "
    "permissions는 프론트엔드가 사이드바 메뉴/버튼을 role에 맞게 숨기는 데 사용한다.",
    responses={401: {"description": "인증 토큰이 없거나 유효하지 않습니다."}},
)
def read_me(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> UserProfile:
    return _to_user_profile(current_user, db)


class ThemeUpdate(BaseModel):
    theme_preference: Literal["LIGHT", "DARK"]


@router.patch(
    "/me/theme",
    response_model=UserProfile,
    summary="다크모드 설정 저장",
    description="UI 와이어프레임 v1.1 1장 다크모드 토글의 저장 API. LIGHT/DARK 중 하나로 사용자 설정을 저장한다.",
)
def update_theme(
    payload: ThemeUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> UserProfile:
    current_user.theme_preference = payload.theme_preference
    db.commit()
    return _to_user_profile(current_user, db)
