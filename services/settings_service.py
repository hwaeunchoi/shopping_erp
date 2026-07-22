"""
services/settings_service.py
---------------------------------
설정(Settings) 화면 Service 계층: 사용자/역할/권한/API Credential/시스템설정.
SRS FR-USER-02/03(권한체계, 메뉴별 접근권한 매핑)과 API Credential 암호화
저장 대응.

API Credential은 key_value_encrypted 컬럼에 core.crypto.encrypt_value()로
암호화한 값만 저장하며, 조회 시에도 평문을 그대로 반환하지 않고 마지막
4자리만 노출하는 마스킹된 값만 돌려준다(평문 저장/노출 금지).
"""

from datetime import datetime, timezone
from typing import Any, Optional

from core.crypto import decrypt_value, encrypt_value
from core.security import hash_password
from models.system import ApiCredential, SystemSetting
from models.user import Permission, Role, User
from repositories.system_repository import ApiCredentialRepository, SystemSettingRepository
from repositories.user_repository import PermissionRepository, RolePermissionRepository, RoleRepository, UserRepository


class UserManagementService:
    def __init__(self, session) -> None:
        self.session = session
        self.user_repo = UserRepository(session)

    def list_users(self) -> list[User]:
        return self.user_repo.list_all()

    def create_user(self, username: str, password: str, name: str, role_id: int, email: Optional[str] = None) -> User:
        user = User(
            username=username,
            password_hash=hash_password(password),
            name=name,
            email=email,
            role_id=role_id,
            is_active=True,
        )
        return self.user_repo.add(user)

    def update_user(
        self,
        user_id: int,
        *,
        name: Optional[str] = None,
        email: Optional[str] = None,
        role_id: Optional[int] = None,
        is_active: Optional[bool] = None,
        password: Optional[str] = None,
    ) -> Optional[User]:
        user = self.user_repo.get_by_id(user_id)
        if user is None:
            return None
        if name is not None:
            user.name = name
        if email is not None:
            user.email = email
        if role_id is not None:
            user.role_id = role_id
        if is_active is not None:
            user.is_active = is_active
        if password is not None:
            user.password_hash = hash_password(password)
        self.session.flush()
        return user


class RoleService:
    def __init__(self, session) -> None:
        self.session = session
        self.role_repo = RoleRepository(session)
        self.permission_repo = PermissionRepository(session)
        self.role_permission_repo = RolePermissionRepository(session)

    def list_roles(self) -> list[Role]:
        return self.role_repo.list_all()

    def create_role(self, name: str, description: Optional[str] = None) -> Role:
        return self.role_repo.add(Role(name=name, description=description))

    def update_role(
        self, role_id: int, *, name: Optional[str] = None, description: Optional[str] = None
    ) -> Optional[Role]:
        role = self.role_repo.get_by_id(role_id)
        if role is None:
            return None
        if name is not None:
            role.name = name
        if description is not None:
            role.description = description
        self.session.flush()
        return role

    def list_permissions(self) -> list[Permission]:
        return self.permission_repo.list_all()

    def get_role_permission_codes(self, role_id: int) -> list[str]:
        return [p.code for p in self.permission_repo.list_by_role(role_id)]

    def set_role_permissions(self, role_id: int, codes: list[str]) -> list[str]:
        """codes 중 존재하지 않는 권한 코드는 조용히 무시한다(존재하는 코드만 매핑)."""
        ids = [perm.id for code in codes if (perm := self.permission_repo.get_by_code(code)) is not None]
        self.role_permission_repo.replace_for_role(role_id, ids)
        return self.get_role_permission_codes(role_id)


def _mask(plain: str) -> str:
    """마지막 4자리만 노출하고 나머지는 마스킹한다(평문 노출 금지)."""
    if len(plain) <= 4:
        return "*" * len(plain)
    return "*" * (len(plain) - 4) + plain[-4:]


class ApiCredentialService:
    """API Credential 암호화 저장/조회.

    복호화된 평문은 절대 API 응답으로 반환하지 않는다 - list_masked()는
    마지막 4자리만 보여주는 마스킹된 값만 돌려준다. get_decrypted()는 API
    레이어(라우터)에서는 절대 호출하지 않고, 서버 내부에서 외부 플랫폼 API를
    실제로 호출하는 커넥터(integrations/malls, integrations/ads)에서만 사용한다.
    """

    def __init__(self, session) -> None:
        self.session = session
        self.repo = ApiCredentialRepository(session)

    def get_decrypted(self, owner_type: str, owner_id: int, key_name: str) -> Optional[str]:
        credential = self.repo.get_by_owner_and_key(owner_type, owner_id, key_name)
        if credential is None:
            return None
        try:
            return decrypt_value(credential.key_value_encrypted)
        except Exception:
            return None

    def list_masked(self, owner_type: Optional[str] = None, owner_id: Optional[int] = None) -> list[dict[str, Any]]:
        items = self.repo.list_by_owner(owner_type, owner_id)
        result: list[dict[str, Any]] = []
        for c in items:
            try:
                masked = _mask(decrypt_value(c.key_value_encrypted))
            except Exception:
                masked = "(복호화 실패)"
            result.append(
                {
                    "id": c.id,
                    "owner_type": c.owner_type,
                    "owner_id": c.owner_id,
                    "key_name": c.key_name,
                    "masked_value": masked,
                    "expires_at": c.expires_at,
                    "updated_at": c.updated_at,
                }
            )
        return result

    def upsert_credential(
        self, owner_type: str, owner_id: int, key_name: str, plain_value: str, expires_at: Optional[datetime] = None
    ) -> ApiCredential:
        encrypted = encrypt_value(plain_value)
        existing = self.repo.get_by_owner_and_key(owner_type, owner_id, key_name)
        now = datetime.now(timezone.utc)
        if existing is not None:
            existing.key_value_encrypted = encrypted
            existing.expires_at = expires_at
            existing.updated_at = now
            self.session.flush()
            return existing
        credential = ApiCredential(
            owner_type=owner_type,
            owner_id=owner_id,
            key_name=key_name,
            key_value_encrypted=encrypted,
            expires_at=expires_at,
            updated_at=now,
        )
        return self.repo.add(credential)

    def delete_credential(self, credential_id: int) -> bool:
        credential = self.repo.get_by_id(credential_id)
        if credential is None:
            return False
        self.repo.delete(credential)
        return True


class SystemSettingService:
    def __init__(self, session) -> None:
        self.session = session
        self.repo = SystemSettingRepository(session)

    def list_settings(self, category: Optional[str] = None) -> list[SystemSetting]:
        return self.repo.list_by_category(category)

    def upsert_setting(self, category: str, key: str, value: Optional[str]) -> SystemSetting:
        return self.repo.upsert(category, key, value)
