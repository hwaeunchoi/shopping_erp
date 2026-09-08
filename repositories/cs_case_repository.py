"""
repositories/cs_case_repository.py
--------------------------------------
상용 ERP 확장(5단계, B묶음) - CS 케이스/이력 Repository.
"""

from datetime import datetime, timezone
from typing import Any, Optional, cast

from sqlalchemy import CursorResult, func, or_, select, update
from sqlalchemy.orm import Session

from models.cs_case import CsCase, CsCaseHistory
from repositories.base_repository import BaseRepository


class CsCaseRepository(BaseRepository[CsCase]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, CsCase)

    def get_by_external(self, platform_id: int, external_inquiry_id: str) -> Optional[CsCase]:
        stmt = select(CsCase).where(
            CsCase.platform_id == platform_id, CsCase.external_inquiry_id == external_inquiry_id
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def list_by_ids(self, ids: list[int]) -> list[CsCase]:
        if not ids:
            return []
        stmt = select(CsCase).where(CsCase.id.in_(ids))
        return list(self.session.execute(stmt).scalars().all())

    def list_filtered(
        self,
        *,
        status: Optional[str] = None,
        platform_id: Optional[int] = None,
        inquiry_type: Optional[str] = None,
        priority: Optional[str] = None,
        assignee_id: Optional[int] = None,
        unassigned_only: bool = False,
        overdue_only: bool = False,
        search: Optional[str] = None,
        now: Optional[datetime] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CsCase]:
        stmt = select(CsCase)
        if status:
            stmt = stmt.where(CsCase.status == status)
        if platform_id is not None:
            stmt = stmt.where(CsCase.platform_id == platform_id)
        if inquiry_type:
            stmt = stmt.where(CsCase.inquiry_type == inquiry_type)
        if priority:
            stmt = stmt.where(CsCase.priority == priority)
        if unassigned_only:
            stmt = stmt.where(CsCase.assignee_id.is_(None))
        elif assignee_id is not None:
            stmt = stmt.where(CsCase.assignee_id == assignee_id)
        if overdue_only:
            reference = now or datetime.now(timezone.utc).replace(tzinfo=None)
            stmt = stmt.where(
                CsCase.due_at.is_not(None), CsCase.due_at < reference, CsCase.status.notin_(("RESOLVED", "CLOSED"))
            )
        if search:
            like = f"%{search}%"
            stmt = stmt.where(
                or_(CsCase.subject.ilike(like), CsCase.external_inquiry_id.like(like), CsCase.tags.like(like))
            )
        stmt = stmt.order_by(CsCase.id.desc()).limit(limit).offset(offset)
        return list(self.session.execute(stmt).scalars().all())

    def count_by_status(self) -> dict[str, int]:
        stmt = select(CsCase.status, func.count()).group_by(CsCase.status)
        return {row[0]: row[1] for row in self.session.execute(stmt).all()}

    def count_unassigned(self) -> int:
        stmt = (
            select(func.count())
            .select_from(CsCase)
            .where(CsCase.assignee_id.is_(None), CsCase.status.notin_(("RESOLVED", "CLOSED")))
        )
        return self.session.execute(stmt).scalar_one()

    def count_overdue(self, now: Optional[datetime] = None) -> int:
        reference = now or datetime.now(timezone.utc).replace(tzinfo=None)
        stmt = (
            select(func.count())
            .select_from(CsCase)
            .where(CsCase.due_at.is_not(None), CsCase.due_at < reference, CsCase.status.notin_(("RESOLVED", "CLOSED")))
        )
        return self.session.execute(stmt).scalar_one()

    def find_recent_duplicate(self, *, order_id: int, inquiry_type: str, since: datetime) -> Optional[CsCase]:
        """중복 문의 감지 - 같은 주문에 대해 같은 문의유형으로 아직 종결되지 않은
        케이스가 최근에 이미 있으면 그 케이스를 반환한다(수기 생성 시 화면에서
        경고용으로 쓴다 - 자동으로 병합하지는 않는다)."""
        stmt = (
            select(CsCase)
            .where(
                CsCase.order_id == order_id,
                CsCase.inquiry_type == inquiry_type,
                CsCase.status.notin_(("RESOLVED", "CLOSED")),
                CsCase.created_at >= since,
            )
            .order_by(CsCase.id.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def claim_transition(self, case_id: int, expected_status: str, new_status: str, **extra_values: Any) -> bool:
        """expected_status일 때만 new_status로 원자적으로 전이한다(+ 부가 컬럼 갱신) -
        services.fulfillment_repository.FulfillmentBatchItemRepository.claim_transition과
        동일 원리(UPDATE ... WHERE id=? AND status=?가 DB 레벨에서 원자적이므로
        동시 배정/동시 상태변경 중 정확히 하나만 성공한다)."""
        stmt = (
            update(CsCase)
            .where(CsCase.id == case_id, CsCase.status == expected_status)
            .values(status=new_status, **extra_values)
        )
        result = cast(CursorResult, self.session.execute(stmt))
        return result.rowcount == 1

    def claim_assignee(self, case_id: int, expected_assignee_id: Optional[int], new_assignee_id: Optional[int]) -> bool:
        """expected_assignee_id(화면이 마지막으로 본 담당자 - 미배정이면 None)일 때만
        new_assignee_id로 원자적으로 바꾼다. 담당자 배정은 status를 바꾸지 않으므로
        claim_transition/claim_field_update의 status 기준 가드로는 "두 사람이 동시에
        같은 미배정 건에 서로 다른 담당자를 배정" 경합을 못 잡는다(둘 다 같은
        status를 보고 있었을 뿐 assignee_id 자체는 신경쓰지 않기 때문) - 그래서
        담당자 배정만 별도로 assignee_id 자체를 낙관적 동시성 기준으로 삼는다."""
        stmt = (
            update(CsCase)
            .where(CsCase.id == case_id, CsCase.assignee_id == expected_assignee_id)
            .values(assignee_id=new_assignee_id)
        )
        result = cast(CursorResult, self.session.execute(stmt))
        return result.rowcount == 1

    def claim_field_update(self, case_id: int, expected_status: str, **extra_values: Any) -> bool:
        """상태값 자체는 바꾸지 않고(현재 status와 같은 값을 조건으로만 사용) 담당자
        배정/답변초안 저장처럼 상태를 바꾸지 않는 변경에도 stale 화면 덮어쓰기 방지를
        적용한다."""
        stmt = update(CsCase).where(CsCase.id == case_id, CsCase.status == expected_status).values(**extra_values)
        result = cast(CursorResult, self.session.execute(stmt))
        return result.rowcount == 1


class CsCaseHistoryRepository(BaseRepository[CsCaseHistory]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, CsCaseHistory)

    def list_by_case(self, case_id: int) -> list[CsCaseHistory]:
        stmt = select(CsCaseHistory).where(CsCaseHistory.case_id == case_id).order_by(CsCaseHistory.id)
        return list(self.session.execute(stmt).scalars().all())
