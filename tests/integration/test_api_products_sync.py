"""
tests/integration/test_api_products_sync.py
------------------------------------------------
api/routers/products.py의 상용 ERP 확장(3단계, 첫 묶음) - 재고/판매상태 전송
엔드포인트 통합 테스트: 명령 접수(202)/조회/해소/RBAC/기본 차단.

실제 채널 HTTP 호출(execute_command)은 scheduler.jobs.product_sync_dispatch_job이
수행하며 여기서는 검증하지 않는다(services/tests/unit/test_product_sync_dispatch_
service.py에서 MockTransport 기반으로 이미 검증했다) - 이 통합 테스트는 API 계약
(202/404/400/idempotent/RBAC/기본 차단)만 확인한다. settings.product_channel_sync_
enabled는 기본 False이므로, 이 파일의 나머지 테스트(전송과 무관한 계약 확인)에
영향 없이 전체 모듈에서 켜 둔다 - TestSyncDisabledByDefault만 자체적으로 다시 꺼서
기본 차단을 검증한다.
"""

import pytest

from config.settings import settings


@pytest.fixture(autouse=True)
def _enable_product_sync(monkeypatch):
    monkeypatch.setattr(settings, "product_channel_sync_enabled", True)


def _create_mapping(client, auth_headers, seed_data, sku_suffix: str) -> int:
    product_id = client.post(
        "/api/products", json={"name": f"동기화 테스트 상품 {sku_suffix}"}, headers=auth_headers
    ).json()["id"]
    option_id = client.post(
        f"/api/products/{product_id}/options", json={"sku_code": f"SYNC-SKU-{sku_suffix}"}, headers=auth_headers
    ).json()["id"]
    mapping = client.post(
        f"/api/products/options/{option_id}/platform-map",
        json={"platform_id": seed_data["platform_id"], "platform_option_id": f"VENDOR-ITEM-{sku_suffix}"},
        headers=auth_headers,
    ).json()
    return mapping["id"]


class TestSyncInventory:
    def test_returns_202_with_pending_command(self, client, auth_headers, seed_data):
        mapping_id = _create_mapping(client, auth_headers, seed_data, "1")

        resp = client.post(
            f"/api/products/platform-map/{mapping_id}/sync-inventory",
            json={"target_quantity": 10},
            headers=auth_headers,
        )

        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "PENDING"
        assert body["already_processed"] is False
        assert isinstance(body["command_id"], int)

    def test_zero_quantity_is_accepted(self, client, auth_headers, seed_data):
        mapping_id = _create_mapping(client, auth_headers, seed_data, "2")

        resp = client.post(
            f"/api/products/platform-map/{mapping_id}/sync-inventory", json={"target_quantity": 0}, headers=auth_headers
        )

        assert resp.status_code == 202

    def test_negative_quantity_returns_400(self, client, auth_headers, seed_data):
        mapping_id = _create_mapping(client, auth_headers, seed_data, "3")

        resp = client.post(
            f"/api/products/platform-map/{mapping_id}/sync-inventory",
            json={"target_quantity": -1},
            headers=auth_headers,
        )

        assert resp.status_code == 400

    def test_over_max_quantity_returns_400(self, client, auth_headers, seed_data):
        mapping_id = _create_mapping(client, auth_headers, seed_data, "4")

        resp = client.post(
            f"/api/products/platform-map/{mapping_id}/sync-inventory",
            json={"target_quantity": 100_000_000},
            headers=auth_headers,
        )

        assert resp.status_code == 400

    def test_missing_mapping_returns_404(self, client, auth_headers):
        resp = client.post(
            "/api/products/platform-map/999999/sync-inventory", json={"target_quantity": 1}, headers=auth_headers
        )
        assert resp.status_code == 404

    def test_resubmitting_same_quantity_returns_same_command_idempotently(self, client, auth_headers, seed_data):
        mapping_id = _create_mapping(client, auth_headers, seed_data, "5")

        first = client.post(
            f"/api/products/platform-map/{mapping_id}/sync-inventory", json={"target_quantity": 7}, headers=auth_headers
        )
        second = client.post(
            f"/api/products/platform-map/{mapping_id}/sync-inventory", json={"target_quantity": 7}, headers=auth_headers
        )

        assert first.json()["command_id"] == second.json()["command_id"]

    def test_requires_authentication(self, client, seed_data):
        resp = client.post("/api/products/platform-map/1/sync-inventory", json={"target_quantity": 1})
        assert resp.status_code == 401


