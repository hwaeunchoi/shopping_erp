"""
tests/integration/test_api_shipments.py
----------------------------------------------
api/routers/shipments.py 통합 테스트: CRUD, 상태변경, 필터/페이지네이션, RBAC.
"""

from datetime import datetime, timezone

from models.order import Order


def _create_order(api_session_factory, seed_data, order_no: str) -> int:
    db = api_session_factory()
    try:
        order = Order(
            platform_id=seed_data["platform_id"],
            platform_order_no=order_no,
            status="NEW",
            order_date=datetime.now(timezone.utc),
            total_amount=10000,
            discount_amount=0,
        )
        db.add(order)
        db.commit()
        return order.id
    finally:
        db.close()


class TestListAndGet:
    def test_list_empty_initially(self, client, auth_headers):
        resp = client.get("/api/shipments", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == {"items": [], "total": 0, "page": 1, "page_size": 20}

    def test_get_missing_returns_404(self, client, auth_headers):
        resp = client.get("/api/shipments/999999", headers=auth_headers)
        assert resp.status_code == 404

    def test_requires_authentication(self, client, seed_data):
        resp = client.get("/api/shipments")
        assert resp.status_code == 401


class TestCreate:
    def test_create_succeeds(self, client, auth_headers, api_session_factory, seed_data):
        order_id = _create_order(api_session_factory, seed_data, "SHIP-API-1")

        resp = client.post(
            "/api/shipments",
            json={"order_id": order_id, "carrier": "CJ대한통운", "tracking_no": "TRK-1"},
            headers=auth_headers,
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["order_ids"] == [order_id]  # 합포장 지원으로 단일 id가 아니라 목록
        assert body["status"] == "READY"

    def test_create_unknown_order_returns_404(self, client, auth_headers):
        resp = client.post("/api/shipments", json={"order_id": 999999, "carrier": "CJ대한통운"}, headers=auth_headers)
        assert resp.status_code == 404

    def test_create_duplicate_returns_409(self, client, auth_headers, api_session_factory, seed_data):
        order_id = _create_order(api_session_factory, seed_data, "SHIP-API-2")
        client.post("/api/shipments", json={"order_id": order_id}, headers=auth_headers)

        resp = client.post("/api/shipments", json={"order_id": order_id}, headers=auth_headers)

        assert resp.status_code == 409


class TestUpdateAndStatus:
    def test_update_info(self, client, auth_headers, api_session_factory, seed_data):
        order_id = _create_order(api_session_factory, seed_data, "SHIP-API-3")
        created = client.post("/api/shipments", json={"order_id": order_id}, headers=auth_headers).json()

        resp = client.patch(
            f"/api/shipments/{created['id']}",
            json={"carrier": "롯데택배", "tracking_no": "TRK-9"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert resp.json()["carrier"] == "롯데택배"

    def test_change_status_syncs_order(self, client, auth_headers, api_session_factory, seed_data):
        order_id = _create_order(api_session_factory, seed_data, "SHIP-API-4")
        created = client.post("/api/shipments", json={"order_id": order_id}, headers=auth_headers).json()

        resp = client.patch(
            f"/api/shipments/{created['id']}/status",
            json={"status": "SHIPPING", "warehouse_id": seed_data["warehouse_id"]},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == "SHIPPING"

        order_resp = client.get(f"/api/orders/{order_id}", headers=auth_headers)
        assert order_resp.json()["status"] == "SHIPPING"

    def test_change_status_invalid_value_returns_422(self, client, auth_headers, api_session_factory, seed_data):
        order_id = _create_order(api_session_factory, seed_data, "SHIP-API-5")
        created = client.post("/api/shipments", json={"order_id": order_id}, headers=auth_headers).json()

        resp = client.patch(f"/api/shipments/{created['id']}/status", json={"status": "UNKNOWN"}, headers=auth_headers)

        assert resp.status_code == 422

    def test_change_status_missing_shipment_returns_404(self, client, auth_headers):
        resp = client.patch("/api/shipments/999999/status", json={"status": "SHIPPING"}, headers=auth_headers)
        assert resp.status_code == 404


class TestFilterAndPagination:
    def test_filters_by_status_and_search(self, client, auth_headers, api_session_factory, seed_data):
        o1 = _create_order(api_session_factory, seed_data, "SHIP-API-F1")
        o2 = _create_order(api_session_factory, seed_data, "SHIP-API-F2")
        client.post("/api/shipments", json={"order_id": o1, "tracking_no": "ABC-100"}, headers=auth_headers)
        client.post("/api/shipments", json={"order_id": o2, "tracking_no": "XYZ-200"}, headers=auth_headers)

        resp = client.get("/api/shipments?search=ABC", headers=auth_headers)

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["tracking_no"] == "ABC-100"

    def test_pagination(self, client, auth_headers, api_session_factory, seed_data):
        for i in range(3):
            oid = _create_order(api_session_factory, seed_data, f"SHIP-API-P{i}")
            client.post("/api/shipments", json={"order_id": oid}, headers=auth_headers)

        resp = client.get("/api/shipments?page=1&page_size=2", headers=auth_headers)

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3
        assert len(body["items"]) == 2
        assert body["page"] == 1
        assert body["page_size"] == 2


class TestSubmitToChannel:
    """POST /{shipment_id}/submit - 비동기 outbox 진입점(enqueue만 함, 채널 호출 없음).

    실제 채널 HTTP 호출(execute_command)은 scheduler.jobs.outbox_dispatch_job이
    수행하며 여기서는 검증하지 않는다 - services/tests/unit/test_shipment_dispatch_service.py
    에서 MockTransport 기반으로 이미 검증했다. 이 통합 테스트는 API 계약(202/404/400/
    idempotent/RBAC)만 확인한다."""

    @staticmethod
    def _create_ready_shipment(client, auth_headers, api_session_factory, seed_data, order_no, tracking_no="TRK-SUB-1"):
        order_id = _create_order(api_session_factory, seed_data, order_no)
        created = client.post(
            "/api/shipments",
            json={"order_id": order_id, "carrier": "CJ_LOGISTICS", "tracking_no": tracking_no},
            headers=auth_headers,
        ).json()
        return created["id"]

    def test_submit_returns_202_with_pending_command(self, client, auth_headers, api_session_factory, seed_data):
        shipment_id = self._create_ready_shipment(client, auth_headers, api_session_factory, seed_data, "SHIP-SUB-1")

        resp = client.post(f"/api/shipments/{shipment_id}/submit", headers=auth_headers)

        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "PENDING"
        assert body["already_processed"] is False
        assert isinstance(body["command_id"], int)

    def test_submit_missing_shipment_returns_404(self, client, auth_headers):
        resp = client.post("/api/shipments/999999/submit", headers=auth_headers)
        assert resp.status_code == 404

    def test_submit_without_carrier_or_tracking_returns_400(self, client, auth_headers, api_session_factory, seed_data):
        order_id = _create_order(api_session_factory, seed_data, "SHIP-SUB-2")
        created = client.post("/api/shipments", json={"order_id": order_id}, headers=auth_headers).json()

        resp = client.post(f"/api/shipments/{created['id']}/submit", headers=auth_headers)

        assert resp.status_code == 400

    def test_resubmitting_returns_same_command_idempotently(self, client, auth_headers, api_session_factory, seed_data):
        shipment_id = self._create_ready_shipment(client, auth_headers, api_session_factory, seed_data, "SHIP-SUB-3")

        first = client.post(f"/api/shipments/{shipment_id}/submit", headers=auth_headers)
        second = client.post(f"/api/shipments/{shipment_id}/submit", headers=auth_headers)

        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["command_id"] == second.json()["command_id"]

    def test_requires_shipment_view_permission(self, client, api_session_factory, seed_data):
        from core.security import hash_password
        from models.user import Permission, Role, RolePermission, User

        db = api_session_factory()
        try:
            role = Role(name="NoShipmentSubmit")
            db.add(role)
            db.flush()
            other_perm = db.query(Permission).filter_by(code="ORDER_VIEW").first()
            db.add(RolePermission(role_id=role.id, permission_id=other_perm.id))
            db.add(
                User(
                    username="noshipmentsubmit",
                    password_hash=hash_password("pw123456"),
                    name="무권한",
                    role_id=role.id,
                    is_active=True,
                )
            )
            db.commit()
        finally:
            db.close()

        login = client.post("/api/auth/login", data={"username": "noshipmentsubmit", "password": "pw123456"})
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        resp = client.post("/api/shipments/1/submit", headers=headers)
        assert resp.status_code == 403


class TestSubmitCommandStatus:
    """GET /commands/{command_id} - 화면이 폴링하는 명령 상태 조회."""

    def test_returns_command_status(self, client, auth_headers, api_session_factory, seed_data):
        order_id = _create_order(api_session_factory, seed_data, "SHIP-CMD-1")
        created = client.post(
            "/api/shipments",
            json={"order_id": order_id, "carrier": "CJ_LOGISTICS", "tracking_no": "TRK-CMD-1"},
            headers=auth_headers,
        ).json()
        command_id = client.post(f"/api/shipments/{created['id']}/submit", headers=auth_headers).json()["command_id"]

        resp = client.get(f"/api/shipments/commands/{command_id}", headers=auth_headers)

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == command_id
        assert body["status"] == "PENDING"
        assert body["attempt_count"] == 0

    def test_missing_command_returns_404(self, client, auth_headers):
        resp = client.get("/api/shipments/commands/999999", headers=auth_headers)
        assert resp.status_code == 404

    def test_requires_authentication(self, client, seed_data):
        resp = client.get("/api/shipments/commands/1")
        assert resp.status_code == 401


class TestRBAC:
    def test_requires_shipment_view_permission(self, client, api_session_factory, seed_data):
        from core.security import hash_password
        from models.user import Permission, Role, RolePermission, User

        db = api_session_factory()
        try:
            role = Role(name="NoShipment")
            db.add(role)
            db.flush()
            other_perm = db.query(Permission).filter_by(code="ORDER_VIEW").first()
            db.add(RolePermission(role_id=role.id, permission_id=other_perm.id))
            db.add(
                User(
                    username="noshipment",
                    password_hash=hash_password("pw123456"),
                    name="무권한",
                    role_id=role.id,
                    is_active=True,
                )
            )
            db.commit()
        finally:
            db.close()

        login = client.post("/api/auth/login", data={"username": "noshipment", "password": "pw123456"})
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        resp = client.get("/api/shipments", headers=headers)
        assert resp.status_code == 403
