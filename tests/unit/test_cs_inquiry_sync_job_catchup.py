"""
tests/unit/test_cs_inquiry_sync_job_catchup.py
-----------------------------------------------------
scheduler.jobs.cs_inquiry_sync_job의 15분 정기 실행(run)·시작 직후 catch-up(run_catchup) 흐름을
테스트 DB로 검증한다. session_scope()는 테스트 세션을 그대로 돌려주도록 바꾸고(닫지 않음),
커넥터는 스텁으로 교체한다 - 실제 채널 API는 호출하지 않는다.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

import pytest

from config.settings import settings
from repositories.cs_case_repository import CsCaseRepository
from repositories.extra_repository import IntegrationStatusRepository
from scheduler.jobs import cs_inquiry_sync_job
from services import cs_inquiry_catchup_service
from services.cs_channel_sync_service import COUPANG_CALL_CENTER_SOURCE, COUPANG_PRODUCT_INQUIRY_SOURCE
from services.cs_inquiry_catchup_service import CsSyncCheckpointRepository
from services.cs_sync_lock import cs_sync_source_lock

CC = COUPANG_CALL_CENTER_SOURCE
# 시계를 고정한다(KST 2026-10-07 10:00) - 날짜 경계/실행 순서에 따라 결과가 달라지지 않도록.
FIXED_NOW = datetime(2026, 10, 7, 1, 0, 0)
PR = COUPANG_PRODUCT_INQUIRY_SOURCE


class _Connector:
    supports_inquiry_sync = True
    supports_product_inquiry_sync = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, Any]] = []

    def fetch_inquiries(self, start_date, end_date, *, max_pages=None, max_retries=None, request_budget=None):
        self.calls.append((CC, start_date, end_date))
        request_budget.consume("coupang")
        return [
            {
                "platform_inquiry_id": "c-1",
                "content": "문의",
                "inquiry_at": FIXED_NOW,
                "raw_status": "progress:requestAnswer",
                "needs_answer": True,
                "platform_order_no": None,
                "customer_phone": None,
            }
        ]

    def fetch_product_inquiries(self, start_date, end_date, *, max_pages=None, max_retries=None, request_budget=None):
        self.calls.append((PR, start_date, end_date))
        request_budget.consume("coupang")
        return []


@pytest.fixture()
def wired(monkeypatch, db_session, platform):
    """job이 테스트 세션/스텁 커넥터를 쓰도록 연결하고 기능 플래그를 켠다."""
    monkeypatch.setattr(settings, "cs_inquiry_sync_enabled", True)
    monkeypatch.setattr(settings, "cs_inquiry_sync_window_days", 1)
    db_session.commit()  # fixture가 flush만 한 platform 행을 보존한다
    monkeypatch.setattr(cs_inquiry_catchup_service, "_utcnow_naive", lambda: FIXED_NOW)

    @contextmanager
    def _scope():
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    connector = _Connector()
    monkeypatch.setattr(cs_inquiry_sync_job, "session_scope", _scope)
    monkeypatch.setattr(cs_inquiry_sync_job, "get_mall_connector", lambda *a, **k: connector)
    return connector


class TestPeriodicRun:
    def test_periodic_run_creates_checkpoints_and_cases_and_records_integration_status(
        self, wired, db_session, platform
    ):
        result = cs_inquiry_sync_job.run()

        assert result[platform.code]["status"] == "SUCCESS"
        assert CsSyncCheckpointRepository(db_session).get_covered_until(platform.id, CC) is not None
        assert CsSyncCheckpointRepository(db_session).get_covered_until(platform.id, PR) is not None
        assert CsCaseRepository(db_session).get_by_external(platform.id, CC, "c-1") is not None
        status = IntegrationStatusRepository(db_session).get_by_type_and_code("CS_INQUIRY", platform.code)
        assert status is not None and status.status == "NORMAL"

    def test_periodic_run_always_executes_even_when_checkpoint_is_fresh(self, wired, db_session, platform):
        cs_inquiry_sync_job.run()
        wired.calls.clear()

        cs_inquiry_sync_job.run()

        assert len(wired.calls) == 2  # 정기 실행은 건너뛰지 않고 오늘을 다시 조회한다(dedup으로 수렴)
        assert CsCaseRepository(db_session).get_by_external(platform.id, CC, "c-1") is not None

    def test_periodic_run_while_lock_is_held_reports_already_running_and_calls_nothing(
        self, wired, db_session, platform
    ):
        with (
            cs_sync_source_lock(platform.id, CC, db_session.get_bind()),
            cs_sync_source_lock(platform.id, PR, db_session.get_bind()),
        ):
            result = cs_inquiry_sync_job.run()

        assert result[platform.code]["status"] == "ALREADY_RUNNING"
        assert wired.calls == []
        # ALREADY_RUNNING은 연동 상태를 오류로 기록하지 않는다.
        assert IntegrationStatusRepository(db_session).get_by_type_and_code("CS_INQUIRY", platform.code) is None


class TestStartupCatchup:
    def test_runs_exactly_once_when_period_is_unprocessed_then_skips_up_to_date(self, wired, db_session, platform):
        three_days_ago = FIXED_NOW - timedelta(days=3)
        for source in (CC, PR):
            CsSyncCheckpointRepository(db_session).advance(platform.id, source, three_days_ago)
        db_session.commit()

        first = cs_inquiry_sync_job.run_catchup()
        calls_after_first = len(wired.calls)
        second = cs_inquiry_sync_job.run_catchup()

        assert first[platform.code]["status"] == "SUCCESS"
        assert calls_after_first == 2  # 콜센터 1 + 상품별 1 (3일 공백 = 구간 1개)
        assert second == {platform.code: {"skipped_up_to_date": 1}}
        assert len(wired.calls) == calls_after_first  # 두 번째 호출은 외부 호출 0건

    def test_no_checkpoint_at_all_triggers_one_run(self, wired, db_session, platform):
        result = cs_inquiry_sync_job.run_catchup()
        assert result[platform.code]["status"] == "SUCCESS"
        assert len(wired.calls) == 2

    def test_catchup_and_periodic_run_race_exactly_one_proceeds(self, wired, db_session, platform):
        """catch-up이 잠금을 들고 있는 동안 도착한 정기 실행은 외부 호출·쓰기 없이 물러난다."""
        with (
            cs_sync_source_lock(platform.id, CC, db_session.get_bind()),
            cs_sync_source_lock(platform.id, PR, db_session.get_bind()),
        ):
            periodic = cs_inquiry_sync_job.run()
        assert periodic[platform.code]["status"] == "ALREADY_RUNNING"
        assert wired.calls == []

        catchup = cs_inquiry_sync_job.run_catchup()
        assert catchup[platform.code]["status"] == "SUCCESS"
        assert len(wired.calls) == 2

    def test_failed_catchup_is_not_retried_and_leaves_checkpoint_for_next_periodic_run(
        self, wired, db_session, platform, monkeypatch
    ):
        from integrations.malls.errors import MarketplaceExternalAPIError

        def _fail(*a, **k):
            wired.calls.append((CC, None, None))
            raise MarketplaceExternalAPIError("coupang", "SERVER_ERROR", True, http_status=500)

        original_fetch = wired.fetch_inquiries
        monkeypatch.setattr(wired, "fetch_inquiries", _fail)

        result = cs_inquiry_sync_job.run_catchup()

        assert result[platform.code]["by_source"][CC]["status"] == "FAILED"
        assert sum(1 for src, _, _ in wired.calls if src == CC) == 1  # 자체 재시도 없음
        assert CsSyncCheckpointRepository(db_session).get_covered_until(platform.id, CC) is None

        # 다음 15분 정기 실행이 자연스럽게 이어받아 성공한다.
        monkeypatch.setattr(wired, "fetch_inquiries", original_fetch)
        later = cs_inquiry_sync_job.run()
        assert later[platform.code]["by_source"][CC]["status"] == "SUCCESS"
        assert CsSyncCheckpointRepository(db_session).get_covered_until(platform.id, CC) is not None
