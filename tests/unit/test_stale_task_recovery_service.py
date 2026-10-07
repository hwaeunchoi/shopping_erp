"""
tests/unit/test_stale_task_recovery_service.py
------------------------------------------------
services.stale_task_recovery_service - 재시작으로 RUNNING에 남은 scheduler 작업 이력을 FAILED +
PROCESS_INTERRUPTED로 정리하는 정책 검증(합성 데이터만 사용).
"""

from datetime import datetime, timedelta

import pytest

from config.settings import settings
from models.extra import TaskExecutionHistory
from repositories.extra_repository import TaskExecutionHistoryRepository
from services.cs_channel_sync_service import COUPANG_CALL_CENTER_SOURCE
from services.cs_sync_lock import cs_sync_source_lock
from services.stale_task_recovery_service import (
    PROCESS_INTERRUPTED,
    make_target_active_probe,
    recover_stale_running_tasks,
)

NOW = datetime(2026, 10, 7, 1, 0, 0)  # naive UTC
TARGETS = ["product_sync", "order_collect", "cs_inquiry_sync", "cs_inquiry_catchup", "backup"]


def _row(
    db_session,
    *,
    target: str = "product_sync",
    status: str = "RUNNING",
    trigger_type: str = "SCHEDULE",
    age: timedelta = timedelta(days=21),
    task_type: str = "PRODUCT_SYNC",
) -> TaskExecutionHistory:
    row = TaskExecutionHistory(
        task_type=task_type, target=target, trigger_type=trigger_type, status=status, started_at=NOW - age
    )
    db_session.add(row)
    db_session.commit()
    return row


def _never_active(_target: str) -> bool:
    return False


class TestRecovery:
    def test_default_threshold_is_six_hours(self):
        assert settings.task_stale_running_threshold_minutes == 360

    def test_stale_running_row_becomes_failed_with_process_interrupted(self, db_session):
        row = _row(db_session)  # 3주 전 PRODUCT_SYNC - 운영에서 실제로 발견된 형태

        result = recover_stale_running_tasks(db_session, TARGETS, now=NOW, is_target_active=_never_active)
        db_session.commit()

        assert result == {"recovered": 1, "skipped_active": 0}
        db_session.refresh(row)
        assert row.status == "FAILED"
        assert row.error_message == PROCESS_INTERRUPTED == "PROCESS_INTERRUPTED"
        assert row.finished_at is not None
        assert row.result_summary is None  # 원본 오류 문자열/요약 없음

    def test_recent_running_row_is_left_alone(self, db_session):
        row = _row(db_session, age=timedelta(minutes=10))
        result = recover_stale_running_tasks(db_session, TARGETS, now=NOW, is_target_active=_never_active)
        db_session.refresh(row)
        assert result["recovered"] == 0
        assert row.status == "RUNNING"
        assert row.finished_at is None

    def test_long_but_within_threshold_job_is_not_terminated(self, db_session):
        """정상적으로 오래 걸리는 활성 작업(예: 5시간째 실행 중)을 종료 처리하지 않는다."""
        row = _row(db_session, age=timedelta(hours=5))
        recover_stale_running_tasks(db_session, TARGETS, now=NOW, is_target_active=_never_active)
        db_session.refresh(row)
        assert row.status == "RUNNING"

    def test_threshold_boundary(self, db_session):
        just_inside = _row(db_session, target="order_collect", age=timedelta(minutes=359))
        just_outside = _row(db_session, target="product_sync", age=timedelta(minutes=361))
        recover_stale_running_tasks(db_session, TARGETS, now=NOW, is_target_active=_never_active)
        db_session.refresh(just_inside)
        db_session.refresh(just_outside)
        assert just_inside.status == "RUNNING"
        assert just_outside.status == "FAILED"

    def test_target_with_active_execution_is_kept(self, db_session):
        row = _row(db_session, target="cs_inquiry_sync", task_type="FULL_SYNC")
        result = recover_stale_running_tasks(db_session, TARGETS, now=NOW, is_target_active=lambda t: True)
        db_session.refresh(row)
        assert result == {"recovered": 0, "skipped_active": 1}
        assert row.status == "RUNNING"

    def test_only_the_active_target_is_kept_other_targets_are_recovered(self, db_session):
        active = _row(db_session, target="cs_inquiry_sync", task_type="FULL_SYNC")
        other = _row(db_session, target="product_sync")
        result = recover_stale_running_tasks(
            db_session, TARGETS, now=NOW, is_target_active=lambda t: t == "cs_inquiry_sync"
        )
        db_session.refresh(active)
        db_session.refresh(other)
        assert result == {"recovered": 1, "skipped_active": 1}
        assert active.status == "RUNNING"
        assert other.status == "FAILED"

    def test_targets_outside_the_given_job_list_are_untouched(self, db_session):
        row = _row(db_session, target="some_api_triggered_task")
        recover_stale_running_tasks(db_session, TARGETS, now=NOW, is_target_active=_never_active)
        db_session.refresh(row)
        assert row.status == "RUNNING"

    def test_manual_trigger_rows_are_untouched(self, db_session):
        """API(다른 프로세스)가 만든 수동 실행 이력은 이 scheduler가 판단할 수 없으므로 건드리지 않는다."""
        row = _row(db_session, trigger_type="MANUAL")
        recover_stale_running_tasks(db_session, TARGETS, now=NOW, is_target_active=_never_active)
        db_session.refresh(row)
        assert row.status == "RUNNING"

    def test_catchup_trigger_rows_are_recoverable(self, db_session):
        row = _row(db_session, target="cs_inquiry_catchup", trigger_type="CATCHUP", task_type="FULL_SYNC")
        recover_stale_running_tasks(db_session, TARGETS, now=NOW, is_target_active=_never_active)
        db_session.refresh(row)
        assert row.status == "FAILED"

    @pytest.mark.parametrize("status", ["SUCCESS", "FAILED"])
    def test_finished_rows_are_untouched(self, db_session, status):
        row = _row(db_session, status=status)
        recover_stale_running_tasks(db_session, TARGETS, now=NOW, is_target_active=_never_active)
        db_session.refresh(row)
        assert row.status == status
        assert row.error_message is None

    def test_second_call_is_a_no_op(self, db_session):
        _row(db_session)
        recover_stale_running_tasks(db_session, TARGETS, now=NOW, is_target_active=_never_active)
        db_session.commit()
        again = recover_stale_running_tasks(db_session, TARGETS, now=NOW, is_target_active=_never_active)
        assert again == {"recovered": 0, "skipped_active": 0}


