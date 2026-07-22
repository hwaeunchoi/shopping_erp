"""
tests/integration/test_api_system_monitor.py
--------------------------------------------------
api/routers/system_monitor.py 통합 테스트. UI 와이어프레임 v1.1 4장
(연동상태/작업이력/시스템상태 탭).
"""

from datetime import datetime, timezone

from models.extra import IntegrationStatus, TaskExecutionHistory


class TestIntegrations:
    def test_requires_authentication(self, client, seed_data):
        resp = client.get("/api/system-monitor/integrations")
        assert resp.status_code == 401

    def test_lists_integration_status(self, client, auth_headers, seed_data, api_session_factory):
        db = api_session_factory()
        try:
            db.add(
                IntegrationStatus(
                    integration_type="MALL",
                    integration_code="coupang",
                    status="NORMAL",
                    updated_at=datetime.now(timezone.utc),
                )
            )
            db.commit()
        finally:
            db.close()

        resp = client.get("/api/system-monitor/integrations", headers=auth_headers)
        assert resp.status_code == 200
        assert any(i["integration_code"] == "coupang" for i in resp.json())


class TestTaskHistory:
    def test_filters_by_status(self, client, auth_headers, seed_data, api_session_factory):
        db = api_session_factory()
        try:
            now = datetime.now(timezone.utc)
            db.add(TaskExecutionHistory(task_type="BACKUP", trigger_type="SCHEDULE", status="SUCCESS", started_at=now))
            db.add(TaskExecutionHistory(task_type="BACKUP", trigger_type="SCHEDULE", status="FAILED", started_at=now))
            db.commit()
        finally:
            db.close()

        resp = client.get("/api/system-monitor/task-history?status_filter=FAILED", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["status"] == "FAILED"


class TestSystemStatus:
    def test_returns_status(self, client, auth_headers, seed_data):
        resp = client.get("/api/system-monitor/status", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["db_size_bytes"] >= 0
        assert body["latest_backup"] is None
