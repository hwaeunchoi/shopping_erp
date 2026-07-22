"""
tests/unit/test_extra_repository.py
------------------------------------------
ReportScheduleRepository/TaskExecutionHistoryRepository/RecentViewRepository/
FavoriteRepository/IntegrationStatusRepository 단위 테스트. SRS 3.7 보고서
(report_schedules), FR-LOG-01 연장(task_execution_history), UI v1.1
addendum(최근조회/즐겨찾기/시스템 모니터링) 대응.
"""

from datetime import datetime, timedelta, timezone

from models.extra import ReportSchedule
from repositories.extra_repository import (
    FavoriteRepository,
    IntegrationStatusRepository,
    RecentViewRepository,
    ReportScheduleRepository,
    TaskExecutionHistoryRepository,
)


def _make_user(db_session, username="testuser"):
    from models.user import Role, User

    role = Role(name=f"role-{username}")
    db_session.add(role)
    db_session.flush()
    user = User(username=username, password_hash="x", name="테스터", role_id=role.id, is_active=True)
    db_session.add(user)
    db_session.flush()
    return user


def _make_schedule(db_session, next_run_at, is_enabled=True, frequency="MONTHLY"):
    schedule = ReportSchedule(
        report_type="PROFIT_REPORT",
        frequency=frequency,
        output_format="XLSX",
        is_enabled=is_enabled,
        next_run_at=next_run_at,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(schedule)
    db_session.flush()
    return schedule


class TestReportScheduleRepository:
    def test_list_due_returns_only_enabled_and_past_due(self, db_session):
        now = datetime.now(timezone.utc)
        due = _make_schedule(db_session, next_run_at=now - timedelta(hours=1))
        not_due_yet = _make_schedule(db_session, next_run_at=now + timedelta(days=1))
        disabled_but_due = _make_schedule(db_session, next_run_at=now - timedelta(hours=1), is_enabled=False)

        result = ReportScheduleRepository(db_session).list_due(now)

        result_ids = {s.id for s in result}
        assert due.id in result_ids
        assert not_due_yet.id not in result_ids
        assert disabled_but_due.id not in result_ids

    def test_list_enabled_excludes_disabled(self, db_session):
        now = datetime.now(timezone.utc)
        enabled = _make_schedule(db_session, next_run_at=now, is_enabled=True)
        _make_schedule(db_session, next_run_at=now, is_enabled=False)

        result = ReportScheduleRepository(db_session).list_enabled()

        assert [s.id for s in result] == [enabled.id]


class TestTaskExecutionHistoryRepository:
    def test_start_creates_running_row(self, db_session):
        history = TaskExecutionHistoryRepository(db_session).start(
            task_type="REPORT_GENERATE", trigger_type="SCHEDULE", target="report_generate"
        )

        assert history.id is not None
        assert history.status == "RUNNING"
        assert history.finished_at is None

    def test_finish_updates_status_and_result(self, db_session):
        repo = TaskExecutionHistoryRepository(db_session)
        history = repo.start(task_type="REPORT_GENERATE", trigger_type="SCHEDULE")

        repo.finish(history, status="SUCCESS", result_summary="{'processed': 1}")

        assert history.status == "SUCCESS"
        assert history.finished_at is not None
        assert history.result_summary == "{'processed': 1}"

    def test_finish_with_error_records_message(self, db_session):
        repo = TaskExecutionHistoryRepository(db_session)
        history = repo.start(task_type="ORDER_COLLECT", trigger_type="SCHEDULE")

        repo.finish(history, status="FAILED", error_message="connector timeout")

        assert history.status == "FAILED"
        assert history.error_message == "connector timeout"

    def test_list_recent_filters_by_task_type(self, db_session):
        repo = TaskExecutionHistoryRepository(db_session)
        repo.start(task_type="REPORT_GENERATE", trigger_type="SCHEDULE")
        repo.start(task_type="ORDER_COLLECT", trigger_type="SCHEDULE")

        result = repo.list_recent(task_type="REPORT_GENERATE")

        assert len(result) == 1
        assert result[0].task_type == "REPORT_GENERATE"

    def test_list_recent_filters_by_status_and_date_range(self, db_session):
        repo = TaskExecutionHistoryRepository(db_session)
        success = repo.start(task_type="BACKUP", trigger_type="SCHEDULE")
        repo.finish(success, status="SUCCESS")
        failed = repo.start(task_type="BACKUP", trigger_type="SCHEDULE")
        repo.finish(failed, status="FAILED", error_message="disk full")

        result = repo.list_recent(status="FAILED")

        assert [r.id for r in result] == [failed.id]

        now = datetime.now(timezone.utc)
        future_only = repo.list_recent(start_date=now + timedelta(days=1))
        assert future_only == []


class TestRecentViewRepository:
    """UI v1.1 6장: 동일 대상 재열람 시 viewed_at만 갱신한다."""

    def test_touch_creates_new_row(self, db_session):
        user = _make_user(db_session)

        view = RecentViewRepository(db_session).touch(user.id, "PRODUCT", 42)

        assert view.id is not None
        assert view.target_type == "PRODUCT"
        assert view.target_id == 42

    def test_touch_same_target_updates_viewed_at_without_duplicate(self, db_session):
        user = _make_user(db_session)
        repo = RecentViewRepository(db_session)

        first = repo.touch(user.id, "ORDER", 1)
        second = repo.touch(user.id, "ORDER", 1)

        assert first.id == second.id
        assert len(repo.list_recent(user.id)) == 1

    def test_list_recent_orders_by_viewed_at_desc_and_limits(self, db_session):
        user = _make_user(db_session)
        repo = RecentViewRepository(db_session)
        for target_id in range(1, 4):
            repo.touch(user.id, "PRODUCT", target_id)

        result = repo.list_recent(user.id, limit=2)

        assert len(result) == 2
        assert result[0].target_id == 3  # 가장 최근에 touch한 것

    def test_list_recent_scoped_to_user(self, db_session):
        user1 = _make_user(db_session, "user1")
        user2 = _make_user(db_session, "user2")
        repo = RecentViewRepository(db_session)
        repo.touch(user1.id, "PRODUCT", 1)
        repo.touch(user2.id, "PRODUCT", 2)

        result = repo.list_recent(user1.id)

        assert [r.target_id for r in result] == [1]


class TestFavoriteRepository:
    def test_toggle_adds_then_removes(self, db_session):
        user = _make_user(db_session)
        repo = FavoriteRepository(db_session)

        added = repo.toggle(user.id, "PRODUCT", 10)
        assert added is True
        assert repo.get(user.id, "PRODUCT", 10) is not None

        removed = repo.toggle(user.id, "PRODUCT", 10)
        assert removed is False
        assert repo.get(user.id, "PRODUCT", 10) is None

    def test_list_by_user_filters_by_target_type(self, db_session):
        user = _make_user(db_session)
        repo = FavoriteRepository(db_session)
        repo.toggle(user.id, "PRODUCT", 1)
        repo.toggle(user.id, "REPORT", 2)

        products_only = repo.list_by_user(user.id, target_type="PRODUCT")

        assert len(products_only) == 1
        assert products_only[0].target_type == "PRODUCT"


class TestIntegrationStatusRepository:
    def test_upsert_success_creates_then_updates(self, db_session):
        repo = IntegrationStatusRepository(db_session)

        first = repo.upsert_success("MALL", "coupang")
        assert first.status == "NORMAL"
        assert first.last_success_at is not None

        second = repo.upsert_success("MALL", "coupang")
        assert second.id == first.id
        assert len(repo.list_all_status()) == 1

    def test_upsert_error_records_message(self, db_session):
        repo = IntegrationStatusRepository(db_session)

        record = repo.upsert_error("AD", "naver_search_ad", "API 인증 오류")

        assert record.status == "ERROR"
        assert record.last_error_message == "API 인증 오류"

    def test_error_then_success_transitions_status(self, db_session):
        repo = IntegrationStatusRepository(db_session)
        repo.upsert_error("MALL", "coupang", "timeout")

        recovered = repo.upsert_success("MALL", "coupang")

        assert recovered.status == "NORMAL"
        assert recovered.last_error_message == "timeout"  # 마지막 오류 메시지는 이력으로 남겨둔다
