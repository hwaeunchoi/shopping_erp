"""
tests/integration/test_api_products_bulk.py
------------------------------------------------
api/routers/products_bulk.py의 상용 ERP 확장(3단계, 네 번째 묶음) - 상품/옵션조합
등록과 재고/판매상태/정보수정 대량 접수·진행상태 조회·재처리 API 계약 테스트.

항목별 안전성(중복 판정/UNKNOWN 차단/트랜잭션 경계 등)은 이미 tests/unit/
test_product_bulk_service.py가 SQLite로 상세히 검증했다 - 이 통합 테스트는 그
서비스가 실제 FastAPI 라우터·인증·권한(RBAC)·DB 세션 의존성 주입과 올바르게
연결돼 있는지(API 계약)만 확인한다. 실제 채널 HTTP 호출은 하지 않는다(이
엔드포인트들은 애초에 접수(enqueue)만 하고 전송은 스케줄러가 한다).
"""

import pytest

from config.settings import settings


@pytest.fixture(autouse=True)
def _enable_all_flags(monkeypatch):
    monkeypatch.setattr(settings, "product_publish_enabled", True)
    monkeypatch.setattr(settings, "product_option_publish_enabled", True)
    monkeypatch.setattr(settings, "product_channel_sync_enabled", True)
    monkeypatch.setattr(settings, "product_info_update_enabled", True)


def _create_option(client, auth_headers, sku_suffix: str) -> tuple[int, int]:
    product_id = client.post(
        "/api/products", json={"name": f"대량처리 테스트 상품 {sku_suffix}"}, headers=auth_headers
    ).json()["id"]
    option_id = client.post(
        f"/api/products/{product_id}/options", json={"sku_code": f"BULK-SKU-{sku_suffix}"}, headers=auth_headers
    ).json()["id"]
    return product_id, option_id


