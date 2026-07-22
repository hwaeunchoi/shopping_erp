"""
repositories/platform_repository.py
--------------------------------------
ERD 2.2 플랫폼 그룹(platforms, platform_fee_rules)에 대한 Repository.
"""

from datetime import date
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from models.platform import Platform, PlatformFeeRule
from repositories.base_repository import BaseRepository


class PlatformRepository(BaseRepository[Platform]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Platform)

    def get_by_code(self, code: str) -> Optional[Platform]:
        stmt = select(Platform).where(Platform.code == code)
        return self.session.execute(stmt).scalar_one_or_none()

    def list_active(self) -> list[Platform]:
        stmt = select(Platform).where(Platform.is_active.is_(True))
        return list(self.session.execute(stmt).scalars().all())


class PlatformFeeRuleRepository(BaseRepository[PlatformFeeRule]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, PlatformFeeRule)

    def get_effective_rule(self, platform_id: int, on_date: date) -> Optional[PlatformFeeRule]:
        """platform_id의 수수료 규칙 중 on_date 시점에 적용 가능한("활성") 규칙을 찾는다.

        "활성"은 별도의 상태 컬럼이 아니라 적용기간(effective_from ~
        effective_to)이 on_date를 포함하는지로 판단한다(effective_to가
        NULL이면 무기한 적용). 적용 가능한 규칙이 여러 개면 effective_from이
        가장 최근인 규칙을 우선한다(최신 우선).
        """
        stmt = (
            select(PlatformFeeRule)
            .where(
                PlatformFeeRule.platform_id == platform_id,
                PlatformFeeRule.effective_from <= on_date,
                or_(PlatformFeeRule.effective_to.is_(None), PlatformFeeRule.effective_to >= on_date),
            )
            .order_by(PlatformFeeRule.effective_from.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalars().first()
