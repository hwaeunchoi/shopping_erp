"""
tests/integration/test_api_settings.py
--------------------------------------------
api/routers/settings.py 통합 테스트: 사용자/역할/권한/API Credential/
시스템설정. 전체가 SETTINGS_MANAGE 권한으로 보호된다.
"""


class TestUsers:
    def test_requires_authentication(self, client, seed_data):
        resp = client.get("/api/settings/users")
        assert resp.status_code == 401

    def test_create_and_list_users(self, client, auth_headers, seed_data):
        create = client.post(
            "/api/settings/users",
            json={"username": "newuser", "password": "pw12345", "name": "새사용자", "role_id": 1},
            headers=auth_headers,
        )
        assert create.status_code == 201
        assert "password" not in create.json()

        listing = client.get("/api/settings/users", headers=auth_headers)
        assert any(u["username"] == "newuser" for u in listing.json())

    def test_update_user_deactivates(self, client, auth_headers, seed_data):
        create = client.post(
            "/api/settings/users",
            json={"username": "todeactivate", "password": "pw12345", "name": "비활성대상", "role_id": 1},
            headers=auth_headers,
        )
        user_id = create.json()["id"]

        update = client.patch(f"/api/settings/users/{user_id}", json={"is_active": False}, headers=auth_headers)

        assert update.status_code == 200
        assert update.json()["is_active"] is False

    def test_update_missing_user_returns_404(self, client, auth_headers, seed_data):
        resp = client.patch("/api/settings/users/999999", json={"is_active": False}, headers=auth_headers)
        assert resp.status_code == 404


class TestRolesAndPermissions:
    def test_list_permissions_returns_seeded_codes(self, client, auth_headers, seed_data):
        resp = client.get("/api/settings/permissions", headers=auth_headers)
        assert resp.status_code == 200
        codes = {p["code"] for p in resp.json()}
        assert "SETTINGS_MANAGE" in codes
        assert "ORDER_VIEW" in codes

    def test_create_role_and_set_permissions(self, client, auth_headers, seed_data):
        create = client.post(
            "/api/settings/roles", json={"name": "커스텀역할", "description": "테스트용"}, headers=auth_headers
        )
        assert create.status_code == 201
        role_id = create.json()["id"]

        set_perms = client.patch(
            f"/api/settings/roles/{role_id}/permissions",
            json={"permission_codes": ["ORDER_VIEW", "AD_MANAGE"]},
            headers=auth_headers,
        )
        assert set_perms.status_code == 200
        assert set(set_perms.json()["permission_codes"]) == {"ORDER_VIEW", "AD_MANAGE"}

        get_perms = client.get(f"/api/settings/roles/{role_id}/permissions", headers=auth_headers)
        assert set(get_perms.json()["permission_codes"]) == {"ORDER_VIEW", "AD_MANAGE"}

    def test_update_missing_role_returns_404(self, client, auth_headers, seed_data):
        resp = client.patch("/api/settings/roles/999999", json={"name": "x"}, headers=auth_headers)
        assert resp.status_code == 404


class TestApiCredentials:
    def test_upsert_and_list_masked(self, client, auth_headers, seed_data):
        upsert = client.post(
            "/api/settings/api-credentials",
            json={
                "owner_type": "PLATFORM",
                "owner_id": seed_data["platform_id"],
                "key_name": "client_secret",
                "plain_value": "super-secret-value-9999",
            },
            headers=auth_headers,
        )
        assert upsert.status_code == 200
        assert "super-secret-value-9999" not in upsert.json()["masked_value"]
        assert upsert.json()["masked_value"].endswith("9999")

        listing = client.get(
            f"/api/settings/api-credentials?owner_type=PLATFORM&owner_id={seed_data['platform_id']}",
            headers=auth_headers,
        )
        assert listing.status_code == 200
        assert all("super-secret-value-9999" not in c["masked_value"] for c in listing.json())

    def test_delete_credential(self, client, auth_headers, seed_data):
        upsert = client.post(
            "/api/settings/api-credentials",
            json={
                "owner_type": "PLATFORM",
                "owner_id": seed_data["platform_id"],
                "key_name": "client_id",
                "plain_value": "abc123",
            },
            headers=auth_headers,
        )
        credential_id = upsert.json()["id"]

        delete = client.delete(f"/api/settings/api-credentials/{credential_id}", headers=auth_headers)
        assert delete.status_code == 204

    def test_delete_missing_credential_returns_404(self, client, auth_headers, seed_data):
        resp = client.delete("/api/settings/api-credentials/999999", headers=auth_headers)
        assert resp.status_code == 404


class TestSystemSettings:
    def test_upsert_and_list(self, client, auth_headers, seed_data):
        upsert = client.patch(
            "/api/settings/system",
            json={"category": "BACKUP", "key": "retention_days", "value": "45"},
            headers=auth_headers,
        )
        assert upsert.status_code == 200
        assert upsert.json()["value"] == "45"

        listing = client.get("/api/settings/system?category=BACKUP", headers=auth_headers)
        assert any(s["key"] == "retention_days" and s["value"] == "45" for s in listing.json())


class TestPermissionEnforcement:
    def test_viewer_role_without_settings_manage_is_forbidden(self, client, api_session_factory, seed_data):
        from core.security import hash_password
        from models.user import Permission, Role, RolePermission, User

        db = api_session_factory()
        try:
            viewer_role = Role(name="ViewerOnly")
            db.add(viewer_role)
            db.flush()
            order_view = Permission(code="ORDER_VIEW_2", name="주문조회2")
            db.add(order_view)
            db.flush()
            db.add(RolePermission(role_id=viewer_role.id, permission_id=order_view.id))
            viewer = User(
                username="viewer1",
                password_hash=hash_password("pw12345"),
                name="뷰어",
                role_id=viewer_role.id,
                is_active=True,
            )
            db.add(viewer)
            db.commit()
        finally:
            db.close()

        login = client.post("/api/auth/login", data={"username": "viewer1", "password": "pw12345"})
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        resp = client.get("/api/settings/users", headers=headers)
        assert resp.status_code == 403
