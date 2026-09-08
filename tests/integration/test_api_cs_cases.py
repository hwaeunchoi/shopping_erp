"""
tests/integration/test_api_cs_cases.py
------------------------------------------
api/routers/cs_cases.py 통합 테스트 - CS(고객문의) 케이스 API 계약.
항목별 업무규칙(상태전이/동시성/중복감지)은 이미
tests/unit/test_cs_case_service.py가 SQLite로 상세히 검증했다 - 여기서는
그 서비스가 실제 라우터·인증·권한(CS_VIEW/CS_MANAGE/CS_ASSIGN/CS_CLOSE/
CS_PII_DETAIL)·DB 세션 의존성 주입과 올바르게 연결됐는지(API 계약)만
확인한다.

합성(테스트용) 문의 본문/수취인 정보만 사용한다 - 실제 고객 개인정보는
어디에도 쓰지 않는다.
"""

import uuid
from datetime import datetime, timezone

from core.security import hash_password
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


def _seed_order(api_session_factory, seed_data, receiver_phone="010-1234-5678") -> int:
    from models.order import Order

    db = api_session_factory()
    try:
        order = Order(
            platform_id=seed_data["platform_id"],
            platform_order_no=f"CS-API-{uuid.uuid4().hex[:8]}",
            status="NEW",
            order_date=datetime.now(timezone.utc),
            total_amount=10000,
            receiver_name="홍길동",
            receiver_phone=receiver_phone,
            receiver_address="서울시 강남구 테헤란로 1",
        )
        db.add(order)
        db.commit()
        return order.id
    finally:
        db.close()


class TestPermission:
    def test_requires_authentication(self, client):
        resp = client.get("/api/cs-cases")
        assert resp.status_code == 401

    def test_view_only_user_cannot_create(self, client, auth_headers, seed_data, api_session_factory):
        creds = _make_limited_user(api_session_factory, ["CS_VIEW"], "cs-viewer-1")
        headers = _login_headers(client, creds)

        resp = client.post("/api/cs-cases", json={"inquiry_type": "ETC", "customer_message": "문의"}, headers=headers)

        assert resp.status_code == 403

    def test_manage_user_cannot_assign(self, client, auth_headers, seed_data, api_session_factory):
        create = client.post(
            "/api/cs-cases", json={"inquiry_type": "ETC", "customer_message": "문의"}, headers=auth_headers
        ).json()
        creds = _make_limited_user(api_session_factory, ["CS_VIEW", "CS_MANAGE"], "cs-manager-1")
        headers = _login_headers(client, creds)

        resp = client.post(
            f"/api/cs-cases/{create['case']['id']}/assign",
            json={"assignee_id": 1, "expected_assignee_id": None},
            headers=headers,
        )

        assert resp.status_code == 403

    def test_manage_user_cannot_close(self, client, auth_headers, seed_data, api_session_factory):
        creds = _make_limited_user(api_session_factory, ["CS_VIEW", "CS_MANAGE"], "cs-manager-2")
        headers = _login_headers(client, creds)
        create = client.post(
            "/api/cs-cases", json={"inquiry_type": "ETC", "customer_message": "문의"}, headers=auth_headers
        ).json()

        resp = client.post(
            f"/api/cs-cases/{create['case']['id']}/close", json={"expected_status": "RESOLVED"}, headers=headers
        )

        assert resp.status_code == 403


