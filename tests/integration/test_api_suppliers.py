"""
tests/integration/test_api_suppliers.py
----------------------------------------------
api/routers/suppliers.py 통합 테스트: 공급처 CRUD, 담당자 CRUD,
상품옵션↔공급처 매핑(멱등), RBAC.
"""


def _create_option(client, auth_headers, sku="SUP-TEST-SKU-1"):
    product = client.post("/api/products", json={"name": "공급처테스트 상품"}, headers=auth_headers).json()
    option = client.post(f"/api/products/{product['id']}/options", json={"sku_code": sku}, headers=auth_headers).json()
    return option["id"]


class TestSupplierCrud:
    def test_requires_authentication(self, client):
        resp = client.get("/api/suppliers")
        assert resp.status_code == 401

    def test_create_list_get_update_supplier(self, client, auth_headers):
        created = client.post(
            "/api/suppliers",
            json={"name": "테스트공급처", "business_no": "123-45-67890", "payment_terms": "월말 정산"},
            headers=auth_headers,
        )
        assert created.status_code == 201
        supplier = created.json()
        assert supplier["name"] == "테스트공급처"
        assert supplier["is_active"] is True

        listed = client.get("/api/suppliers", headers=auth_headers)
        assert listed.status_code == 200
        assert any(s["id"] == supplier["id"] for s in listed.json())

        fetched = client.get(f"/api/suppliers/{supplier['id']}", headers=auth_headers)
        assert fetched.status_code == 200
        assert fetched.json()["name"] == "테스트공급처"

        updated = client.patch(
            f"/api/suppliers/{supplier['id']}",
            json={"payment_terms": "익월 10일 정산", "is_active": False},
            headers=auth_headers,
        )
        assert updated.status_code == 200
        assert updated.json()["payment_terms"] == "익월 10일 정산"
        assert updated.json()["is_active"] is False

        active_only = client.get("/api/suppliers?active_only=true", headers=auth_headers)
        assert all(s["id"] != supplier["id"] for s in active_only.json())

    def test_get_missing_supplier_returns_404(self, client, auth_headers):
        resp = client.get("/api/suppliers/999999", headers=auth_headers)
        assert resp.status_code == 404

    def test_update_missing_supplier_returns_404(self, client, auth_headers):
        resp = client.patch("/api/suppliers/999999", json={"name": "x"}, headers=auth_headers)
        assert resp.status_code == 404


class TestSupplierContacts:
    def test_add_list_delete_contact(self, client, auth_headers):
        supplier = client.post("/api/suppliers", json={"name": "담당자테스트공급처"}, headers=auth_headers).json()

        created = client.post(
            f"/api/suppliers/{supplier['id']}/contacts",
            json={"name": "김담당", "phone": "010-1234-5678", "is_primary": True},
            headers=auth_headers,
        )
        assert created.status_code == 201
        contact = created.json()
        assert contact["name"] == "김담당"
        assert contact["supplier_id"] == supplier["id"]

        listed = client.get(f"/api/suppliers/{supplier['id']}/contacts", headers=auth_headers)
        assert listed.status_code == 200
        assert len(listed.json()) == 1

        deleted = client.delete(f"/api/suppliers/{supplier['id']}/contacts/{contact['id']}", headers=auth_headers)
        assert deleted.status_code == 204

        listed_after = client.get(f"/api/suppliers/{supplier['id']}/contacts", headers=auth_headers)
        assert listed_after.json() == []

    def test_contacts_for_missing_supplier_returns_404(self, client, auth_headers):
        resp = client.get("/api/suppliers/999999/contacts", headers=auth_headers)
        assert resp.status_code == 404

    def test_delete_contact_wrong_supplier_returns_404(self, client, auth_headers):
        supplier_a = client.post("/api/suppliers", json={"name": "A공급처"}, headers=auth_headers).json()
        supplier_b = client.post("/api/suppliers", json={"name": "B공급처"}, headers=auth_headers).json()
        contact = client.post(
            f"/api/suppliers/{supplier_a['id']}/contacts", json={"name": "이담당"}, headers=auth_headers
        ).json()

        resp = client.delete(f"/api/suppliers/{supplier_b['id']}/contacts/{contact['id']}", headers=auth_headers)
        assert resp.status_code == 404


class TestProductSupplierMap:
    def test_map_supplier_then_list_by_option_is_idempotent(self, client, auth_headers):
        option_id = _create_option(client, auth_headers, sku="SUP-MAP-SKU")
        supplier = client.post("/api/suppliers", json={"name": "매핑테스트공급처"}, headers=auth_headers).json()

        first = client.post(
            "/api/suppliers/product-options/map",
            json={"product_option_id": option_id, "supplier_id": supplier["id"], "is_primary": True},
            headers=auth_headers,
        )
        assert first.status_code == 201
        mapping = first.json()

        second = client.post(
            "/api/suppliers/product-options/map",
            json={"product_option_id": option_id, "supplier_id": supplier["id"], "is_primary": True},
            headers=auth_headers,
        )
        assert second.status_code == 201
        assert second.json()["id"] == mapping["id"]  # 멱등 - 새로 만들지 않음

        listed = client.get(f"/api/suppliers/product-options/{option_id}/suppliers", headers=auth_headers)
        assert listed.status_code == 200
        assert len(listed.json()) == 1

        deleted = client.delete(f"/api/suppliers/product-options/map/{mapping['id']}", headers=auth_headers)
        assert deleted.status_code == 204

        listed_after = client.get(f"/api/suppliers/product-options/{option_id}/suppliers", headers=auth_headers)
        assert listed_after.json() == []

    def test_map_missing_product_option_returns_404(self, client, auth_headers):
        supplier = client.post("/api/suppliers", json={"name": "매핑오류테스트공급처"}, headers=auth_headers).json()
        resp = client.post(
            "/api/suppliers/product-options/map",
            json={"product_option_id": 999999, "supplier_id": supplier["id"]},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_map_missing_supplier_returns_404(self, client, auth_headers):
        option_id = _create_option(client, auth_headers, sku="SUP-MAP-MISSING-SKU")
        resp = client.post(
            "/api/suppliers/product-options/map",
            json={"product_option_id": option_id, "supplier_id": 999999},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_delete_missing_map_returns_404(self, client, auth_headers):
        resp = client.delete("/api/suppliers/product-options/map/999999", headers=auth_headers)
        assert resp.status_code == 404
