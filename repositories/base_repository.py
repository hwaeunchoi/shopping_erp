"""
repositories/base_repository.py
----------------------------------
모든 Repository의 공통 베이스.

core/database.py 설계 원칙에 따라 Repository는 세션을 직접 생성하지 않고
생성자에서 주입받는다. 세션의 생명주기(commit/rollback/close)는 호출하는
Service 계층(또는 API의 core.database.get_db / session_scope)이 책임진다.
"""

from typing import Generic, Optional, TypeVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models.base import Base

ModelType = TypeVar("ModelType", bound=Base)


class BaseRepository(Generic[ModelType]):
    """단일 모델에 대한 공통 CRUD/조회를 제공하는 Repository 베이스 클래스."""

    def __init__(self, session: Session, model: type[ModelType]) -> None:
        self.session = session
        self.model = model

    def get_by_id(self, id_: int) -> Optional[ModelType]:
        return self.session.get(self.model, id_)

    def list_all(self, limit: Optional[int] = None, offset: int = 0) -> list[ModelType]:
        stmt = select(self.model).offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.execute(stmt).scalars().all())

    def add(self, obj: ModelType) -> ModelType:
        self.session.add(obj)
        self.session.flush()
        return obj

    def delete(self, obj: ModelType) -> None:
        self.session.delete(obj)
        self.session.flush()

    def count(self) -> int:
        stmt = select(func.count()).select_from(self.model)
        return self.session.execute(stmt).scalar_one()