class TestCreateCase:
    def test_happy_path(self, client, auth_headers, seed_data):
        resp = client.post(
            "/api/cs-cases",
            json={"inquiry_type": "DELIVERY", "customer_message": "배송이 늦어요", "priority": "HIGH"},
            headers=auth_headers,
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["case"]["status"] == "OPEN"
        assert body["case"]["priority"] == "HIGH"
        assert body["duplicate_of_case_id"] is None

    def test_cannot_specify_status_field(self, client, auth_headers, seed_data):
        """요청 스키마 자체가 status 필드를 받지 않는다 - 보내도 무시된다(CLOSED
        직접 생성을 스키마 레벨에서 차단)."""
        resp = client.post(
            "/api/cs-cases",
            json={"inquiry_type": "ETC", "customer_message": "문의", "status": "CLOSED"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        assert resp.json()["case"]["status"] == "OPEN"

    def test_unknown_inquiry_type_returns_400(self, client, auth_headers, seed_data):
        resp = client.post(
            "/api/cs-cases", json={"inquiry_type": "NOT_A_TYPE", "customer_message": "문의"}, headers=auth_headers
        )
        assert resp.status_code == 400

    def test_links_order_and_reports_duplicate(self, client, auth_headers, seed_data, api_session_factory):
        order_id = _seed_order(api_session_factory, seed_data)

        first = client.post(
            "/api/cs-cases",
            json={"inquiry_type": "DELIVERY", "customer_message": "문의1", "order_id": order_id},
            headers=auth_headers,
        ).json()
        second = client.post(
            "/api/cs-cases",
            json={"inquiry_type": "DELIVERY", "customer_message": "문의2", "order_id": order_id},
            headers=auth_headers,
        ).json()

        assert first["case"]["order_id"] == order_id
        assert second["duplicate_of_case_id"] == first["case"]["id"]


class TestPiiMasking:
    def test_list_shows_masked_phone_for_pii_permission_holder_and_non_holder(
        self, client, auth_headers, seed_data, api_session_factory
    ):
        order_id = _seed_order(api_session_factory, seed_data, receiver_phone="010-9876-5432")
        client.post(
            "/api/cs-cases",
            json={"inquiry_type": "DELIVERY", "customer_message": "문의", "order_id": order_id},
            headers=auth_headers,
        )

        # auth_headers(seed_data의 admin)는 CS_PII_DETAIL을 포함한 전체 권한을 가진다.
        resp = client.get("/api/cs-cases", headers=auth_headers)
        assert resp.status_code == 200
        row = next(r for r in resp.json() if r["order_id"] == order_id)
        assert row["customer_phone_masked"] == "***-****-5432"
        assert row["customer_phone_full"] == "010-9876-5432"

        creds = _make_limited_user(api_session_factory, ["CS_VIEW"], "cs-no-pii")
        headers = _login_headers(client, creds)
        resp2 = client.get("/api/cs-cases", headers=headers)
        row2 = next(r for r in resp2.json() if r["order_id"] == order_id)
        assert row2["customer_phone_masked"] == "***-****-5432"
        assert row2["customer_phone_full"] is None
        assert row2["customer_address_full"] is None


class TestFullWorkflow:
    def test_assign_status_memo_reply_close_reopen(self, client, auth_headers, seed_data):
        create = client.post(
            "/api/cs-cases", json={"inquiry_type": "ETC", "customer_message": "문의"}, headers=auth_headers
        ).json()
        case_id = create["case"]["id"]

        assign_resp = client.post(
            f"/api/cs-cases/{case_id}/assign",
            json={"assignee_id": seed_data["admin_id"], "expected_assignee_id": None},
            headers=auth_headers,
        )
        assert assign_resp.status_code == 200
        assert assign_resp.json()["assignee_id"] == seed_data["admin_id"]

        status_resp = client.post(
            f"/api/cs-cases/{case_id}/status",
            json={"new_status": "IN_PROGRESS", "expected_status": "OPEN"},
            headers=auth_headers,
        )
        assert status_resp.status_code == 200 and status_resp.json()["status"] == "IN_PROGRESS"

        memo_resp = client.post(
            f"/api/cs-cases/{case_id}/memos", json={"content": "내부 메모입니다"}, headers=auth_headers
        )
        assert memo_resp.status_code == 201

        draft_resp = client.post(
            f"/api/cs-cases/{case_id}/reply-draft",
            json={"reply_draft": "고객님께 안내드립니다", "expected_status": "IN_PROGRESS"},
            headers=auth_headers,
        )
        assert draft_resp.status_code == 200
        assert draft_resp.json()["reply_draft"] == "고객님께 안내드립니다"

        memos = client.get(f"/api/cs-cases/{case_id}/memos", headers=auth_headers).json()
        assert len(memos) == 1
        assert memos[0]["content"] == "내부 메모입니다"

        resolve_resp = client.post(
            f"/api/cs-cases/{case_id}/status",
            json={"new_status": "RESOLVED", "expected_status": "IN_PROGRESS"},
            headers=auth_headers,
        )
        assert resolve_resp.status_code == 200

        close_resp = client.post(
            f"/api/cs-cases/{case_id}/close", json={"expected_status": "RESOLVED"}, headers=auth_headers
        )
        assert close_resp.status_code == 200 and close_resp.json()["status"] == "CLOSED"

        closed_edit_resp = client.post(
            f"/api/cs-cases/{case_id}/reply-draft",
            json={"reply_draft": "수정 시도", "expected_status": "CLOSED"},
            headers=auth_headers,
        )
        assert closed_edit_resp.status_code == 400

        reopen_resp = client.post(
            f"/api/cs-cases/{case_id}/reopen", json={"expected_status": "CLOSED"}, headers=auth_headers
        )
        assert reopen_resp.status_code == 200
        assert reopen_resp.json()["status"] == "OPEN"

        history = client.get(f"/api/cs-cases/{case_id}/history", headers=auth_headers).json()
        actions = [h["action"] for h in history]
        assert actions == [
            "CREATED",
            "ASSIGNED",
            "STATUS_CHANGE",
            "MEMO_ADDED",
            "REPLY_DRAFT_UPDATED",
            "STATUS_CHANGE",
            "STATUS_CHANGE",
            "REOPENED",
        ]

    def test_status_endpoint_rejects_closed_target(self, client, auth_headers, seed_data):
        create = client.post(
            "/api/cs-cases", json={"inquiry_type": "ETC", "customer_message": "문의"}, headers=auth_headers
        ).json()
        resp = client.post(
            f"/api/cs-cases/{create['case']['id']}/status",
            json={"new_status": "CLOSED", "expected_status": "OPEN"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_stale_expected_status_returns_409(self, client, auth_headers, seed_data):
        create = client.post(
            "/api/cs-cases", json={"inquiry_type": "ETC", "customer_message": "문의"}, headers=auth_headers
        ).json()
        case_id = create["case"]["id"]

        resp = client.post(
            f"/api/cs-cases/{case_id}/status",
            json={"new_status": "WAITING_CUSTOMER", "expected_status": "IN_PROGRESS"},
            headers=auth_headers,
        )
        assert resp.status_code == 409


class TestBulkOperations:
    def test_bulk_assign_partial_failure(self, client, auth_headers, seed_data):
        case1 = client.post(
            "/api/cs-cases", json={"inquiry_type": "ETC", "customer_message": "문의1"}, headers=auth_headers
        ).json()["case"]
        case2 = client.post(
            "/api/cs-cases", json={"inquiry_type": "ETC", "customer_message": "문의2"}, headers=auth_headers
        ).json()["case"]

        resp = client.post(
            "/api/cs-cases/bulk/assign",
            json={"case_ids": [case1["id"], case2["id"], 999999], "assignee_id": seed_data["admin_id"]},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        by_id = {r["case_id"]: r for r in resp.json()}
        assert by_id[case1["id"]]["outcome"] == "ACCEPTED"
        assert by_id[case2["id"]]["outcome"] == "ACCEPTED"
        assert by_id[999999]["outcome"] == "NOT_FOUND"

    def test_bulk_status_rejects_closed_target(self, client, auth_headers, seed_data):
        resp = client.post(
            "/api/cs-cases/bulk/status", json={"case_ids": [1], "new_status": "CLOSED"}, headers=auth_headers
        )
        assert resp.status_code == 400


class TestDashboardAndSync:
    def test_dashboard_summary(self, client, auth_headers, seed_data):
        client.post("/api/cs-cases", json={"inquiry_type": "ETC", "customer_message": "문의"}, headers=auth_headers)
        resp = client.get("/api/cs-cases/dashboard", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert "by_status" in body and "unassigned_count" in body and "overdue_count" in body

    def test_sync_disabled_by_default_makes_no_external_call(self, client, auth_headers, seed_data):
        resp = client.post(
            "/api/cs-cases/sync", json={"platform_id": seed_data["platform_id"], "days": 7}, headers=auth_headers
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "DISABLED"

    def test_sync_unknown_platform_returns_404(self, client, auth_headers, seed_data):
        resp = client.post("/api/cs-cases/sync", json={"platform_id": 999999, "days": 7}, headers=auth_headers)
        assert resp.status_code == 404


class TestReference:
    def test_reference_lists_inquiry_types_and_priorities(self, client, auth_headers, seed_data):
        resp = client.get("/api/cs-cases/meta/reference", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert "ETC" in body["inquiry_types"]
        assert "NORMAL" in body["priorities"]
