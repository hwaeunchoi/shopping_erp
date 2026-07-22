"""
tests/integration/test_api_inventory.py
----------------------------------------------
api/routers/inventory.py 통합 테스트: 목록 조회(SKU/상품명/창고명 포함),
수동 조정(입고/실사, product_option_id+warehouse_id 기준 - 신규 조합 자동 생성 포함),
안전재고 변경, 입출고 이력 조회, RBAC.
"""


def _create_option(client, auth_headers, sku="INV-TEST-SKU-1"):
    product = client.post("/api/products", json={"name": "재고테스트 상품"}, headers=auth_headers).json()
    option = client.post(f"/api/products/{product['id']}/options", json={"sku_code": sku}, headers=auth_headers).json()
    return option["id"]


class TestListInventory:
    def test_requires_authentication(self, client):
        resp = client.get("/api/inventory")
        assert resp.status_code == 401


class TestAdjustAndSafetyStock:
    def test_adjust_creates_inventory_row_then_visible_in_list(self, client, auth_headers, seed_data):
        option_id = _create_option(client, auth_headers, sku="INV-ADJUST-SKU")

        created = client.post(
            "/api/inventory/adjust",
            json={
                "product_option_id": option_id,
                "warehouse_id": seed_data["warehouse_id"],
                "delta": 50,
                "memo": "초기 입고",
            },
            headers=auth_headers,
        )
        assert created.status_code == 200
        assert created.json()["sellable_stock"] == 50
        inventory_id = created.json()["id"]

        listed = client.get("/api/inventory", headers=auth_headers)
        assert listed.status_code == 200
        row = next(r for r in listed.json() if r["id"] == inventory_id)
        assert row["sku_code"] == "INV-ADJUST-SKU"
        assert row["product_name"] == "재고테스트 상품"
        assert row["warehouse_name"] == "본사창고"
        assert row["sellable_stock"] == 50

        adjusted = client.post(
            "/api/inventory/adjust",
            json={
                "product_option_id": option_id,
                "warehouse_id": seed_data["warehouse_id"],
                "delta": -10,
                "memo": "실사 감모",
            },
            headers=auth_headers,
        )
        assert adjusted.status_code == 200
        assert adjusted.json()["sellable_stock"] == 40
        assert adjusted.json()["id"] == inventory_id

    def test_adjust_below_zero_returns_409(self, client, auth_headers, seed_data):
        option_id = _create_option(client, auth_headers, sku="INV-NEG-SKU")
        client.post(
            "/api/inventory/adjust",
            json={"product_option_id": option_id, "warehouse_id": seed_data["warehouse_id"], "delta": 5},
            headers=auth_headers,
        )

        resp = client.post(
            "/api/inventory/adjust",
            json={"product_option_id": option_id, "warehouse_id": seed_data["warehouse_id"], "delta": -999},
            headers=auth_headers,
        )
        assert resp.status_code == 409

    def test_update_safety_stock(self, client, auth_headers, seed_data):
        option_id = _create_option(client, auth_headers, sku="INV-SAFETY-SKU")
        client.post(
            "/api/inventory/adjust",
            json={"product_option_id": option_id, "warehouse_id": seed_data["warehouse_id"], "delta": 5},
            headers=auth_headers,
        )

        resp = client.patch(
            "/api/inventory/safety-stock",
            json={"product_option_id": option_id, "warehouse_id": seed_data["warehouse_id"], "safety_stock": 20},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["safety_stock"] == 20
        assert resp.json()["sellable_stock"] == 5  # 재고 수량은 그대로

    def test_safety_stock_creates_inventory_row_when_missing(self, client, auth_headers, seed_data):
        option_id = _create_option(client, auth_headers, sku="INV-SAFETY-NEW-SKU")

        resp = client.patch(
            "/api/inventory/safety-stock",
            json={"product_option_id": option_id, "warehouse_id": seed_data["warehouse_id"], "safety_stock": 3},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["safety_stock"] == 3
        assert resp.json()["sellable_stock"] == 0


class TestInventoryTransactions:
    def test_lists_transactions_after_adjustments(self, client, auth_headers, seed_data):
        option_id = _create_option(client, auth_headers, sku="INV-TXN-SKU")

        created = client.post(
            "/api/inventory/adjust",
            json={
                "product_option_id": option_id,
                "warehouse_id": seed_data["warehouse_id"],
                "delta": 30,
                "memo": "1차 입고",
            },
            headers=auth_headers,
        )
        inventory_id = created.json()["id"]

        client.post(
            "/api/inventory/adjust",
            json={
                "product_option_id": option_id,
                "warehouse_id": seed_data["warehouse_id"],
                "delta": -5,
                "memo": "2차 조정",
            },
            headers=auth_headers,
        )

        resp = client.get(f"/api/inventory/{inventory_id}/transactions", headers=auth_headers)
        assert resp.status_code == 200
        types = [t["type"] for t in resp.json()]
        assert types.count("ADJUST") == 2

    def test_transactions_missing_inventory_returns_404(self, client, auth_headers):
        resp = client.get("/api/inventory/999999/transactions", headers=auth_headers)
        assert resp.status_code == 404
