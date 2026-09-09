"""
tests/integration/test_api_operations.py
------------------------------------------------
api/routers/operations.py 통합 테스트 - 상용 ERP 확장(6단계) 운영 대시보드/
통합 실패 작업함 API 계약. 통계·라우팅 상세 로직은 이미
tests/unit/test_operations_dashboard_service.py / test_operations_retry_service.py가
검증했다 - 여기서는 그 서비스가 실제 라우터·인증·권한(DASHBOARD_VIEW/
SYSTEM_MONITOR_VIEW/OPERATIONS_RETRY/OPERATIONS_UNKNOWN_RESOLVE)·DB 세션
의존성 주입과 올바르게 연결됐는지(API 계약)만 확인한다.
"""

import uuid

from core.security import hash_password
from models.integration_sync import ExternalCommand
from models.user import Permission, Role, RolePermission, User


def _make_limited_user(api_session_factory, permission_codes: list[str], username: str) -> dict:
    db = api_session_factory()
    try:
        role = Role(name=f"LimitedRole-{uuid.uuid4().hex[:8]}")
        db.add(role)
        db.flush()
        for code in permission_codes:
            perm = db.query(Permission).filter_by(code=code).one_or_none()
            if perm is None:
                perm = Permission(code=code, name=code)
                db.add(perm)
                db.flush()
            db.add(RolePermission(role_id=role.id, permission_id=perm.id))
        user = User(
            username=username,
            password_hash=hash_password("Pw123456!"),
            name="제한사용자",
            role_id=role.id,
            is_active=True,
        )
        db.add(user)
        db.commit()
        return {"username": username, "password": "Pw123456!"}
    finally:
        db.close()


def _login_headers(client, creds: dict) -> dict:
    resp = client.post("/api/auth/login", data=creds)
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _make_command(api_session_factory, platform_id: int, status: str, command_type: str = "SHIPMENT_SUBMIT") -> int:
    db = api_session_factory()
    try:
        cmd = ExternalCommand(
            idempotency_key=f"KEY-{uuid.uuid4().hex}",
            command_type=command_type,
            platform_id=platform_id,
            target_type="SHIPMENT",
            target_id=1,
            status=status,
            trace_id=uuid.uuid4().hex,
        )
        db.add(cmd)
        db.commit()
        return cmd.id
    finally:
        db.close()


class TestPermission:
    def test_requires_authentication(self, client):
        resp = client.get("/api/operations/summary")
        assert resp.status_code == 401

    def test_view_only_user_can_read_summary(self, client, api_session_factory, seed_data):
        creds = _make_limited_user(api_session_factory, ["DASHBOARD_VIEW"], "ops-viewer-1")
        headers = _login_headers(client, creds)
        resp = client.get("/api/operations/summary", headers=headers)
        assert resp.status_code == 200

    def test_view_only_user_cannot_bulk_retry(self, client, api_session_factory, seed_data):
        creds = _make_limited_user(api_session_factory, ["DASHBOARD_VIEW"], "ops-viewer-2")
        headers = _login_headers(client, creds)
        command_id = _make_command(api_session_factory, seed_data["platform_id"], "FAILED")

        resp = client.post("/api/operations/failures/bulk-retry", json={"command_ids": [command_id]}, headers=headers)
        assert resp.status_code == 403

    def test_view_only_user_cannot_resolve_unknown(self, client, api_session_factory, seed_data):
        creds = _make_limited_user(api_session_factory, ["DASHBOARD_VIEW"], "ops-viewer-3")
        headers = _login_headers(client, creds)
        command_id = _make_command(api_session_factory, seed_data["platform_id"], "UNKNOWN")

        resp = client.post(
            f"/api/operations/failures/{command_id}/resolve-unknown",
            json={"resolution": "CONFIRMED_SUCCESS", "evidence_note": "채널에서 확인함"},
            headers=headers,
        )
        assert resp.status_code == 403

    def test_retry_permission_alone_does_not_grant_unknown_resolve(self, client, api_session_factory, seed_data):
        """재처리 권한과 UNKNOWN 해소 권한은 서로 다른 코드다 - 하나만 있으면
        다른 하나는 여전히 403이어야 한다."""
        creds = _make_limited_user(api_session_factory, ["DASHBOARD_VIEW", "OPERATIONS_RETRY"], "ops-retry-only")
        headers = _login_headers(client, creds)
        command_id = _make_command(api_session_factory, seed_data["platform_id"], "UNKNOWN")

        resp = client.post(
            f"/api/operations/failures/{command_id}/resolve-unknown",
            json={"resolution": "CONFIRMED_SUCCESS", "evidence_note": "채널에서 확인함"},
            headers=headers,
        )
        assert resp.status_code == 403

    def test_scheduler_jobs_requires_system_monitor_view(self, client, api_session_factory, seed_data):
        creds = _make_limited_user(api_session_factory, ["DASHBOARD_VIEW"], "ops-no-monitor")
        headers = _login_headers(client, creds)
        resp = client.get("/api/operations/scheduler-jobs", headers=headers)
        assert resp.status_code == 403


