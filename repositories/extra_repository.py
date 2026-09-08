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
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from models.extra import Attachment, Favorite, IntegrationStatus, Memo, RecentView, ReportSchedule, TaskExecutionHistory
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
        """동일 대상 재열람 시 새 행을 만들지 않고 viewed_at만 갱신한다.

        (user_id, target_type, target_id) 유니크 제약(uq_recent_view -
        migrations/versions/20260904_1530_c4cd4340574e_recent_views_unique_constraint.py)을
        원자적 INSERT ... ON CONFLICT DO UPDATE 한 문장으로 흡수한다. 예전
        방식("조회 후 없으면 INSERT")은 두 요청이 동시에 같은 조합을 처음
        기록하려 하면 조회 시점엔 둘 다 "없음"으로 보여(TOCTOU) 하나가 유니크
        제약 위반(IntegrityError)을 던졌다 - 이 메서드가 그 예외를 잡아
        "중복이니 무시"하는 방식은 쓰지 않는다(정말 다른 원인으로 난
        IntegrityError까지 조용히 삼킬 위험이 있다). 대신 DB가 그 경합 자체를
        단일 문장 안에서 직렬화하게 만들어, 두 요청 모두 예외 없이 끝나고
        정확히 한 행만 남는다.

        WHERE viewed_at < excluded.viewed_at로 갱신 조건을 걸어, 나중에 실행이
        끝난 요청이라도 그 요청이 들고 있던 시각이 이미 저장된 값보다 과거이면
        (다른 요청이 그사이 더 최신 시각으로 먼저 기록한 경우) viewed_at을
        뒤로 되돌리지 않는다 - 이 조건에 걸려 갱신이 스킵되면 RETURNING이 빈
        결과를 주므로, 그 경우에만 별도 조회로 현재 행을 그대로 반환한다."""
        now = datetime.now(timezone.utc)
        dialect_name = self.session.get_bind().dialect.name
        insert_fn = pg_insert if dialect_name == "postgresql" else sqlite_insert
        insert_stmt = insert_fn(RecentView).values(
            user_id=user_id, target_type=target_type, target_id=target_id, viewed_at=now
        )
        upsert_stmt = insert_stmt.on_conflict_do_update(
            index_elements=["user_id", "target_type", "target_id"],
            set_={"viewed_at": insert_stmt.excluded.viewed_at},
            where=(RecentView.viewed_at < insert_stmt.excluded.viewed_at),
        ).returning(RecentView.id)
        row_id = self.session.execute(upsert_stmt).scalar_one_or_none()
        self.session.flush()
        if row_id is None:
            return self.session.execute(
                select(RecentView).where(
                    RecentView.user_id == user_id,
                    RecentView.target_type == target_type,
                    RecentView.target_id == target_id,
                )
            ).scalar_one()
        # populate_existing=True: 이 행이 세션 identity map에 이미 로드돼 있으면
        # (예: 같은 요청 안에서 먼저 조회한 적이 있으면) get()이 캐시된 예전
        # 파이썬 객체를 그대로 돌려줘 방금 UPDATE로 바뀐 viewed_at을 반영하지
        # 못한다 - Core로 실행한 UPDATE는 이미 로드된 ORM 인스턴스를 자동으로
        # 갱신하지 않기 때문이다. 항상 DB의 최신 값으로 다시 채운다.
        view = self.session.get(RecentView, row_id, populate_existing=True)
        assert view is not None  # 방금 INSERT/UPDATE로 확정된 행 - 항상 존재한다.
        return view

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


class AttachmentRepository(BaseRepository[Attachment]):
    """첨부파일 메타데이터(다형성 target_type/target_id) - 상용 ERP 확장(5단계, B묶음)에서
    CS 케이스(target_type="CS_CASE") 조회용으로 처음 실사용한다. 실제 업로드
    저장소는 이 코드베이스에 아직 없으므로(models/extra.py의 Attachment는 이전까지
    스키마만 있고 어떤 라우터/서비스도 쓰지 않았다) file_path는 클라이언트가 이미
    다른 방식으로 확보한 참조(예: 채널이 제공한 첨부 URL)만 저장한다 - 새 업로드
    파이프라인을 이번 단계에서 만들지 않는다."""

    def __init__(self, session: Session) -> None:
        super().__init__(session, Attachment)

    def list_by_target(self, target_type: str, target_id: int) -> list[Attachment]:
        stmt = (
            select(Attachment)
            .where(Attachment.target_type == target_type, Attachment.target_id == target_id)
            .order_by(Attachment.created_at.desc())
        )
        return list(self.session.execute(stmt).scalars().all())
