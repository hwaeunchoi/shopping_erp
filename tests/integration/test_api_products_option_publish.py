"""
tests/integration/test_api_products_option_publish.py
------------------------------------------------------
api/routers/products.py의 상용 ERP 확장(3단계, 세 번째 묶음) - 옵션조합 상품
신규 등록 엔드포인트 통합 테스트: 상품 레벨 초안 저장/조회, 품목(SKU) 추가/삭제,
등록 접수(202)/중복접수/이미 등록됨(409)/품목 없음(400)/기본 차단(OFF).

실제 채널 HTTP 호출(execute_command, check_registration_status)은 여기서
검증하지 않는다(services/tests/unit/test_product_option_publish_service.py,
test_naver_smartstore_connector_option_publish.py,
test_coupang_connector_option_publish.py에서 스텁/MockTransport 기반으로 이미
검증했다 - test_api_products_publish.py와 동일한 테스트 범위 방침). 이 통합
테스트는 API 계약(202/404/409/400/idempotent/기본 차단)만 확인한다.
"""

import pytest

from config.settings import settings


@pytest.fixture(autouse=True)
def _enable_product_option_publish(monkeypatch):
    monkeypatch.setattr(settings, "product_option_publish_enabled", True)


def _create_product_with_two_options(client, auth_headers, suffix: str) -> tuple[int, int, int]:
    product_id = client.post(
        "/api/products", json={"name": f"옵션조합 등록 테스트 상품 {suffix}"}, headers=auth_headers
    ).json()["id"]
    option1_id = client.post(
        f"/api/products/{product_id}/options", json={"sku_code": f"OPT-PUB-SKU-{suffix}-A"}, headers=auth_headers
    ).json()["id"]
    option2_id = client.post(
        f"/api/products/{product_id}/options", json={"sku_code": f"OPT-PUB-SKU-{suffix}-B"}, headers=auth_headers
    ).json()["id"]
    return product_id, option1_id, option2_id


