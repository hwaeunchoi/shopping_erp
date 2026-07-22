"""
repositories/settlement_repository.py
-----------------------------------------
ERD 2.6 정산 그룹(settlements, settlement_details)에 대한 Repository.
"""

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.settlement import Settlement, SettlementDetail
from repositories.base_repository import BaseRepository


class SettlementRepository(BaseRepository[Settlement]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Settlement)

    def list_by_platform(self, platform_id: int) -> list[Settlement]:
        stmt = select(Settlement).where(Settlement.platform_id == platform_id)
        return list(self.session.execute(stmt).scalars().all())

    def get_by_platform_and_cycle(self, platform_id: int, settlement_cycle: str) -> Optional[Settlement]:
        """정산확인 배치의 upsert용 조회 (platform_id + settlement_cycle 조합)."""
        stmt = select(Settlement).where(
            Settlement.platform_id == platform_id, Settlement.settlement_cycle == settlement_cycle
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def list_by_status(self, status: str) -> list[Settlement]:
        stmt = select(Settlement).where(Settlement.status == status)
        return list(self.session.execute(stmt).scalars().all())

    def list_details(self, settlement_id: int) -> list[SettlementDetail]:
        stmt = select(SettlementDetail).where(SettlementDetail.settlement_id == settlement_id)
        return list(self.session.execute(stmt).scalars().all())
