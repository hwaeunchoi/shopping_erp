"""
tests/integration/test_api_fulfillment.py
------------------------------------------------
api/routers/fulfillment.py 통합 테스트 - 출고 배치(피킹/검수/포장/송장등록/
채널전송 접수) API 계약. 항목별 업무규칙(재고 차감/중복 방지/상태전이)은 이미
tests/unit/test_fulfillment_service.py가 SQLite로 상세히 검증했다 - 여기서는
그 서비스가 실제 라우터·인증·권한(SHIPMENT_VIEW)·DB 세션 의존성 주입과 올바르게
연결됐는지(API 계약)만 확인한다.

주문/주문상품/재고는 공개 API로 만들 수 없어(수동 생성 엔드포인트가 없음)
api_session_factory로 직접 시딩한다 - 상품/옵션은 기존 상품 API를 그대로
재사용한다(products.py 통합 테스트의 _create_option과 동일 패턴).
"""

import uuid
from datetime import datetime, timezone

import pytest

from config.settings import settings


@pytest.fixture(autouse=True)
def _enable_shipment_submit(monkeypatch):
    monkeypatch.setattr(settings, "shipment_channel_submit_enabled", True)


def _create_option(client, auth_headers, sku_suffix: str) -> tuple[int, int]:
    product_id = client.post(
        "/api/products", json={"name": f"출고테스트상품 {sku_suffix}"}, headers=auth_headers
    ).json()["id"]
    option_id = client.post(
        f"/api/products/{product_id}/options", json={"sku_code": f"FUL-SKU-{sku_suffix}"}, headers=auth_headers
    ).json()["id"]
    return product_id, option_id


def _seed_order_item(api_session_factory, seed_data, option_id: int, quantity: int) -> tuple[int, int]:
    from models.order import Order, OrderItem

    db = api_session_factory()
    try:
        order = Order(
            platform_id=seed_data["platform_id"],
            platform_order_no=f"FUL-API-{uuid.uuid4().hex[:8]}",
            status="NEW",
            order_date=datetime.now(timezone.utc),
            total_amount=10000,
        )
        db.add(order)
        db.flush()
        item = OrderItem(
            order_id=order.id,
            product_option_id=option_id,
            quantity=quantity,
            unit_price=1000,
            line_amount=1000 * quantity,
            platform_order_item_no=f"FUL-LINE-{uuid.uuid4().hex[:8]}",
        )
        db.add(item)
        db.commit()
        return order.id, item.id
    finally:
        db.close()


