"""
tests/integration/test_api_recent_views.py
--------------------------------------------------
api/routers/recent_views.py 통합 테스트. UI 와이어프레임 v1.1 6장(최근 본 항목).
"""


class TestRecentViews:
    def test_requires_authentication(self, client, seed_data):
        resp = client.get("/api/recent-views")
        assert resp.status_code == 401

    def test_record_then_list(self, client, auth_headers, seed_data):
        resp = client.post("/api/recent-views", json={"target_type": "PRODUCT", "target_id": 5}, headers=auth_headers)
        assert resp.status_code == 200

        list_resp = client.get("/api/recent-views", headers=auth_headers)
        body = list_resp.json()
        assert len(body) == 1
        assert body[0]["target_type"] == "PRODUCT"
        assert body[0]["target_id"] == 5

    def test_revisit_same_target_does_not_duplicate(self, client, auth_headers, seed_data):
        client.post("/api/recent-views", json={"target_type": "ORDER", "target_id": 1}, headers=auth_headers)
        client.post("/api/recent-views", json={"target_type": "ORDER", "target_id": 1}, headers=auth_headers)

        list_resp = client.get("/api/recent-views", headers=auth_headers)
        assert len(list_resp.json()) == 1
