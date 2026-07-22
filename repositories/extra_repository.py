"""
repositories/extra_repository.py
------------------------------------
ERD 부가기능 그룹(models/extra.py) 중 report_schedules,
task_execution_history, recent_views, favorites, integration_status, memos에
대한 Repository. SRS 3.7 보고서 기능(종합 보고서/예약 발송), UI v1.1
addendum(최근조회/즐겨찾기/시스템 모니터링), 주문 메모 기능에서 사용한다.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.extra import Favorite, IntegrationStatus, Memo, RecentView, ReportSchedule, TaskExecutionHistory
from repositories.base_repository import BaseRepository


class ReportScheduleRepository(BaseRepository[ReportSchedule]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, ReportSchedule)

    def list_enabled(self) -> list[ReportSchedule]:
        stmt = select(ReportSchedule).where(ReportSchedule.is_enabled.is_(True))
        return list(self.session.execute(stmt).scalars().all())

    def list_due(self, now: datetime) -> list[ReportSchedule]:
        """활성화되어 있고 next_run_at이 now 이전(도래)인 예약 보고서 목록. 스케줄러가 사용한다."""
        stmt = select(ReportSchedule).where(ReportSchedule.is_enabled.is_(True), ReportSchedule.next_run_at <= now)
        return list(self.session.execute(stmt).scalars().all())


class TaskExecutionHistoryRepository(BaseRepository[TaskExecutionHistory]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, TaskExecutionHistory)

    def start(
        self, task_type: str, trigger_type: str, target: Optional[str] = None, triggered_by: Optional[int] = None
    ) -> TaskExecutionHistory:
        """작업 시작 시 status="RUNNING"인 이력 행을 만든다."""
        history = TaskExecutionHistory(
            task_type=task_type,
            target=target,
            trigger_type=trigger_type,
            triggered_by=triggered_by,
            status="RUNNING",
            started_at=datetime.now(timezone.utc),
        )
        return self.add(history)

    def finish(
        self,
        history: TaskExecutionHistory,
        status: str,
        result_summary: Optional[str] = None,
        error_message: Optional[str] = None,
        processed_count: Optional[int] = None,
        total_count: Optional[int] = None,
    ) -> TaskExecutionHistory:
        """작업 종료 시 status(SUCCESS/FAILED)와 결과를 기록한다."""
        history.status = status
        history.finished_at = datetime.now(timezone.utc)
        history.result_summary = result_summary[:1000] if result_summary else None
        history.error_message = error_message[:2000] if error_message else None
        history.processed_count = processed_count
        history.total_count = total_count
        self.session.flush()
        return history

    def list_recent(
        self,
        task_type: Optional[str] = None,
        status: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        limit: int = 50,
    ) -> list[TaskExecutionHistory]:
        """시스템 모니터링 > 작업이력 탭: 유형/상태/기간 필터."""
        stmt = select(TaskExecutionHistory)
        if task_type is not None:
            stmt = stmt.where(TaskExecutionHistory.task_type == task_type)
        if status is not None:
            stmt = stmt.where(TaskExecutionHistory.status == status)
        if start_date is not None:
            stmt = stmt.where(TaskExecutionHistory.started_at >= start_date)
        if end_date is not None:
            stmt = stmt.where(TaskExecutionHistory.started_at < end_date)
        stmt = stmt.order_by(TaskExecutionHistory.started_at.desc()).limit(limit)
        return list(self.session.execute(stmt).scalars().all())


class RecentViewRepository(BaseRepository[RecentView]):
    """SRS UI v1.1 6장: 동일 대상 재열람 시 새 행을 만들지 않고 viewed_at만 갱신한다."""

    def __init__(self, session: Session) -> None:
        super().__init__(session, RecentView)

    def touch(self, user_id: int, target_type: str, target_id: int) -> RecentView:
        stmt = select(RecentView).where(
            RecentView.user_id == user_id, RecentView.target_type == target_type, RecentView.target_id == target_id
        )
        existing = self.session.execute(stmt).scalar_one_or_none()
        now = datetime.now(timezone.utc)
        if existing is not None:
            existing.viewed_at = now
            self.session.flush()
            return existing
        return self.add(RecentView(user_id=user_id, target_type=target_type, target_id=target_id, viewed_at=now))

    def list_recent(self, user_id: int, limit: int = 10) -> list[RecentView]:
        stmt = (
            select(RecentView).where(RecentView.user_id == user_id).order_by(RecentView.viewed_at.desc()).limit(limit)
        )
        return list(self.session.execute(stmt).scalars().all())


class FavoriteRepository(BaseRepository[Favorite]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Favorite)

    def list_by_user(self, user_id: int, target_type: Optional[str] = None) -> list[Favorite]:
        stmt = select(Favorite).where(Favorite.user_id == user_id)
        if target_type is not None:
            stmt = stmt.where(Favorite.target_type == target_type)
        stmt = stmt.order_by(Favorite.created_at.desc())
        return list(self.session.execute(stmt).scalars().all())

    def get(self, user_id: int, target_type: str, target_id: int) -> Optional[Favorite]:
        stmt = select(Favorite).where(
            Favorite.user_id == user_id, Favorite.target_type == target_type, Favorite.target_id == target_id
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def toggle(self, user_id: int, target_type: str, target_id: int) -> bool:
        """이미 즐겨찾기면 삭제하고 False, 아니면 추가하고 True를 반환한다."""
        existing = self.get(user_id, target_type, target_id)
        if existing is not None:
            self.delete(existing)
            return False
        self.add(
            Favorite(
                user_id=user_id, target_type=target_type, target_id=target_id, created_at=datetime.now(timezone.utc)
            )
        )
        return True


class IntegrationStatusRepository(BaseRepository[IntegrationStatus]):
    """SRS UI v1.1 4.1 연동상태 탭. task_execution_history 종료 시 이 테이블을 갱신한다."""

    def __init__(self, session: Session) -> None:
        super().__init__(session, IntegrationStatus)

    def list_all_status(self) -> list[IntegrationStatus]:
        stmt = select(IntegrationStatus).order_by(
            IntegrationStatus.integration_type, IntegrationStatus.integration_code
        )
        return list(self.session.execute(stmt).scalars().all())

    def get_by_type_and_code(self, integration_type: str, integration_code: str) -> Optional[IntegrationStatus]:
        stmt = select(IntegrationStatus).where(
            IntegrationStatus.integration_type == integration_type,
            IntegrationStatus.integration_code == integration_code,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def upsert_success(self, integration_type: str, integration_code: str) -> IntegrationStatus:
        record = self.get_by_type_and_code(integration_type, integration_code)
        now = datetime.now(timezone.utc)
        if record is None:
            record = IntegrationStatus(
                integration_type=integration_type,
                integration_code=integration_code,
                status="NORMAL",
                last_success_at=now,
                updated_at=now,
            )
            return self.add(record)
        record.status = "NORMAL"
        record.last_success_at = now
        record.updated_at = now
        self.session.flush()
        return record

    def upsert_error(self, integration_type: str, integration_code: str, error_message: str) -> IntegrationStatus:
        record = self.get_by_type_and_code(integration_type, integration_code)
        now = datetime.now(timezone.utc)
        if record is None:
            record = IntegrationStatus(
                integration_type=integration_type,
                integration_code=integration_code,
                status="ERROR",
                last_error_at=now,
                last_error_message=error_message[:2000],
                updated_at=now,
            )
            return self.add(record)
        record.status = "ERROR"
        record.last_error_at = now
        record.last_error_message = error_message[:2000]
        record.updated_at = now
        self.session.flush()
        return record


class MemoRepository(BaseRepository[Memo]):
    """주문/상품/고객별 메모(다형성 target_type/target_id). 주문상세 화면의 메모 기능에서 사용."""

    def __init__(self, session: Session) -> None:
        super().__init__(session, Memo)

    def list_by_target(self, target_type: str, target_id: int) -> list[Memo]:
        stmt = (
            select(Memo)
            .where(Memo.target_type == target_type, Memo.target_id == target_id)
            .order_by(Memo.created_at.desc())
        )
        return list(self.session.execute(stmt).scalars().all())
