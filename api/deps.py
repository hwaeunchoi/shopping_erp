"""
api/deps.py
-------------
FastAPI 공용 의존성: DB 세션 주입, JWT 인증, 권한(RBAC) 검사.

DB 세션은 core.database.get_db를 그대로 재노출한다 - API 계층은 세션을
직접 만들지 않는다는 core/database.py의 설계 원칙을 따른다. get_db()는
자동 커밋을 하지 않으므로, 쓰기 작업을 수행하는 라우터 핸들러는 각자
db.commit()을 명시적으로 호출해야 한다.
"""

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from core.database import get_db  # noqa: F401  (라우터에서 재사용)
from core.security import decode_access_token
from models.user import User
from repositories.user_repository import PermissionRepository, UserRepository

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    payload = decode_access_token(token)
    username = payload.get("sub") if payload else None
    if username is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="유효하지 않은 인증 토큰입니다.")

    user = UserRepository(db).get_by_username(username)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="사용자를 찾을 수 없거나 비활성 상태입니다."
        )
    return user


def require_permission(code: str):
    """역할(Role)에 code 권한이 매핑돼 있는지 검사하는 의존성을 반환한다.

    사용 예: @router.get("/orders", dependencies=[Depends(require_permission("ORDER_VIEW"))])
    """

    def checker(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> User:
        permissions = PermissionRepository(db).list_by_role(current_user.role_id)
        if code not in {p.code for p in permissions}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"권한이 없습니다: {code}")
        return current_user

    return checker
