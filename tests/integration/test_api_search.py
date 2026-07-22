"""
tests/integration/test_api_search.py
----------------------------------------
api/routers/search.py 통합 테스트. UI 와이어프레임 v1.1 1장(글로벌 통합검색).
"""

from models.customer import Customer
from models.product import Product


class TestSearch:
    def test_requires_authentication(self, client, seed_data):
        resp = client.get("/api/search?q=test")
        assert resp.status_code == 401

    def test_empty_query_returns_empty_categories(self, client, auth_headers, seed_data):
        resp = client.get("/api/search?q=", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["orders"] == []
        assert body["products"] == []

    def test_finds_product_by_name(self, client, auth_headers, seed_data, api_session_factory):
        db = api_session_factory()
        try:
            db.add(Product(name="검색테스트상품", category="테스트", status="ACTIVE"))
            db.commit()
        finally:
            db.close()

        resp = client.get("/api/search?q=검색테스트", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert any(p["label"] == "검색테스트상품" for p in body["products"])

    def test_finds_customer_by_name(self, client, auth_headers, seed_data, api_session_factory):
        db = api_session_factory()
        try:
            db.add(
                Customer(platform_id=seed_data["platform_id"], platform_customer_key="SEARCH-CUST", name="검색고객이름")
            )
            db.commit()
        finally:
            db.close()

        resp = client.get("/api/search?q=검색고객", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert any(c["label"] == "검색고객이름" for c in body["customers"])
