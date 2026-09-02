"""
repositories/settlement_repository.py
-----------------------------------------
ERD 2.6 정산 그룹(settlements, settlement_details)과 상용 ERP 확장(2단계)
정산 대사(settlement_discrepancies)에 대한 Repository.
"""

from datetime import date
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from models.settlement import Settlement, SettlementDetail, SettlementDiscrepancy
from repositories.base_repository import BaseRepository


class SettlementRepository(BaseRepository[Settlement]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Settlement)

    def list_by_platform(self, platform_id: int) -> list[Settlement]:
        stmt = select(Settlement).where(Settlement.platform_id == platform_id)
        return list(self.session.execute(stmt).scalars().all())

    def get_by_platform_and_cycle(
        self, platform_id: int, settlement_cycle: str, settlement_type: Optional[str] = None
    ) -> Optional[Settlement]:
        """정산확인 배치의 upsert용 조회. settlement_type을 함께 주면(예: 쿠팡
        MONTHLY/WEEKLY/ADDITIONAL/RESERVE) 같은 회차 문자열이라도 종류가 다른
        정산 건은 별개로 취급한다(models.settlement 모듈 docstring 참고)."""
        stmt = select(Settlement).where(
            Settlement.platform_id == platform_id, Settlement.settlement_cycle == settlement_cycle
        )
        if settlement_type is not None:
            stmt = stmt.where(Settlement.settlement_type == settlement_type)
        return self.session.execute(stmt).scalar_one_or_none()

    def get_by_platform_and_date(self, platform_id: int, settlement_date: date) -> Optional[Settlement]:
        """정산 상세(주문 단위) 라인을 그 라인의 settlementDate로 정산 회차에 매칭할 때 쓴다
        (services.settlement_sync_service 참고) - 못 찾으면 호출부가 SettlementDiscrepancy로
        남긴다. settled_date(확정) 또는 scheduled_date(아직 SCHEDULED인 회차)를 모두
        확인한다 - 상세 라인이 회차 확정 전에 먼저 도착할 수 있기 때문이다."""
        stmt = select(Settlement).where(
            Settlement.platform_id == platform_id,
            or_(Settlement.settled_date == settlement_date, Settlement.scheduled_date == settlement_date),
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def list_by_status(self, status: str) -> list[Settlement]:
        stmt = select(Settlement).where(Settlement.status == status)
        return list(self.session.execute(stmt).scalars().all())

    def list_details(self, settlement_id: int) -> list[SettlementDetail]:
        stmt = select(SettlementDetail).where(SettlementDetail.settlement_id == settlement_id)
        return list(self.session.execute(stmt).scalars().all())

    def get_detail_by_natural_key(
        self,
        settlement_id: int,
        order_id: int,
        order_item_id: Optional[int],
        sale_type: Optional[str],
        recognition_date: Optional[date],
    ) -> Optional[SettlementDetail]:
        """정산 상세 라인은 채널이 별도 라인 ID를 주지 않아(예: 쿠팡 revenue-history)
        (settlement_id, order_id, order_item_id, sale_type, recognition_date) 조합을
        자연키로 삼아 재수집 시 중복 생성을 막는다."""
        stmt = select(SettlementDetail).where(
            SettlementDetail.settlement_id == settlement_id,
            SettlementDetail.order_id == order_id,
            SettlementDetail.order_item_id == order_item_id,
            SettlementDetail.sale_type == sale_type,
            SettlementDetail.recognition_date == recognition_date,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def add_detail(self, detail: SettlementDetail) -> SettlementDetail:
        self.session.add(detail)
        self.session.flush()
        return detail


class SettlementDiscrepancyRepository(BaseRepository[SettlementDiscrepancy]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, SettlementDiscrepancy)

    def get_unresolved_for(
        self, platform_id: int, reason: str, order_id: Optional[int] = None, settlement_id: Optional[int] = None
    ) -> Optional[SettlementDiscrepancy]:
        """같은 미해소 차액/불일치를 재수집 때마다 중복 생성하지 않기 위한 조회."""
        stmt = select(SettlementDiscrepancy).where(
            SettlementDiscrepancy.platform_id == platform_id,
            SettlementDiscrepancy.reason == reason,
            SettlementDiscrepancy.resolved_at.is_(None),
        )
        if order_id is not None:
            stmt = stmt.where(SettlementDiscrepancy.order_id == order_id)
        if settlement_id is not None:
            stmt = stmt.where(SettlementDiscrepancy.settlement_id == settlement_id)
        return self.session.execute(stmt).scalar_one_or_none()

    def list_unresolved(self, platform_id: Optional[int] = None) -> list[SettlementDiscrepancy]:
        stmt = select(SettlementDiscrepancy).where(SettlementDiscrepancy.resolved_at.is_(None))
        if platform_id is not None:
            stmt = stmt.where(SettlementDiscrepancy.platform_id == platform_id)
        return list(self.session.execute(stmt.order_by(SettlementDiscrepancy.detected_at.desc())).scalars())
