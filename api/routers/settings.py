"""
api/routers/settings.py
----------------------------
설정(Settings) 화면: 사용자 관리/역할 관리/권한 관리/API Credential 관리/
시스템 설정. SRS FR-USER-02/03 및 API Credential 암호화 저장 대응.
전체 화면이 SETTINGS_MANAGE 권한으로 보호된다(scripts/init_db.py의
DEFAULT_PERMISSIONS에 이미 정의된 코드를 재사용, 신규 권한 코드 추가 없음).
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.deps import get_db, require_permission
from models.user import Permission, Role, User
from services.settings_service import ApiCredentialService, RoleService, SystemSettingService, UserManagementService

router = APIRouter(
    prefix="/api/settings", tags=["settings"], dependencies=[Depends(require_permission("SETTINGS_MANAGE"))]
)


# --- 사용자 관리 ---


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    name: str
    email: Optional[str]
    role_id: int
    is_active: bool


class UserCreate(BaseModel):
    username: str
    password: str
    name: str
    role_id: int
    email: Optional[str] = None


class UserUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    role_id: Optional[int] = None
    is_active: Optional[bool] = None
    password: Optional[str] = None


@router.get("/users", response_model=list[UserOut], summary="사용자 목록 조회")
def list_users(db: Session = Depends(get_db)) -> list:
    return UserManagementService(db).list_users()


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED, summary="사용자 등록")
def create_user(payload: UserCreate, db: Session = Depends(get_db)) -> User:
    user = UserManagementService(db).create_user(
        username=payload.username,
        password=payload.password,
        name=payload.name,
        role_id=payload.role_id,
        email=payload.email,
    )
    db.commit()
    return user


@router.patch(
    "/users/{user_id}",
    response_model=UserOut,
    summary="사용자 수정(역할 변경/비활성화/비밀번호 재설정 포함)",
    responses={404: {"description": "사용자를 찾을 수 없습니다."}},
)
def update_user(user_id: int, payload: UserUpdate, db: Session = Depends(get_db)) -> User:
    user = UserManagementService(db).update_user(user_id, **payload.model_dump(exclude_unset=True))
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다.")
    db.commit()
    return user


# --- 역할/권한 관리 ---


class RoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: Optional[str]


class RoleCreate(BaseModel):
    name: str
    description: Optional[str] = None


class RoleUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class PermissionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    menu_group: Optional[str]


class RolePermissionsOut(BaseModel):
    role_id: int
    permission_codes: list[str]


class RolePermissionsUpdate(BaseModel):
    permission_codes: list[str]


@router.get("/roles", response_model=list[RoleOut], summary="역할 목록 조회")
def list_roles(db: Session = Depends(get_db)) -> list:
    return RoleService(db).list_roles()


@router.post("/roles", response_model=RoleOut, status_code=status.HTTP_201_CREATED, summary="역할 등록")
def create_role(payload: RoleCreate, db: Session = Depends(get_db)) -> Role:
    role = RoleService(db).create_role(payload.name, payload.description)
    db.commit()
    return role


@router.patch(
    "/roles/{role_id}",
    response_model=RoleOut,
    summary="역할 수정",
    responses={404: {"description": "역할을 찾을 수 없습니다."}},
)
def update_role(role_id: int, payload: RoleUpdate, db: Session = Depends(get_db)) -> Role:
    role = RoleService(db).update_role(role_id, name=payload.name, description=payload.description)
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="역할을 찾을 수 없습니다.")
    db.commit()
    return role


@router.get("/permissions", response_model=list[PermissionOut], summary="전체 권한 코드 목록 조회")
def list_permissions(db: Session = Depends(get_db)) -> list[Permission]:
    return RoleService(db).list_permissions()


@router.get("/roles/{role_id}/permissions", response_model=RolePermissionsOut, summary="역할에 매핑된 권한 코드 조회")
def get_role_permissions(role_id: int, db: Session = Depends(get_db)) -> RolePermissionsOut:
    codes = RoleService(db).get_role_permission_codes(role_id)
    return RolePermissionsOut(role_id=role_id, permission_codes=codes)


@router.patch(
    "/roles/{role_id}/permissions",
    response_model=RolePermissionsOut,
    summary="역할의 권한 매핑 전체 교체",
    description="메뉴별 접근권한을 세분화해 지정한다(SRS FR-USER-03). 넘어온 permission_codes 목록으로 기존 매핑을 완전히 대체한다.",
)
def set_role_permissions(
    role_id: int, payload: RolePermissionsUpdate, db: Session = Depends(get_db)
) -> RolePermissionsOut:
    codes = RoleService(db).set_role_permissions(role_id, payload.permission_codes)
    db.commit()
    return RolePermissionsOut(role_id=role_id, permission_codes=codes)


# --- API Credential 관리 ---


class ApiCredentialOut(BaseModel):
    id: int
    owner_type: str
    owner_id: int
    key_name: str
    masked_value: str
    expires_at: Optional[datetime]
    updated_at: datetime


class ApiCredentialUpsert(BaseModel):
    owner_type: str
    owner_id: int
    key_name: str
    plain_value: str
    expires_at: Optional[datetime] = None


@router.get(
    "/api-credentials",
    response_model=list[ApiCredentialOut],
    summary="API Credential 목록 조회(마스킹된 값만 노출)",
    description="key_value_encrypted는 절대 평문으로 반환하지 않는다 - 마지막 4자리만 노출한다.",
)
def list_api_credentials(
    owner_type: Optional[str] = None, owner_id: Optional[int] = None, db: Session = Depends(get_db)
) -> list[dict]:
    return ApiCredentialService(db).list_masked(owner_type=owner_type, owner_id=owner_id)


@router.post(
    "/api-credentials",
    response_model=ApiCredentialOut,
    status_code=status.HTTP_200_OK,
    summary="API Credential 등록/갱신(Fernet 암호화 저장)",
)
def upsert_api_credential(payload: ApiCredentialUpsert, db: Session = Depends(get_db)) -> dict:
    service = ApiCredentialService(db)
    service.upsert_credential(
        owner_type=payload.owner_type,
        owner_id=payload.owner_id,
        key_name=payload.key_name,
        plain_value=payload.plain_value,
        expires_at=payload.expires_at,
    )
    db.commit()
    matches = [
        c
        for c in service.list_masked(owner_type=payload.owner_type, owner_id=payload.owner_id)
        if c["key_name"] == payload.key_name
    ]
    return matches[0]


@router.delete(
    "/api-credentials/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="API Credential 삭제",
    responses={404: {"description": "API Credential을 찾을 수 없습니다."}},
)
def delete_api_credential(credential_id: int, db: Session = Depends(get_db)) -> None:
    if not ApiCredentialService(db).delete_credential(credential_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API Credential을 찾을 수 없습니다.")
    db.commit()


# --- 시스템 설정 ---


class SystemSettingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category: str
    key: str
    value: Optional[str]
    updated_at: datetime


class SystemSettingUpsert(BaseModel):
    category: str
    key: str
    value: Optional[str] = None


@router.get("/system", response_model=list[SystemSettingOut], summary="시스템 설정 목록 조회")
def list_system_settings(category: Optional[str] = None, db: Session = Depends(get_db)) -> list:
    return SystemSettingService(db).list_settings(category)


@router.patch("/system", response_model=SystemSettingOut, summary="시스템 설정 등록/수정")
def upsert_system_setting(payload: SystemSettingUpsert, db: Session = Depends(get_db)):
    setting = SystemSettingService(db).upsert_setting(payload.category, payload.key, payload.value)
    db.commit()
    return setting