class TestDefaultActivityProbe:
    def test_cs_target_with_a_real_held_source_lock_is_kept(self, db_session, platform):
        row = _row(db_session, target="cs_inquiry_sync", task_type="FULL_SYNC")
        with cs_sync_source_lock(platform.id, COUPANG_CALL_CENTER_SOURCE, db_session.get_bind()) as held:
            assert held is True
            result = recover_stale_running_tasks(db_session, TARGETS, now=NOW)
        db_session.refresh(row)
        assert result["skipped_active"] == 1
        assert row.status == "RUNNING"

    def test_cs_target_without_any_held_lock_is_recovered(self, db_session, platform):
        row = _row(db_session, target="cs_inquiry_sync", task_type="FULL_SYNC")
        result = recover_stale_running_tasks(db_session, TARGETS, now=NOW)
        db_session.commit()
        db_session.refresh(row)
        assert result["recovered"] == 1
        assert row.status == "FAILED"

    def test_probe_reports_false_for_targets_without_a_lock(self, db_session, platform):
        probe = make_target_active_probe(db_session)
        assert probe("product_sync") is False
        assert probe("backup") is False  # SQLite에는 백업 advisory lock이 없다


class TestRunningSplitForMonitoring:
    def test_recent_and_stale_running_rows_are_counted_separately(self, db_session):
        _row(db_session, age=timedelta(minutes=5))
        _row(db_session, target="order_collect", age=timedelta(hours=2))
        _row(db_session, target="backup", age=timedelta(days=21))
        _row(db_session, target="claim_sync", age=timedelta(days=2), status="SUCCESS")
        cutoff = NOW - timedelta(minutes=settings.task_stale_running_threshold_minutes)

        counts = TaskExecutionHistoryRepository(db_session).count_running_split(cutoff)

        assert counts == {"recent": 2, "stale": 1}

    def test_no_running_rows_counts_zero(self, db_session):
        assert TaskExecutionHistoryRepository(db_session).count_running_split(NOW) == {"recent": 0, "stale": 0}

    def test_recovered_rows_do_not_count_as_running_any_more(self, db_session):
        _row(db_session)
        recover_stale_running_tasks(db_session, TARGETS, now=NOW, is_target_active=_never_active)
        db_session.commit()
        cutoff = NOW - timedelta(minutes=settings.task_stale_running_threshold_minutes)
        assert TaskExecutionHistoryRepository(db_session).count_running_split(cutoff) == {"recent": 0, "stale": 0}