def _create_draft(client, auth_headers, seed_data, sku_suffix: str) -> int:
    _, option_id = _create_option(client, auth_headers, sku_suffix)
    resp = client.post(
        f"/api/products/options/{option_id}/publish-draft",
        json={
            "platform_id": seed_data["platform_id"],
            "name": "대량등록 테스트상품",
            "sale_price": 19900,
            "description_html": "<p>설명</p>",
            "category_code": "50000803",
            "image_urls": ["https://img.example.com/a.jpg"],
            "stock_quantity": 10,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    return resp.json()["id"]


def _create_mapping(client, auth_headers, seed_data, sku_suffix: str) -> int:
    _, option_id = _create_option(client, auth_headers, sku_suffix)
    mapping = client.post(
        f"/api/products/options/{option_id}/platform-map",
        json={"platform_id": seed_data["platform_id"], "platform_option_id": f"VENDOR-ITEM-{sku_suffix}"},
        headers=auth_headers,
    ).json()
    return mapping["id"]


class TestListEndpoints:
    def test_list_publish_drafts(self, client, auth_headers, seed_data):
        draft_id = _create_draft(client, auth_headers, seed_data, "L1")

        resp = client.get("/api/products/bulk/publish-drafts", headers=auth_headers)

        assert resp.status_code == 200
        ids = [item["id"] for item in resp.json()["items"]]
        assert draft_id in ids

    def test_list_platform_maps_with_keyword_filter(self, client, auth_headers, seed_data):
        mapping_id = _create_mapping(client, auth_headers, seed_data, "L2")

        resp = client.get("/api/products/bulk/platform-maps", params={"keyword": "BULK-SKU-L2"}, headers=auth_headers)

        assert resp.status_code == 200
        ids = [item["id"] for item in resp.json()["items"]]
        assert mapping_id in ids

    def test_requires_authentication(self, client):
        resp = client.get("/api/products/bulk/publish-drafts")
        assert resp.status_code == 401


class TestSubmitPublishDrafts:
    def test_all_accepted(self, client, auth_headers, seed_data):
        d1 = _create_draft(client, auth_headers, seed_data, "S1")
        d2 = _create_draft(client, auth_headers, seed_data, "S2")

        resp = client.post(
            "/api/products/bulk/publish-drafts/submit", json={"draft_ids": [d1, d2]}, headers=auth_headers
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["aborted"] is False
        assert [item["outcome"] for item in body["items"]] == ["ACCEPTED", "ACCEPTED"]
        assert all(item["command_id"] is not None for item in body["items"])

    def test_not_found_does_not_block_rest(self, client, auth_headers, seed_data):
        d1 = _create_draft(client, auth_headers, seed_data, "S3")

        resp = client.post(
            "/api/products/bulk/publish-drafts/submit", json={"draft_ids": [999999, d1]}, headers=auth_headers
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["items"][0]["outcome"] == "VALIDATION_FAILED"
        assert body["items"][0]["error_code"] == "NOT_FOUND"
        assert body["items"][1]["outcome"] == "ACCEPTED"

    def test_disabled_flag_returns_validation_failed_not_503(self, client, auth_headers, seed_data, monkeypatch):
        # 대량 접수는 단건과 달리 항목별 결과 배열을 돌려주는 계약이라 503 대신
        # VALIDATION_FAILED(FEATURE_DISABLED)로 표현한다 - 일부 종류만 꺼져 있어도
        # 나머지 종류의 대량 접수는 계속 동작해야 하므로 전체 요청을 실패시키지
        # 않는다(모듈 docstring 참고).
        monkeypatch.setattr(settings, "product_publish_enabled", False)
        d1 = _create_draft(client, auth_headers, seed_data, "S4")

        resp = client.post("/api/products/bulk/publish-drafts/submit", json={"draft_ids": [d1]}, headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json()["items"][0]["outcome"] == "VALIDATION_FAILED"
        assert resp.json()["items"][0]["error_code"] == "FEATURE_DISABLED"


class TestSubmitInventoryAndSaleStatus:
    def test_inventory_submit_and_status_query(self, client, auth_headers, seed_data):
        mapping_id = _create_mapping(client, auth_headers, seed_data, "I1")

        submit_resp = client.post(
            "/api/products/bulk/platform-map/sync-inventory/submit",
            json={"items": [{"product_platform_map_id": mapping_id, "target_quantity": 7}]},
            headers=auth_headers,
        )
        assert submit_resp.status_code == 200
        item = submit_resp.json()["items"][0]
        assert item["outcome"] == "ACCEPTED"
        command_id = item["command_id"]

        status_resp = client.post(
            "/api/products/bulk/commands/status", json={"command_ids": [command_id]}, headers=auth_headers
        )
        assert status_resp.status_code == 200
        statuses = status_resp.json()["items"]
        assert len(statuses) == 1
        assert statuses[0]["id"] == command_id
        assert statuses[0]["status"] == "PENDING"

    def test_negative_quantity_is_validation_failed(self, client, auth_headers, seed_data):
        mapping_id = _create_mapping(client, auth_headers, seed_data, "I2")

        resp = client.post(
            "/api/products/bulk/platform-map/sync-inventory/submit",
            json={"items": [{"product_platform_map_id": mapping_id, "target_quantity": -5}]},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert resp.json()["items"][0]["outcome"] == "VALIDATION_FAILED"


class TestRetryCommands:
    @staticmethod
    def _set_status(api_session_factory, command_id: int, status: str) -> None:
        from models.integration_sync import ExternalCommand

        db = api_session_factory()
        try:
            command = db.get(ExternalCommand, command_id)
            command.status = status
            db.commit()
        finally:
            db.close()

    def test_pending_command_is_not_retryable(self, client, auth_headers, seed_data):
        mapping_id = _create_mapping(client, auth_headers, seed_data, "R1")
        submit_resp = client.post(
            "/api/products/bulk/platform-map/sync-inventory/submit",
            json={"items": [{"product_platform_map_id": mapping_id, "target_quantity": 3}]},
            headers=auth_headers,
        )
        command_id = submit_resp.json()["items"][0]["command_id"]

        resp = client.post(
            "/api/products/bulk/commands/retry", json={"command_ids": [command_id]}, headers=auth_headers
        )

        assert resp.status_code == 200
        assert resp.json()["items"][0]["outcome"] == "NOT_RETRYABLE"

    def test_failed_command_is_retried_to_pending(self, client, auth_headers, seed_data, api_session_factory):
        mapping_id = _create_mapping(client, auth_headers, seed_data, "R2")
        submit_resp = client.post(
            "/api/products/bulk/platform-map/sync-inventory/submit",
            json={"items": [{"product_platform_map_id": mapping_id, "target_quantity": 3}]},
            headers=auth_headers,
        )
        command_id = submit_resp.json()["items"][0]["command_id"]
        self._set_status(api_session_factory, command_id, "FAILED")

        resp = client.post(
            "/api/products/bulk/commands/retry", json={"command_ids": [command_id]}, headers=auth_headers
        )

        assert resp.status_code == 200
        assert resp.json()["items"][0]["outcome"] == "RETRIED"
        status_resp = client.post(
            "/api/products/bulk/commands/status", json={"command_ids": [command_id]}, headers=auth_headers
        )
        assert status_resp.json()["items"][0]["status"] == "PENDING"

    def test_unknown_command_is_blocked_from_retry(self, client, auth_headers, seed_data, api_session_factory):
        mapping_id = _create_mapping(client, auth_headers, seed_data, "R3")
        submit_resp = client.post(
            "/api/products/bulk/platform-map/sync-inventory/submit",
            json={"items": [{"product_platform_map_id": mapping_id, "target_quantity": 3}]},
            headers=auth_headers,
        )
        command_id = submit_resp.json()["items"][0]["command_id"]
        self._set_status(api_session_factory, command_id, "UNKNOWN")

        resp = client.post(
            "/api/products/bulk/commands/retry", json={"command_ids": [command_id]}, headers=auth_headers
        )

        assert resp.status_code == 200
        assert resp.json()["items"][0]["outcome"] == "UNKNOWN_REQUIRES_RESOLUTION"
        status_resp = client.post(
            "/api/products/bulk/commands/status", json={"command_ids": [command_id]}, headers=auth_headers
        )
        assert status_resp.json()["items"][0]["status"] == "UNKNOWN"
