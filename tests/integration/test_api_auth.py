"""
tests/integration/test_api_auth.py
----------------------------------------
api/routers/auth.py 통합 테스트 (실제 FastAPI 앱 + JWT 발급/검증).
"""

from models.system import SystemLog


class TestLogin:
    def test_wrong_password_returns_401(self, client, seed_data):
        resp = client.post("/api/auth/login", data={"username": "admin", "password": "wrong-password"})
        assert resp.status_code == 401

    def test_unknown_user_returns_401(self, client, seed_data):
        resp = client.post("/api/auth/login", data={"username": "nobody", "password": "x"})
        assert resp.status_code == 401

    def test_correct_login_returns_token(self, client, seed_data):
        resp = client.post("/api/auth/login", data={"username": "admin", "password": "ChangeMe!123"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["token_type"] == "bearer"
        assert body["access_token"]


class TestLoginActivityLog:
    """SRS FR-USER-04: 로그인 성공/실패가 system_logs(USER_ACTIVITY)에 남는지 검증."""

    def test_success_writes_info_log_with_user_id(self, client, seed_data, api_session_factory):
        resp = client.post("/api/auth/login", data={"username": "admin", "password": "ChangeMe!123"})
        assert resp.status_code == 200

        db = api_session_factory()
        try:
            logs = db.query(SystemLog).filter_by(log_type="USER_ACTIVITY").all()
            assert len(logs) == 1
            assert logs[0].level == "INFO"
            assert "로그인 성공" in logs[0].message
            assert logs[0].user_id == seed_data["admin_id"]
        finally:
            db.close()

    def test_wrong_password_writes_warn_log(self, client, seed_data, api_session_factory):
        resp = client.post("/api/auth/login", data={"username": "admin", "password": "wrong-password"})
        assert resp.status_code == 401

        db = api_session_factory()
        try:
            logs = db.query(SystemLog).filter_by(log_type="USER_ACTIVITY").all()
            assert len(logs) == 1
            assert logs[0].level == "WARN"
            assert "로그인 실패" in logs[0].message
            assert logs[0].user_id == seed_data["admin_id"]
        finally:
            db.close()

    def test_unknown_user_writes_warn_log_without_user_id(self, client, seed_data, api_session_factory):
        resp = client.post("/api/auth/login", data={"username": "nobody", "password": "x"})
        assert resp.status_code == 401

        db = api_session_factory()
        try:
            logs = db.query(SystemLog).filter_by(log_type="USER_ACTIVITY").all()
            assert len(logs) == 1
            assert logs[0].level == "WARN"
            assert logs[0].user_id is None
        finally:
            db.close()


class TestMe:
    def test_requires_token(self, client, seed_data):
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401

    def test_rejects_garbage_token(self, client, seed_data):
        resp = client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
        assert resp.status_code == 401

    def test_returns_profile_for_valid_token(self, client, auth_headers):
        resp = client.get("/api/auth/me", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["username"] == "admin"

    def test_profile_includes_role_permission_codes(self, client, auth_headers):
        """프론트엔드 role 기반 UI 제어(사이드바 메뉴 숨김 등)를 위해 permissions를 내려준다."""
        resp = client.get("/api/auth/me", headers=auth_headers)
        assert resp.status_code == 200
        assert "SETTINGS_MANAGE" in resp.json()["permissions"]
        assert "ORDER_VIEW" in resp.json()["permissions"]


class TestUpdateTheme:
    """UI 와이어프레임 v1.1 1장: 다크모드 토글 저장 API."""

    def test_requires_authentication(self, client, seed_data):
        resp = client.patch("/api/auth/me/theme", json={"theme_preference": "DARK"})
        assert resp.status_code == 401

    def test_updates_theme_preference(self, client, auth_headers, seed_data):
        resp = client.patch("/api/auth/me/theme", json={"theme_preference": "DARK"}, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["theme_preference"] == "DARK"

        me_resp = client.get("/api/auth/me", headers=auth_headers)
        assert me_resp.json()["theme_preference"] == "DARK"

    def test_rejects_invalid_value(self, client, auth_headers, seed_data):
        resp = client.patch("/api/auth/me/theme", json={"theme_preference": "PURPLE"}, headers=auth_headers)
        assert resp.status_code == 422
