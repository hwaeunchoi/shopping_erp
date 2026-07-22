"""
repositories/user_repository.py
----------------------------------
ERD 2.1 시스템/사용자 그룹(users, roles, permissions)에 대한 Repository.
"""

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.user import Permission, Role, RolePermission, User
from repositories.base_repository import BaseRepository


class UserRepository(BaseRepository[User]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, User)

    def get_by_username(self, username: str) -> Optional[User]:
        stmt = select(User).where(User.username == username)
        return self.session.execute(stmt).scalar_one_or_none()

    def list_active(self) -> list[User]:
        stmt = select(User).where(User.is_active.is_(True))
        return list(self.session.execute(stmt).scalars().all())


class RoleRepository(BaseRepository[Role]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Role)

    def get_by_name(self, name: str) -> Optional[Role]:
        stmt = select(Role).where(Role.name == name)
        return self.session.execute(stmt).scalar_one_or_none()


class PermissionRepository(BaseRepository[Permission]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Permission)

    def list_by_role(self, role_id: int) -> list[Permission]:
        stmt = (
            select(Permission)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(RolePermission.role_id == role_id)
        )
        return list(self.session.execute(stmt).scalars().all())

    def get_by_code(self, code: str) -> Optional[Permission]:
        stmt = select(Permission).where(Permission.code == code)
        return self.session.execute(stmt).scalar_one_or_none()


class RolePermissionRepository(BaseRepository[RolePermission]):
    """설정 화면의 역할별 권한 매핑 관리(SRS FR-USER-02/03)."""

    def __init__(self, session: Session) -> None:
        super().__init__(session, RolePermission)

    def replace_for_role(self, role_id: int, permission_ids: list[int]) -> None:
        """role_id의 기존 매핑을 전부 지우고 permission_ids로 새로 채운다."""
        stmt = select(RolePermission).where(RolePermission.role_id == role_id)
        for existing in self.session.execute(stmt).scalars().all():
            self.session.delete(existing)
        self.session.flush()
        for permission_id in permission_ids:
            self.session.add(RolePermission(role_id=role_id, permission_id=permission_id))
        self.session.flush()
