"""
tests/unit/test_report_generate_job.py
---------------------------------------------
scheduler/jobs/report_generate_job.py의 순수 헬퍼 함수 단위 테스트.

run() 본체는 core.database.session_scope()(실제 DB 엔진에 바인딩된
SessionLocal)를 사용해 이 프로젝트의 다른 스케줄러 잡들과 마찬가지로
테스트 DB로 격리할 수 없으므로, 날짜 계산처럼 부작용 없는 로직만
단위 테스트한다.
"""

from datetime import datetime, timezone

from scheduler.jobs.report_generate_job import _advance_next_run, _previous_month


class TestPreviousMonth:
    def test_returns_prior_month_within_same_year(self):
        assert _previous_month(datetime(2026, 7, 15, tzinfo=timezone.utc)) == (2026, 6)

    def test_returns_december_of_prior_year_when_january(self):
        assert _previous_month(datetime(2026, 1, 5, tzinfo=timezone.utc)) == (2025, 12)


class TestAdvanceNextRun:
    def test_daily_adds_one_day(self):
        current = datetime(2026, 7, 1, tzinfo=timezone.utc)
        assert _advance_next_run(current, "DAILY") == datetime(2026, 7, 2, tzinfo=timezone.utc)

    def test_weekly_adds_seven_days(self):
        current = datetime(2026, 7, 1, tzinfo=timezone.utc)
        assert _advance_next_run(current, "WEEKLY") == datetime(2026, 7, 8, tzinfo=timezone.utc)

    def test_monthly_adds_one_month(self):
        current = datetime(2026, 7, 1, tzinfo=timezone.utc)
        assert _advance_next_run(current, "MONTHLY") == datetime(2026, 8, 1, tzinfo=timezone.utc)

    def test_monthly_rolls_over_year_boundary(self):
        current = datetime(2026, 12, 1, tzinfo=timezone.utc)
        assert _advance_next_run(current, "MONTHLY") == datetime(2027, 1, 1, tzinfo=timezone.utc)
