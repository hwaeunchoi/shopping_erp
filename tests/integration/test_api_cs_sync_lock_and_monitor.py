"""
tests/integration/test_api_cs_sync_lock_and_monitor.py
---------------------------------------------------------
CS 문의 자동수집 보강(15분 정기 + catch-up + stale 정리)의 API 접점 검증:
1) POST /api/cs-cases/sync(수동 sync)가 자동 실행과 같은 (platform, source) 잠금을 공유한다.
2) GET /api/system-monitor/running-tasks가 최근 RUNNING과 stale RUNNING을 분리해 센다.
3) checkpoint 행(integration_status, type=CS_CHECKPOINT)이 연동상태 화면에 노출되지 않는다.

실제 채널은 호출하지 않는다(모의 커넥터). 합성 데이터만 사용한다.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from config.settings import settings
from models.extra import IntegrationStatus, TaskExecutionHistory
from services.cs_channel_sync_service import COUPANG_CALL_CENTER_SOURCE, COUPANG_PRODUCT_INQUIRY_SOURCE
from services.cs_inquiry_catchup_service import CsSyncCheckpointRepository
from services.cs_sync_lock import cs_sync_source_lock

CC = COUPANG_CALL_CENTER_SOURCE
PR = COUPANG_PRODUCT_INQUIRY_SOURCE


def _connector() -> MagicMock:
    connector = MagicMock()
    connector.supports_inquiry_sync = True
    connector.supports_product_inquiry_sync = True
    connector.fetch_inquiries.return_value = []
    connector.fetch_product_inquiries.return_value = []
    return connector


class TestManualSyncSharesTheAutomaticRunLock:
    def test_all_sources_sync_is_rejected_when_one_source_is_already_running(
        self, client, auth_headers, seed_data, api_engine
    ):
        connector = _connector()
        with (
            patch.object(settings, "cs_inquiry_sync_enabled", True),
            patch("api.routers.cs_cases.get_mall_connector", return_value=connector),
            cs_sync_source_lock(seed_data["platform_id"], CC, api_engine) as held,
        ):
            assert held is True
            resp = client.post(
                "/api/cs-cases/sync", json={"platform_id": seed_data["platform_id"], "days": 7}, headers=auth_headers
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "ALREADY_RUNNING"
        assert resp.json()["reason_code"] == "ALREADY_RUNNING"
        connector.fetch_inquiries.assert_not_called()
        connector.fetch_product_inquiries.assert_not_called()
        # 두 번째 잠금(PR)은 잡았다가 첫 번째(CC)가 막혀 거절됐다 - PR 잠금이 남아 있으면 안 된다.
        with cs_sync_source_lock(seed_data["platform_id"], PR, api_engine) as pr_free:
            assert pr_free is True

    def test_single_source_sync_is_rejected_when_that_source_is_running(
        self, client, auth_headers, seed_data, api_engine
    ):
        connector = _connector()
        with (
            patch.object(settings, "cs_inquiry_sync_enabled", True),
            patch("api.routers.cs_cases.get_mall_connector", return_value=connector),
            cs_sync_source_lock(seed_data["platform_id"], PR, api_engine),
        ):
            resp = client.post(
                "/api/cs-cases/sync",
                json={
                    "platform_id": seed_data["platform_id"],
                    "days": 1,
                    "source": PR,
                    "max_pages": 1,
                    "max_retries": 0,
                },
                headers=auth_headers,
            )

        assert resp.json()["status"] == "ALREADY_RUNNING"
        connector.fetch_product_inquiries.assert_not_called()

    def test_single_source_sync_is_not_blocked_by_the_other_sources_lock(
        self, client, auth_headers, seed_data, api_engine
    ):
        connector = _connector()
        with (
            patch.object(settings, "cs_inquiry_sync_enabled", True),
            patch("api.routers.cs_cases.get_mall_connector", return_value=connector),
            cs_sync_source_lock(seed_data["platform_id"], CC, api_engine),
        ):
            resp = client.post(
                "/api/cs-cases/sync",
                json={
                    "platform_id": seed_data["platform_id"],
                    "days": 1,
                    "source": PR,
                    "max_pages": 1,
                    "max_retries": 0,
                },
                headers=auth_headers,
            )

        assert resp.json()["status"] == "SUCCESS"
        connector.fetch_product_inquiries.assert_called_once()

    def test_lock_is_released_after_the_request(self, client, auth_headers, seed_data, api_engine):
        with (
            patch.object(settings, "cs_inquiry_sync_enabled", True),
            patch("api.routers.cs_cases.get_mall_connector", return_value=_connector()),
        ):
            first = client.post(
                "/api/cs-cases/sync", json={"platform_id": seed_data["platform_id"], "days": 7}, headers=auth_headers
            )
            second = client.post(
                "/api/cs-cases/sync", json={"platform_id": seed_data["platform_id"], "days": 7}, headers=auth_headers
            )

        assert first.json()["status"] == "SUCCESS"
        assert second.json()["status"] == "SUCCESS"
        for source in (CC, PR):
            with cs_sync_source_lock(seed_data["platform_id"], source, api_engine) as acquired:
                assert acquired is True

    def test_manual_sync_does_not_move_the_automatic_checkpoint(
        self, client, auth_headers, seed_data, api_session_factory
    ):
        with (
            patch.object(settings, "cs_inquiry_sync_enabled", True),
            patch("api.routers.cs_cases.get_mall_connector", return_value=_connector()),
        ):
            client.post(
                "/api/cs-cases/sync", json={"platform_id": seed_data["platform_id"], "days": 7}, headers=auth_headers
            )

        db = api_session_factory()
        try:
            repo = CsSyncCheckpointRepository(db)
            assert repo.get_covered_until(seed_data["platform_id"], CC) is None
            assert repo.get_covered_until(seed_data["platform_id"], PR) is None
        finally:
            db.close()


class TestRunningTasksMonitor:
    def _seed_task(self, api_session_factory, *, age: timedelta, status: str = "RUNNING", target: str = "product_sync"):
        db = api_session_factory()
        try:
            started = datetime.now(timezone.utc).replace(tzinfo=None) - age
            db.add(
                TaskExecutionHistory(
                    task_type="PRODUCT_SYNC", target=target, trigger_type="SCHEDULE", status=status, started_at=started
                )
            )
            db.commit()
        finally:
            db.close()

    def test_recent_and_stale_running_are_reported_separately(self, client, auth_headers, api_session_factory):
        self._seed_task(api_session_factory, age=timedelta(minutes=3))
        self._seed_task(api_session_factory, age=timedelta(days=21), target="order_collect")
        self._seed_task(api_session_factory, age=timedelta(days=21), status="SUCCESS", target="claim_sync")

        resp = client.get("/api/system-monitor/running-tasks", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json() == {
            "recent_running": 1,
            "stale_running": 1,
            "stale_threshold_minutes": settings.task_stale_running_threshold_minutes,
        }

    def test_requires_authentication(self, client):
        assert client.get("/api/system-monitor/running-tasks").status_code == 401


class TestCheckpointRowsAreHiddenFromIntegrationStatusScreen:
    def test_checkpoint_rows_not_listed(self, client, auth_headers, seed_data, api_session_factory):
        db = api_session_factory()
        try:
            CsSyncCheckpointRepository(db).advance(seed_data["platform_id"], CC, datetime(2026, 10, 7, 0, 0, 0))
            db.add(
                IntegrationStatus(
                    integration_type="CS_INQUIRY",
                    integration_code="coupang",
                    status="NORMAL",
                    updated_at=datetime(2026, 10, 7, 0, 0, 0),
                )
            )
            db.commit()
        finally:
            db.close()

        resp = client.get("/api/system-monitor/integrations", headers=auth_headers)

        assert resp.status_code == 200
        assert {row["integration_type"] for row in resp.json()} == {"CS_INQUIRY"}