def _add_item(client, auth_headers, draft_id: int, option_id: int, color: str, price: float, stock: int) -> dict:
    resp = client.post(
        f"/api/products/option-publish-drafts/{draft_id}/items",
        json={
            "product_option_id": option_id,
            "option_values": [["색상", color]],
            "sale_price": price,
            "stock_quantity": stock,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestSaveDraftAndItems:
    def test_save_partial_draft_then_get_it_back(self, client, auth_headers, seed_data):
        product_id, _, _ = _create_product_with_two_options(client, auth_headers, "1")

        resp = client.post(
            f"/api/products/{product_id}/option-publish-draft",
            json={"platform_id": seed_data["platform_id"], "name": "초안 상품명", "base_sale_price": 20000},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        draft = resp.json()
        assert draft["name"] == "초안 상품명"
        assert draft["items"] == []

        got = client.get(
            f"/api/products/{product_id}/option-publish-draft/{seed_data['platform_id']}", headers=auth_headers
        )
        assert got.status_code == 200
        assert got.json()["id"] == draft["id"]

    def test_get_missing_draft_returns_404(self, client, auth_headers, seed_data):
        resp = client.get(f"/api/products/999999/option-publish-draft/{seed_data['platform_id']}", headers=auth_headers)
        assert resp.status_code == 404

    def test_rejects_file_scheme_image_url(self, client, auth_headers, seed_data):
        product_id, _, _ = _create_product_with_two_options(client, auth_headers, "2")

        resp = client.post(
            f"/api/products/{product_id}/option-publish-draft",
            json={"platform_id": seed_data["platform_id"], "image_urls": ["file:///etc/passwd"]},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_add_item_defaults_seller_product_code_to_sku_code(self, client, auth_headers, seed_data):
        product_id, option1_id, _ = _create_product_with_two_options(client, auth_headers, "3")
        draft_id = client.post(
            f"/api/products/{product_id}/option-publish-draft",
            json={"platform_id": seed_data["platform_id"]},
            headers=auth_headers,
        ).json()["id"]

        item = _add_item(client, auth_headers, draft_id, option1_id, "블랙", 20000, 10)

        assert item["seller_product_code"] == "OPT-PUB-SKU-3-A"
        assert item["option_values"] == [["색상", "블랙"]]

    def test_add_item_from_another_product_returns_400(self, client, auth_headers, seed_data):
        product_id, _, _ = _create_product_with_two_options(client, auth_headers, "4")
        _, other_option_id, _ = _create_product_with_two_options(client, auth_headers, "4b")
        draft_id = client.post(
            f"/api/products/{product_id}/option-publish-draft",
            json={"platform_id": seed_data["platform_id"]},
            headers=auth_headers,
        ).json()["id"]

        resp = client.post(
            f"/api/products/option-publish-drafts/{draft_id}/items",
            json={"product_option_id": other_option_id},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_delete_item(self, client, auth_headers, seed_data):
        product_id, option1_id, _ = _create_product_with_two_options(client, auth_headers, "5")
        draft_id = client.post(
            f"/api/products/{product_id}/option-publish-draft",
            json={"platform_id": seed_data["platform_id"]},
            headers=auth_headers,
        ).json()["id"]
        item = _add_item(client, auth_headers, draft_id, option1_id, "블랙", 20000, 10)

        resp = client.delete(f"/api/products/option-publish-drafts/{draft_id}/items/{item['id']}", headers=auth_headers)
        assert resp.status_code == 204

        resp2 = client.delete(
            f"/api/products/option-publish-drafts/{draft_id}/items/{item['id']}", headers=auth_headers
        )
        assert resp2.status_code == 404


class TestSubmitDraft:
    def _draft_with_two_items(self, client, auth_headers, seed_data, suffix: str) -> int:
        product_id, option1_id, option2_id = _create_product_with_two_options(client, auth_headers, suffix)
        draft_id = client.post(
            f"/api/products/{product_id}/option-publish-draft",
            json={"platform_id": seed_data["platform_id"], "name": "옵션조합 등록상품", "base_sale_price": 20000},
            headers=auth_headers,
        ).json()["id"]
        _add_item(client, auth_headers, draft_id, option1_id, "블랙", 20000, 10)
        _add_item(client, auth_headers, draft_id, option2_id, "화이트", 21000, 5)
        return draft_id

    def test_submit_returns_202_with_pending_command(self, client, auth_headers, seed_data):
        draft_id = self._draft_with_two_items(client, auth_headers, seed_data, "6")

        resp = client.post(f"/api/products/option-publish-drafts/{draft_id}/submit", headers=auth_headers)

        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "PENDING"
        assert body["already_processed"] is False

    def test_duplicate_submit_reuses_same_command(self, client, auth_headers, seed_data):
        draft_id = self._draft_with_two_items(client, auth_headers, seed_data, "7")

        first = client.post(f"/api/products/option-publish-drafts/{draft_id}/submit", headers=auth_headers).json()
        second = client.post(f"/api/products/option-publish-drafts/{draft_id}/submit", headers=auth_headers).json()

        assert first["command_id"] == second["command_id"]

    def test_submit_missing_draft_returns_404(self, client, auth_headers):
        resp = client.post("/api/products/option-publish-drafts/999999/submit", headers=auth_headers)
        assert resp.status_code == 404

    def test_submit_without_items_returns_400(self, client, auth_headers, seed_data):
        product_id, _, _ = _create_product_with_two_options(client, auth_headers, "8")
        draft_id = client.post(
            f"/api/products/{product_id}/option-publish-draft",
            json={"platform_id": seed_data["platform_id"], "name": "옵션조합 등록상품"},
            headers=auth_headers,
        ).json()["id"]

        resp = client.post(f"/api/products/option-publish-drafts/{draft_id}/submit", headers=auth_headers)
        assert resp.status_code == 400

    def test_submit_already_registered_returns_409(self, client, auth_headers, seed_data):
        product_id, option1_id, option2_id = _create_product_with_two_options(client, auth_headers, "9")
        client.post(
            f"/api/products/options/{option1_id}/platform-map",
            json={"platform_id": seed_data["platform_id"], "platform_option_id": "ALREADY-REGISTERED-OPT-1"},
            headers=auth_headers,
        )
        draft_id = client.post(
            f"/api/products/{product_id}/option-publish-draft",
            json={"platform_id": seed_data["platform_id"], "name": "옵션조합 등록상품", "base_sale_price": 20000},
            headers=auth_headers,
        ).json()["id"]
        _add_item(client, auth_headers, draft_id, option1_id, "블랙", 20000, 10)
        _add_item(client, auth_headers, draft_id, option2_id, "화이트", 21000, 5)

        resp = client.post(f"/api/products/option-publish-drafts/{draft_id}/submit", headers=auth_headers)

        assert resp.status_code == 409

    def test_submit_disabled_by_default_returns_503(self, client, auth_headers, seed_data, monkeypatch):
        monkeypatch.setattr(settings, "product_option_publish_enabled", False)
        draft_id = self._draft_with_two_items(client, auth_headers, seed_data, "10")

        resp = client.post(f"/api/products/option-publish-drafts/{draft_id}/submit", headers=auth_headers)

        assert resp.status_code == 503


class TestSyncCommandPollingCoversOptionPublishCommands:
    def test_command_is_pollable_via_shared_sync_commands_endpoint(self, client, auth_headers, seed_data):
        draft_id = TestSubmitDraft()._draft_with_two_items(client, auth_headers, seed_data, "11")
        command_id = client.post(f"/api/products/option-publish-drafts/{draft_id}/submit", headers=auth_headers).json()[
            "command_id"
        ]

        resp = client.get(f"/api/products/sync-commands/{command_id}", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json()["status"] == "PENDING"