class TestSyncSaleStatus:
    def test_returns_202_with_pending_command(self, client, auth_headers, seed_data):
        mapping_id = _create_mapping(client, auth_headers, seed_data, "6")

        resp = client.post(
            f"/api/products/platform-map/{mapping_id}/sync-sale-status",
            json={"target_status": "SUSPENDED"},
            headers=auth_headers,
        )

        assert resp.status_code == 202
        assert resp.json()["status"] == "PENDING"

    def test_unknown_target_status_returns_400(self, client, auth_headers, seed_data):
        mapping_id = _create_mapping(client, auth_headers, seed_data, "7")

        resp = client.post(
            f"/api/products/platform-map/{mapping_id}/sync-sale-status",
            json={"target_status": "OUTOFSTOCK"},
            headers=auth_headers,
        )

        assert resp.status_code == 400

    def test_missing_mapping_returns_404(self, client, auth_headers):
        resp = client.post(
            "/api/products/platform-map/999999/sync-sale-status",
            json={"target_status": "ON_SALE"},
            headers=auth_headers,
        )
        assert resp.status_code == 404


class TestSyncCommandStatus:
    def test_returns_command_status(self, client, auth_headers, seed_data):
        mapping_id = _create_mapping(client, auth_headers, seed_data, "8")
        command_id = client.post(
            f"/api/products/platform-map/{mapping_id}/sync-inventory", json={"target_quantity": 3}, headers=auth_headers
        ).json()["command_id"]

        resp = client.get(f"/api/products/sync-commands/{command_id}", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json()["status"] == "PENDING"
        assert resp.json()["command_type"] == "INVENTORY_UPDATE"

    def test_missing_command_returns_404(self, client, auth_headers):
        resp = client.get("/api/products/sync-commands/999999", headers=auth_headers)
        assert resp.status_code == 404

    def test_shipment_command_is_not_visible_through_this_endpoint(self, client, auth_headers, api_session_factory):
        """target_type이 다른 명령(예: 송장 전송)은 이 엔드포인트로 조회되지 않는다 -
        서로 다른 worker/화면이 남의 명령종류를 잘못 집어가지 않는지 확인."""
        from models.integration_sync import ExternalCommand

        db = api_session_factory()
        try:
            command = ExternalCommand(
                idempotency_key="SHIPMENT_SUBMIT:999:TRK",
                command_type="SHIPMENT_SUBMIT",
                platform_id=1,
                target_type="SHIPMENT",
                target_id=999,
                status="PENDING",
                trace_id="trace",
            )
            db.add(command)
            db.commit()
            command_id = command.id
        finally:
            db.close()

        resp = client.get(f"/api/products/sync-commands/{command_id}", headers=auth_headers)
        assert resp.status_code == 404


class TestResolveUnknownProductSyncCommand:
    @staticmethod
    def _make_unknown_command(api_session_factory, command_id: int) -> None:
        from models.integration_sync import ExternalCommand

        db = api_session_factory()
        try:
            command = db.get(ExternalCommand, command_id)
            command.status = "UNKNOWN"
            db.commit()
        finally:
            db.close()

    def test_confirmed_not_sent_requeues_as_pending(self, client, auth_headers, seed_data, api_session_factory):
        mapping_id = _create_mapping(client, auth_headers, seed_data, "9")
        command_id = client.post(
            f"/api/products/platform-map/{mapping_id}/sync-inventory", json={"target_quantity": 4}, headers=auth_headers
        ).json()["command_id"]
        self._make_unknown_command(api_session_factory, command_id)

        resp = client.post(
            f"/api/products/sync-commands/{command_id}/resolve",
            json={"resolution": "CONFIRMED_NOT_SENT"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == "PENDING"

    def test_resolving_non_unknown_command_returns_400(self, client, auth_headers, seed_data):
        mapping_id = _create_mapping(client, auth_headers, seed_data, "10")
        command_id = client.post(
            f"/api/products/platform-map/{mapping_id}/sync-inventory", json={"target_quantity": 5}, headers=auth_headers
        ).json()["command_id"]

        resp = client.post(
            f"/api/products/sync-commands/{command_id}/resolve",
            json={"resolution": "CONFIRMED_SUCCESS"},
            headers=auth_headers,
        )

        assert resp.status_code == 400

    def test_missing_command_returns_404(self, client, auth_headers):
        resp = client.post(
            "/api/products/sync-commands/999999/resolve", json={"resolution": "CONFIRMED_SUCCESS"}, headers=auth_headers
        )
        assert resp.status_code == 404


class TestSyncDisabledByDefault:
    def test_sync_inventory_returns_503_when_disabled(self, client, auth_headers, seed_data, monkeypatch):
        monkeypatch.setattr(settings, "product_channel_sync_enabled", False)
        mapping_id = _create_mapping(client, auth_headers, seed_data, "11")

        resp = client.post(
            f"/api/products/platform-map/{mapping_id}/sync-inventory", json={"target_quantity": 1}, headers=auth_headers
        )

        assert resp.status_code == 503

    def test_sync_sale_status_returns_503_when_disabled(self, client, auth_headers, seed_data, monkeypatch):
        monkeypatch.setattr(settings, "product_channel_sync_enabled", False)
        mapping_id = _create_mapping(client, auth_headers, seed_data, "12")

        resp = client.post(
            f"/api/products/platform-map/{mapping_id}/sync-sale-status",
            json={"target_status": "ON_SALE"},
            headers=auth_headers,
        )

        assert resp.status_code == 503
