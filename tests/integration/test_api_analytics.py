"""
tests/integration/test_api_analytics.py
----------------------------------------------
api/routers/analytics.py 통합 테스트: 조회 + 계산엔진 트리거(POST) + upsert 확인.
"""


class TestProfitLoss:
    def test_list_empty_initially(self, client, auth_headers):
        resp = client.get("/api/analytics/profit-loss", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_calculate_creates_and_lists_summary(self, client, auth_headers):
        calc = client.post(
            "/api/analytics/profit-loss/calculate", json={"target_date": "2026-01-01"}, headers=auth_headers
        )
        assert calc.status_code == 200
        assert calc.json()["period_key"] == "2026-01-01"
        assert calc.json()["order_count"] == 0

        listing = client.get("/api/analytics/profit-loss", headers=auth_headers)
        assert len(listing.json()) == 1

    def test_calculate_twice_upserts_instead_of_duplicating(self, client, auth_headers):
        client.post("/api/analytics/profit-loss/calculate", json={"target_date": "2026-01-02"}, headers=auth_headers)
        client.post("/api/analytics/profit-loss/calculate", json={"target_date": "2026-01-02"}, headers=auth_headers)

        listing = client.get("/api/analytics/profit-loss", headers=auth_headers)
        matching = [row for row in listing.json() if row["period_key"] == "2026-01-02"]
        assert len(matching) == 1

    def test_requires_authentication(self, client, seed_data):
        resp = client.get("/api/analytics/profit-loss")
        assert resp.status_code == 401

    def test_calculate_with_platform_id_creates_separate_row_from_overall(self, client, auth_headers, seed_data):
        """SRS FR-PROFIT-02: 플랫폼 단위 세분화 조회."""
        overall = client.post(
            "/api/analytics/profit-loss/calculate", json={"target_date": "2026-01-03"}, headers=auth_headers
        )
        assert overall.status_code == 200
        assert overall.json()["platform_id"] is None

        by_platform = client.post(
            "/api/analytics/profit-loss/calculate",
            json={"target_date": "2026-01-03", "platform_id": seed_data["platform_id"]},
            headers=auth_headers,
        )
        assert by_platform.status_code == 200
        assert by_platform.json()["platform_id"] == seed_data["platform_id"]

        overall_listing = client.get("/api/analytics/profit-loss?period_type=DAILY", headers=auth_headers)
        matching_overall = [r for r in overall_listing.json() if r["period_key"] == "2026-01-03"]
        assert len(matching_overall) == 1
        assert matching_overall[0]["platform_id"] is None

        platform_listing = client.get(
            f"/api/analytics/profit-loss?period_type=DAILY&platform_id={seed_data['platform_id']}", headers=auth_headers
        )
        matching_platform = [r for r in platform_listing.json() if r["period_key"] == "2026-01-03"]
        assert len(matching_platform) == 1
        assert matching_platform[0]["platform_id"] == seed_data["platform_id"]


class TestProductPerformance:
    def test_list_empty_initially(self, client, auth_headers):
        resp = client.get("/api/analytics/product-performance?period_key=2026-05-01", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_calculate_and_rank(self, client, auth_headers, api_session_factory, seed_data):
        from datetime import datetime, timezone

        from models.order import Order, OrderItem
        from models.product import Product, ProductOption

        db = api_session_factory()
        try:
            product = Product(name="분석테스트 상품", category="test", base_price=1000, status="ACTIVE")
            db.add(product)
            db.flush()
            option = ProductOption(product_id=product.id, sku_code="PERF-API-SKU", is_active=True)
            db.add(option)
            db.flush()
            order = Order(
                platform_id=seed_data["platform_id"],
                platform_order_no="PERF-API-ORDER",
                status="DELIVERED",
                order_date=datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc),
                total_amount=20000,
                discount_amount=0,
            )
            db.add(order)
            db.flush()
            db.add(
                OrderItem(
                    order_id=order.id,
                    product_option_id=option.id,
                    quantity=2,
                    unit_price=10000,
                    cost_price_snapshot=3000,
                    line_amount=20000,
                )
            )
            db.commit()
            option_id = option.id
        finally:
            db.close()

        calc = client.post(
            "/api/analytics/product-performance/calculate", json={"target_date": "2026-05-01"}, headers=auth_headers
        )
        assert calc.status_code == 200
        assert len(calc.json()) == 1
        assert calc.json()[0]["product_option_id"] == option_id
        assert calc.json()[0]["sales_qty"] == 2
        assert calc.json()[0]["revenue"] == 20000
        assert calc.json()[0]["net_profit"] == 20000 - 3000 * 2

        ranked = client.get("/api/analytics/product-performance?period_key=2026-05-01&order=desc", headers=auth_headers)
        assert ranked.status_code == 200
        assert ranked.json()[0]["product_option_id"] == option_id

    def test_requires_authentication(self, client, seed_data):
        resp = client.get("/api/analytics/product-performance?period_key=2026-05-01")
        assert resp.status_code == 401
