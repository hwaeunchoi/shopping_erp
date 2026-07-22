"""
tests/integration/test_api_alert_rules.py
--------------------------------------------------
api/routers/alert_rules.py 통합 테스트. UI v1.0 알림센터(사용자 정의 알림 규칙).
"""


class TestAlertRules:
    def test_requires_authentication(self, client, seed_data):
        resp = client.get("/api/alert-rules")
        assert resp.status_code == 401

    def test_create_list_update_delete(self, client, auth_headers, seed_data):
        create_resp = client.post(
            "/api/alert-rules",
            json={"name": "반품율 경고", "metric": "RETURN_RATE", "operator": "GT", "threshold_value": 20.0},
            headers=auth_headers,
        )
        assert create_resp.status_code == 201
        rule_id = create_resp.json()["id"]
        assert create_resp.json()["is_enabled"] is True

        list_resp = client.get("/api/alert-rules", headers=auth_headers)
        assert any(r["id"] == rule_id for r in list_resp.json())

        update_resp = client.patch(f"/api/alert-rules/{rule_id}", json={"is_enabled": False}, headers=auth_headers)
        assert update_resp.status_code == 200
        assert update_resp.json()["is_enabled"] is False

        delete_resp = client.delete(f"/api/alert-rules/{rule_id}", headers=auth_headers)
        assert delete_resp.status_code == 204

        list_after = client.get("/api/alert-rules", headers=auth_headers)
        assert not any(r["id"] == rule_id for r in list_after.json())

    def test_update_missing_rule_returns_404(self, client, auth_headers, seed_data):
        resp = client.patch("/api/alert-rules/999999", json={"is_enabled": False}, headers=auth_headers)
        assert resp.status_code == 404

    def test_delete_missing_rule_returns_404(self, client, auth_headers, seed_data):
        resp = client.delete("/api/alert-rules/999999", headers=auth_headers)
        assert resp.status_code == 404
