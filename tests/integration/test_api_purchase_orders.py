"""
tests/integration/test_api_purchase_orders.py
-------------------------------------------------
api/routers/purchase_orders.py 통합 테스트: 작성(DRAFT)/품목추가/확정/입고처리
(부분·전량)/취소, RBAC, 상태 위반 409, 존재하지 않는 리소스 404.
"""


def _create_option(client, auth_headers, sku="PO-TEST-SKU-1"):
    product = client.post("/api/products", json={"name": "발주테스트 상품"}, headers=auth_headers).json()
    option = client.post(f"/api/products/{product['id']}/options", json={"sku_code": sku}, headers=auth_headers).json()
    return option["id"]


def _create_supplier(client, auth_headers, name="발주테스트공급처"):
    return client.post("/api/suppliers", json={"name": name}, headers=auth_headers).json()["id"]


class TestPurchaseOrderLifecycle:
    def test_requires_authentication(self, client):
        resp = client.get("/api/purchase-orders")
        assert resp.status_code == 401

    def test_create_add_item_confirm_receive_full_cycle(self, client, auth_headers, seed_data):
        supplier_id = _create_supplier(client, auth_headers)
        option_id = _create_option(client, auth_headers, sku="PO-CYCLE-SKU")

        created = client.post(
            "/api/purchase-orders", json={"supplier_id": supplier_id, "memo": "정기발주"}, headers=auth_headers
        )
        assert created.status_code == 201
        po = created.json()
        assert po["status"] == "DRAFT"

        item_resp = client.post(
            f"/api/purchase-orders/{po['id']}/items",
            json={"product_option_id": option_id, "quantity": 50, "unit_cost": 1000},
            headers=auth_headers,
        )
        assert item_resp.status_code == 201
        item = item_resp.json()

        confirmed = client.post(f"/api/purchase-orders/{po['id']}/confirm", headers=auth_headers)
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "ORDERED"
        assert confirmed.json()["order_date"] is not None

        received = client.post(
            f"/api/purchase-orders/{po['id']}/items/{item['id']}/receive",
            json={"quantity": 50, "warehouse_id": seed_data["warehouse_id"]},
            headers=auth_headers,
        )
        assert received.status_code == 200
        assert received.json()["received_quantity"] == 50

        detail = client.get(f"/api/purchase-orders/{po['id']}", headers=auth_headers)
        assert detail.status_code == 200
        assert detail.json()["status"] == "RECEIVED"

        inventory = client.get("/api/inventory", headers=auth_headers).json()
        row = next(r for r in inventory if r["sku_code"] == "PO-CYCLE-SKU")
        assert row["sellable_stock"] == 50

    def test_partial_receive_keeps_partially_received_status(self, client, auth_headers, seed_data):
        supplier_id = _create_supplier(client, auth_headers, name="부분입고공급처")
        option_id = _create_option(client, auth_headers, sku="PO-PARTIAL-SKU")

        po = client.post("/api/purchase-orders", json={"supplier_id": supplier_id}, headers=auth_headers).json()
        item = client.post(
            f"/api/purchase-orders/{po['id']}/items",
            json={"product_option_id": option_id, "quantity": 20, "unit_cost": 500},
            headers=auth_headers,
        ).json()
        client.post(f"/api/purchase-orders/{po['id']}/confirm", headers=auth_headers)

        received = client.post(
            f"/api/purchase-orders/{po['id']}/items/{item['id']}/receive",
            json={"quantity": 8, "warehouse_id": seed_data["warehouse_id"]},
            headers=auth_headers,
        )
        assert received.status_code == 200

        detail = client.get(f"/api/purchase-orders/{po['id']}", headers=auth_headers)
        assert detail.json()["status"] == "PARTIALLY_RECEIVED"

    def test_add_item_after_confirm_returns_409(self, client, auth_headers):
        supplier_id = _create_supplier(client, auth_headers, name="상태위반공급처")
        option_id = _create_option(client, auth_headers, sku="PO-STATE-SKU")

        po = client.post("/api/purchase-orders", json={"supplier_id": supplier_id}, headers=auth_headers).json()
        client.post(
            f"/api/purchase-orders/{po['id']}/items",
            json={"product_option_id": option_id, "quantity": 5, "unit_cost": 100},
            headers=auth_headers,
        )
        client.post(f"/api/purchase-orders/{po['id']}/confirm", headers=auth_headers)

        resp = client.post(
            f"/api/purchase-orders/{po['id']}/items",
            json={"product_option_id": option_id, "quantity": 1, "unit_cost": 100},
            headers=auth_headers,
        )
        assert resp.status_code == 409

    def test_confirm_without_items_returns_409(self, client, auth_headers):
        supplier_id = _create_supplier(client, auth_headers, name="빈발주공급처")
        po = client.post("/api/purchase-orders", json={"supplier_id": supplier_id}, headers=auth_headers).json()

        resp = client.post(f"/api/purchase-orders/{po['id']}/confirm", headers=auth_headers)
        assert resp.status_code == 409

    def test_cancel_draft_then_confirm_returns_409(self, client, auth_headers):
        supplier_id = _create_supplier(client, auth_headers, name="취소공급처")
        po = client.post("/api/purchase-orders", json={"supplier_id": supplier_id}, headers=auth_headers).json()

        cancelled = client.post(f"/api/purchase-orders/{po['id']}/cancel", headers=auth_headers)
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "CANCELLED"

        resp = client.post(f"/api/purchase-orders/{po['id']}/confirm", headers=auth_headers)
        assert resp.status_code == 409

    def test_create_missing_supplier_returns_404(self, client, auth_headers):
        resp = client.post("/api/purchase-orders", json={"supplier_id": 999999}, headers=auth_headers)
        assert resp.status_code == 404

    def test_get_missing_purchase_order_returns_404(self, client, auth_headers):
        resp = client.get("/api/purchase-orders/999999", headers=auth_headers)
        assert resp.status_code == 404

    def test_receive_over_remaining_quantity_returns_409(self, client, auth_headers, seed_data):
        supplier_id = _create_supplier(client, auth_headers, name="초과입고공급처")
        option_id = _create_option(client, auth_headers, sku="PO-OVER-SKU")

        po = client.post("/api/purchase-orders", json={"supplier_id": supplier_id}, headers=auth_headers).json()
        item = client.post(
            f"/api/purchase-orders/{po['id']}/items",
            json={"product_option_id": option_id, "quantity": 5, "unit_cost": 100},
            headers=auth_headers,
        ).json()
        client.post(f"/api/purchase-orders/{po['id']}/confirm", headers=auth_headers)

        resp = client.post(
            f"/api/purchase-orders/{po['id']}/items/{item['id']}/receive",
            json={"quantity": 6, "warehouse_id": seed_data["warehouse_id"]},
            headers=auth_headers,
        )
        assert resp.status_code == 409

    def test_list_filters_by_status(self, client, auth_headers):
        supplier_id = _create_supplier(client, auth_headers, name="목록필터공급처")
        po = client.post("/api/purchase-orders", json={"supplier_id": supplier_id}, headers=auth_headers).json()

        listed = client.get("/api/purchase-orders?status_filter=DRAFT", headers=auth_headers)
        assert listed.status_code == 200
        assert any(p["id"] == po["id"] for p in listed.json())

        listed_ordered = client.get("/api/purchase-orders?status_filter=ORDERED", headers=auth_headers)
        assert all(p["id"] != po["id"] for p in listed_ordered.json())
