"""
repositories/cost_repository.py
----------------------------------
ERD 2.7 비용 그룹(costs)에 대한 Repository.
"""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.cost import Cost
from repositories.base_repository import BaseRepository


class CostRepository(BaseRepository[Cost]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Cost)

    def list_by_date_range(self, start: date, end: date) -> list[Cost]:
        stmt = select(Cost).where(Cost.incurred_date >= start, Cost.incurred_date < end)
        return list(self.session.execute(stmt).scalars().all())

    def list_by_category(self, category: str) -> list[Cost]:
        stmt = select(Cost).where(Cost.category == category)
        return list(self.session.execute(stmt).scalars().all())
