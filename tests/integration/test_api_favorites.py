"""
tests/integration/test_api_favorites.py
----------------------------------------------
api/routers/favorites.py 통합 테스트. UI 와이어프레임 v1.1 5장(즐겨찾기).
"""


class TestFavorites:
    def test_requires_authentication(self, client, seed_data):
        resp = client.get("/api/favorites")
        assert resp.status_code == 401

    def test_toggle_adds_then_removes(self, client, auth_headers, seed_data):
        resp1 = client.post(
            "/api/favorites/toggle", json={"target_type": "PRODUCT", "target_id": 1}, headers=auth_headers
        )
        assert resp1.status_code == 200
        assert resp1.json()["is_favorited"] is True

        list_resp = client.get("/api/favorites", headers=auth_headers)
        assert len(list_resp.json()) == 1

        resp2 = client.post(
            "/api/favorites/toggle", json={"target_type": "PRODUCT", "target_id": 1}, headers=auth_headers
        )
        assert resp2.status_code == 200
        assert resp2.json()["is_favorited"] is False

        list_resp2 = client.get("/api/favorites", headers=auth_headers)
        assert len(list_resp2.json()) == 0

    def test_list_filters_by_target_type(self, client, auth_headers, seed_data):
        client.post("/api/favorites/toggle", json={"target_type": "PRODUCT", "target_id": 1}, headers=auth_headers)
        client.post("/api/favorites/toggle", json={"target_type": "REPORT", "target_id": 2}, headers=auth_headers)

        resp = client.get("/api/favorites?target_type=PRODUCT", headers=auth_headers)
        body = resp.json()
        assert len(body) == 1
        assert body[0]["target_type"] == "PRODUCT"