def _seed_inventory(api_session_factory, seed_data, option_id: int, sellable_stock: int) -> None:
    from models.inventory import Inventory

    db = api_session_factory()
    try:
        db.add(
            Inventory(
                product_option_id=option_id,
                warehouse_id=seed_data["warehouse_id"],
                sellable_stock=sellable_stock,
                reserved_stock=0,
                safety_stock=0,
                updated_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
    finally:
        db.close()


class TestPermission:
    def test_requires_authentication(self, client):
        resp = client.get("/api/fulfillment/fulfillable-order-items")
        assert resp.status_code == 401


class TestWarehouses:
    def test_lists_active_warehouse(self, client, auth_headers, seed_data):
        resp = client.get("/api/fulfillment/warehouses", headers=auth_headers)
        assert resp.status_code == 200
        assert any(w["id"] == seed_data["warehouse_id"] for w in resp.json())


class TestFulfillableOrderItems:
    def test_lists_new_order_item(self, client, auth_headers, seed_data, api_session_factory):
        _, option_id = _create_option(client, auth_headers, "1")
        _seed_order_item(api_session_factory, seed_data, option_id, 5)

        resp = client.get(
            "/api/fulfillment/fulfillable-order-items",
            params={"platform_id": seed_data["platform_id"]},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert any(i["remaining_quantity"] == 5 for i in resp.json())


class TestCreateBatch:
    def test_happy_path(self, client, auth_headers, seed_data, api_session_factory):
        _, option_id = _create_option(client, auth_headers, "2")
        _, item_id = _seed_order_item(api_session_factory, seed_data, option_id, 5)

        resp = client.post(
            "/api/fulfillment/batches",
            json={"warehouse_id": seed_data["warehouse_id"], "selections": [{"order_item_id": item_id, "quantity": 5}]},
            headers=auth_headers,
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["batch"]["warehouse_id"] == seed_data["warehouse_id"]
        assert len(body["items"]) == 1
        assert body["items"][0]["requested_quantity"] == 5
        assert body["items"][0]["status"] == "READY"

    def test_quantity_exceeding_remaining_returns_400(self, client, auth_headers, seed_data, api_session_factory):
        _, option_id = _create_option(client, auth_headers, "3")
        _, item_id = _seed_order_item(api_session_factory, seed_data, option_id, 5)

        resp = client.post(
            "/api/fulfillment/batches",
            json={"warehouse_id": seed_data["warehouse_id"], "selections": [{"order_item_id": item_id, "quantity": 6}]},
            headers=auth_headers,
        )

        assert resp.status_code == 400


class TestFullWorkflow:
    def _create_batch_item(self, client, auth_headers, seed_data, api_session_factory, sku_suffix, quantity=5):
        _, option_id = _create_option(client, auth_headers, sku_suffix)
        _, item_id = _seed_order_item(api_session_factory, seed_data, option_id, quantity)
        _seed_inventory(api_session_factory, seed_data, option_id, sellable_stock=100)
        resp = client.post(
            "/api/fulfillment/batches",
            json={
                "warehouse_id": seed_data["warehouse_id"],
                "selections": [{"order_item_id": item_id, "quantity": quantity}],
            },
            headers=auth_headers,
        )
        batch_item_id = resp.json()["items"][0]["id"]
        return batch_item_id

    def test_pick_verify_pack_submit(self, client, auth_headers, seed_data, api_session_factory):
        bi = self._create_batch_item(client, auth_headers, seed_data, api_session_factory, "4", quantity=5)

        r1 = client.post(
            f"/api/fulfillment/batch-items/{bi}/start-picking", json={"expected_status": "READY"}, headers=auth_headers
        )
        assert r1.status_code == 200 and r1.json()["status"] == "PICKING"

        r2 = client.post(
            f"/api/fulfillment/batch-items/{bi}/complete-picking",
            json={"expected_status": "PICKING", "picked_quantity": 5},
            headers=auth_headers,
        )
        assert r2.status_code == 200 and r2.json()["status"] == "PICKED"

        r3 = client.post(
            f"/api/fulfillment/batch-items/{bi}/start-verification",
            json={"expected_status": "PICKED"},
            headers=auth_headers,
        )
        assert r3.status_code == 200 and r3.json()["status"] == "VERIFYING"

        r4 = client.post(
            f"/api/fulfillment/batch-items/{bi}/complete-verification",
            json={"expected_status": "VERIFYING", "verified_quantity": 5},
            headers=auth_headers,
        )
        assert r4.status_code == 200 and r4.json()["status"] == "VERIFIED"

        r5 = client.post(
            "/api/fulfillment/batch-items/pack",
            json={"batch_item_ids": [bi], "carrier": "CJ_LOGISTICS", "tracking_no": "API-TEST-0001"},
            headers=auth_headers,
        )
        assert r5.status_code == 200
        assert r5.json()[0]["outcome"] == "ACCEPTED"

        batch_detail = client.get(
            f"/api/fulfillment/batches/{client.get('/api/fulfillment/batches', headers=auth_headers).json()[0]['id']}",
            headers=auth_headers,
        ).json()
        shipment_id = next(i["shipment_id"] for i in batch_detail["items"] if i["id"] == bi)
        assert shipment_id is not None

        r6 = client.post(
            "/api/fulfillment/shipments/submit", json={"shipment_ids": [shipment_id]}, headers=auth_headers
        )
        assert r6.status_code == 202
        assert r6.json()[0]["outcome"] == "ACCEPTED"

        progress = client.get(
            f"/api/fulfillment/batches/{batch_detail['batch']['id']}/progress", headers=auth_headers
        ).json()
        assert progress[0]["batch_item"]["status"] == "SUBMITTED"
        assert progress[0]["command_status"] == "PENDING"

        history = client.get(
            f"/api/fulfillment/batches/{batch_detail['batch']['id']}/history", headers=auth_headers
        ).json()
        to_statuses = [h["to_status"] for h in history]
        assert to_statuses == ["READY", "PICKING", "PICKED", "VERIFYING", "VERIFIED", "PACKED", "SUBMITTED"]

    def test_invalid_carrier_returns_validation_failed_not_500(
        self, client, auth_headers, seed_data, api_session_factory
    ):
        bi = self._create_batch_item(client, auth_headers, seed_data, api_session_factory, "5", quantity=3)
        client.post(
            f"/api/fulfillment/batch-items/{bi}/start-picking", json={"expected_status": "READY"}, headers=auth_headers
        )
        client.post(
            f"/api/fulfillment/batch-items/{bi}/complete-picking",
            json={"expected_status": "PICKING", "picked_quantity": 3},
            headers=auth_headers,
        )
        client.post(
            f"/api/fulfillment/batch-items/{bi}/start-verification",
            json={"expected_status": "PICKED"},
            headers=auth_headers,
        )
        client.post(
            f"/api/fulfillment/batch-items/{bi}/complete-verification",
            json={"expected_status": "VERIFYING", "verified_quantity": 3},
            headers=auth_headers,
        )

        resp = client.post(
            "/api/fulfillment/batch-items/pack",
            json={"batch_item_ids": [bi], "carrier": "DHL", "tracking_no": "X"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert resp.json()[0]["outcome"] == "VALIDATION_FAILED"

    def test_stale_expected_status_returns_409(self, client, auth_headers, seed_data, api_session_factory):
        bi = self._create_batch_item(client, auth_headers, seed_data, api_session_factory, "6")
        client.post(
            f"/api/fulfillment/batch-items/{bi}/start-picking", json={"expected_status": "READY"}, headers=auth_headers
        )

        resp = client.post(
            f"/api/fulfillment/batch-items/{bi}/start-picking", json={"expected_status": "READY"}, headers=auth_headers
        )

        assert resp.status_code == 409

    def test_cancel_before_packing(self, client, auth_headers, seed_data, api_session_factory):
        bi = self._create_batch_item(client, auth_headers, seed_data, api_session_factory, "7")

        resp = client.post(
            f"/api/fulfillment/batch-items/{bi}/cancel", json={"expected_status": "READY"}, headers=auth_headers
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == "CANCELLED"


class TestExistingShipmentApiRegression:
    def test_single_shipment_create_still_works(self, client, auth_headers, seed_data, api_session_factory):
        _, option_id = _create_option(client, auth_headers, "8")
        order_id, _ = _seed_order_item(api_session_factory, seed_data, option_id, 2)

        resp = client.post(
            "/api/shipments",
            json={"order_id": order_id, "carrier": "CJ_LOGISTICS", "tracking_no": "REGRESSION-1"},
            headers=auth_headers,
        )

        assert resp.status_code == 201
        assert resp.json()["status"] == "READY"

    def test_shipments_list_endpoint_unaffected(self, client, auth_headers):
        resp = client.get("/api/shipments", headers=auth_headers)
        assert resp.status_code == 200