class TestSummaryAndTimeseries:
    def test_summary_reflects_real_db_state(self, client, auth_headers, api_session_factory, seed_data):
        _make_command(api_session_factory, seed_data["platform_id"], "FAILED")
        resp = client.get("/api/operations/summary", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["shipment_commands_by_status"]["FAILED"] == 1
        # FAILED도 분모에 포함된다(성공+실패 확정 건) - 성공 0/실패 1 -> 0%.
        assert body["success_rate"]["last_24h"]["rate_percent"] == 0.0
        assert body["success_rate"]["last_24h"]["total"] == 1

    def test_timeseries_returns_requested_number_of_days(self, client, auth_headers):
        resp = client.get("/api/operations/timeseries?days=5", headers=auth_headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 5

    def test_integrations_endpoint_ok(self, client, auth_headers):
        resp = client.get("/api/operations/integrations", headers=auth_headers)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


class TestFailuresList:
    def test_default_excludes_pending_and_success(self, client, auth_headers, api_session_factory, seed_data):
        pending_id = _make_command(api_session_factory, seed_data["platform_id"], "PENDING")
        failed_id = _make_command(api_session_factory, seed_data["platform_id"], "FAILED")

        resp = client.get("/api/operations/failures", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        ids = {row["id"] for row in body["items"]}
        assert failed_id in ids
        assert pending_id not in ids

    def test_explicit_status_filter_overrides_default(self, client, auth_headers, api_session_factory, seed_data):
        pending_id = _make_command(api_session_factory, seed_data["platform_id"], "PENDING")

        resp = client.get("/api/operations/failures?status_filter=PENDING", headers=auth_headers)
        assert resp.status_code == 200
        ids = {row["id"] for row in resp.json()["items"]}
        assert pending_id in ids

    def test_pagination_fields_present_and_limit_capped(self, client, auth_headers):
        resp = client.get("/api/operations/failures?limit=9999", headers=auth_headers)
        assert resp.status_code == 422  # limit 상한(200) 초과는 검증 오류.

    def test_stable_ordering_is_id_desc(self, client, auth_headers, api_session_factory, seed_data):
        first_id = _make_command(api_session_factory, seed_data["platform_id"], "FAILED")
        second_id = _make_command(api_session_factory, seed_data["platform_id"], "FAILED")

        resp = client.get("/api/operations/failures?limit=2", headers=auth_headers)
        ids = [row["id"] for row in resp.json()["items"]]
        assert ids.index(second_id) < ids.index(first_id)

    def test_no_pii_or_secret_fields_in_response(self, client, auth_headers, api_session_factory, seed_data):
        _make_command(api_session_factory, seed_data["platform_id"], "FAILED")
        resp = client.get("/api/operations/failures", headers=auth_headers)
        row = resp.json()["items"][0]
        forbidden_keys = {
            "phone",
            "address",
            "customer_message",
            "reply_draft",
            "authorization",
            "password",
            "credential",
            "access_key",
            "secret_key",
        }
        assert forbidden_keys.isdisjoint(row.keys())

    def test_detail_endpoint_404_for_missing(self, client, auth_headers):
        resp = client.get("/api/operations/failures/999999", headers=auth_headers)
        assert resp.status_code == 404

    def test_detail_endpoint_includes_unknown_actions(self, client, auth_headers, api_session_factory, seed_data):
        command_id = _make_command(api_session_factory, seed_data["platform_id"], "UNKNOWN")
        resp = client.get(f"/api/operations/failures/{command_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert set(resp.json()["available_unknown_actions"]) == {
            "CONFIRMED_NOT_SENT",
            "CONFIRMED_SUCCESS",
            "CONFIRMED_FAILED",
        }


class TestBulkRetryApi:
    def test_bulk_retry_happy_path(self, client, auth_headers, api_session_factory, seed_data):
        command_id = _make_command(api_session_factory, seed_data["platform_id"], "FAILED")
        resp = client.post(
            "/api/operations/failures/bulk-retry", json={"command_ids": [command_id]}, headers=auth_headers
        )
        assert resp.status_code == 200
        assert resp.json() == [{"command_id": command_id, "outcome": "RETRIED", "error_code": None}]

    def test_bulk_retry_blocks_unknown(self, client, auth_headers, api_session_factory, seed_data):
        command_id = _make_command(api_session_factory, seed_data["platform_id"], "UNKNOWN")
        resp = client.post(
            "/api/operations/failures/bulk-retry", json={"command_ids": [command_id]}, headers=auth_headers
        )
        assert resp.status_code == 200
        assert resp.json()[0]["outcome"] == "UNKNOWN_REQUIRES_RESOLUTION"

    def test_bulk_retry_rejects_empty_selection(self, client, auth_headers):
        resp = client.post("/api/operations/failures/bulk-retry", json={"command_ids": []}, headers=auth_headers)
        assert resp.status_code == 422  # Pydantic min_length=1

    def test_bulk_retry_over_limit_returns_400(self, client, auth_headers):
        resp = client.post(
            "/api/operations/failures/bulk-retry", json={"command_ids": list(range(1, 60))}, headers=auth_headers
        )
        assert resp.status_code == 400


class TestResolveUnknownApi:
    def test_resolve_unknown_happy_path(self, client, auth_headers, api_session_factory, seed_data):
        command_id = _make_command(api_session_factory, seed_data["platform_id"], "UNKNOWN")
        resp = client.post(
            f"/api/operations/failures/{command_id}/resolve-unknown",
            json={"resolution": "CONFIRMED_FAILED", "evidence_note": "채널 관리자 화면에서 반영되지 않았음을 확인함"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "FAILED"

    def test_resolve_unknown_rejects_short_evidence(self, client, auth_headers, api_session_factory, seed_data):
        command_id = _make_command(api_session_factory, seed_data["platform_id"], "UNKNOWN")
        resp = client.post(
            f"/api/operations/failures/{command_id}/resolve-unknown",
            json={"resolution": "CONFIRMED_FAILED", "evidence_note": "ok"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_resolve_unknown_missing_command_404(self, client, auth_headers):
        resp = client.post(
            "/api/operations/failures/999999/resolve-unknown",
            json={"resolution": "CONFIRMED_FAILED", "evidence_note": "충분히 긴 확인 근거 텍스트입니다"},
            headers=auth_headers,
        )
        assert resp.status_code == 404
