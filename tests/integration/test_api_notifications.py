"""
tests/integration/test_api_notifications.py
--------------------------------------------------
api/routers/notifications.py 통합 테스트. UI v1.0 알림센터.
"""

from datetime import datetime, timezone

from models.system import Notification


class TestNotifications:
    def test_requires_authentication(self, client, seed_data):
        resp = client.get("/api/notifications")
        assert resp.status_code == 401

    def test_list_and_unread_count(self, client, auth_headers, seed_data, api_session_factory):
        db = api_session_factory()
        try:
            db.add(
                Notification(
                    type="API_FAILURE",
                    severity="CRITICAL",
                    message="수집 실패",
                    is_read=False,
                    created_at=datetime.now(timezone.utc),
                )
            )
            db.add(
                Notification(
                    type="RETURN_RATE",
                    severity="WARNING",
                    message="반품 급증",
                    is_read=True,
                    created_at=datetime.now(timezone.utc),
                )
            )
            db.commit()
        finally:
            db.close()

        list_resp = client.get("/api/notifications", headers=auth_headers)
        assert list_resp.status_code == 200
        assert len(list_resp.json()) == 2

        unread_resp = client.get("/api/notifications?unread_only=true", headers=auth_headers)
        assert len(unread_resp.json()) == 1

        count_resp = client.get("/api/notifications/unread-count", headers=auth_headers)
        assert count_resp.json()["unread_count"] == 1

    def test_mark_read_and_mark_all_read(self, client, auth_headers, seed_data, api_session_factory):
        db = api_session_factory()
        try:
            n1 = Notification(
                type="A", severity="INFO", message="1", is_read=False, created_at=datetime.now(timezone.utc)
            )
            n2 = Notification(
                type="B", severity="INFO", message="2", is_read=False, created_at=datetime.now(timezone.utc)
            )
            db.add(n1)
            db.add(n2)
            db.commit()
            n1_id = n1.id
        finally:
            db.close()

        read_resp = client.patch(f"/api/notifications/{n1_id}/read", headers=auth_headers)
        assert read_resp.status_code == 200
        assert read_resp.json()["is_read"] is True

        mark_all_resp = client.post("/api/notifications/read-all", headers=auth_headers)
        assert mark_all_resp.status_code == 200
        assert mark_all_resp.json()["marked_count"] == 1  # n2만 남아있었음

        count_resp = client.get("/api/notifications/unread-count", headers=auth_headers)
        assert count_resp.json()["unread_count"] == 0

    def test_mark_read_missing_notification_returns_404(self, client, auth_headers, seed_data):
        resp = client.patch("/api/notifications/999999/read", headers=auth_headers)
        assert resp.status_code == 404
