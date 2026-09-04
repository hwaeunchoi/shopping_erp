"""
tests/integration/test_api_products_publish.py
------------------------------------------------
api/routers/products.py의 상용 ERP 확장(3단계, 두 번째 묶음) - 단순 상품 신규
등록/제한된 정보수정 엔드포인트 통합 테스트: 초안 저장/조회, 등록 접수(202)/
중복접수/이미 등록됨(409), 정보수정 접수, 기본 차단(OFF).

실제 채널 HTTP 호출(execute_command)은 scheduler.jobs.product_publish_dispatch_job이
수행하며 여기서는 검증하지 않는다(services/tests/unit/test_product_publish_service.py,
test_naver_smartstore_connector_publish.py, test_coupang_connector_publish.py에서
MockTransport/스텁 기반으로 이미 검증했다) - 이 통합 테스트는 API 계약(202/404/
409/400/idempotent/기본 차단)만 확인한다.
"""

import pytest

from config.settings import settings


@pytest.fixture(autouse=True)
def _enable_product_publish(monkeypatch):
    # 신규 등록(submit)은 product_publish_enabled, 정보수정(update-info)은 완전히
    # 독립된 별도 플래그 product_info_update_enabled로 통제한다(services.
    # product_sync_dispatch_service._enqueue 참고 - command_type별로 다른 플래그를
    # 본다). 이 파일의 나머지 테스트는 "두 기능 모두 켜져 있을 때"를 전제하므로
    # 함께 켠다 - 독립성 자체는 TestUpdatePlatformMapInfo.
    # test_publish_flag_alone_does_not_enable_info_update가 별도로 검증한다.
    monkeypatch.setattr(settings, "product_publish_enabled", True)
    monkeypatch.setattr(settings, "product_info_update_enabled", True)


def _create_option(client, auth_headers, sku_suffix: str) -> tuple[int, int]:
    product_id = client.post(
        "/api/products", json={"name": f"등록 테스트 상품 {sku_suffix}"}, headers=auth_headers
    ).json()["id"]
    option_id = client.post(
        f"/api/products/{product_id}/options", json={"sku_code": f"PUB-SKU-{sku_suffix}"}, headers=auth_headers
    ).json()["id"]
    return product_id, option_id


class TestSaveAndGetDraft:
    def test_save_partial_draft_then_get_it_back(self, client, auth_headers, seed_data):
        _, option_id = _create_option(client, auth_headers, "1")

        resp = client.post(
            f"/api/products/options/{option_id}/publish-draft",
            json={"platform_id": seed_data["platform_id"], "name": "초안 상품명"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        draft = resp.json()
        assert draft["name"] == "초안 상품명"
        assert draft["sale_price"] is None

        got = client.get(
            f"/api/products/options/{option_id}/publish-draft/{seed_data['platform_id']}", headers=auth_headers
        )
        assert got.status_code == 200
        assert got.json()["id"] == draft["id"]

    def test_rejects_file_scheme_image_url(self, client, auth_headers, seed_data):
        _, option_id = _create_option(client, auth_headers, "2")

        resp = client.post(
            f"/api/products/options/{option_id}/publish-draft",
            json={"platform_id": seed_data["platform_id"], "image_urls": ["file:///etc/passwd"]},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_get_missing_draft_returns_404(self, client, auth_headers, seed_data):
        resp = client.get(
            f"/api/products/options/999999/publish-draft/{seed_data['platform_id']}", headers=auth_headers
        )
        assert resp.status_code == 404


def _etc_channel_fields(notice_type: str = "ETC") -> dict:
    return {
        "productInfoProvidedNotice": {
            "productInfoProvidedNoticeType": notice_type,
            "categoryNoticeTypeConfirmedByOperator": False,
            "etc": {},
        }
    }


class TestConfirmEtcNotice:
    """POST .../publish-drafts/{id}/confirm-etc-notice - 네이버 ETC 카테고리
    적합성 "운영자 확인" 기록 API(services.product_publish_service.
    ProductPublishService.confirm_etc_notice 참고)."""

    def test_confirm_returns_confirmation_fields(self, client, auth_headers, seed_data):
        _, option_id = _create_option(client, auth_headers, "etc-1")
        draft_id = client.post(
            f"/api/products/options/{option_id}/publish-draft",
            json={
                "platform_id": seed_data["platform_id"],
                "category_code": "50000803",
                "channel_fields": _etc_channel_fields(),
            },
            headers=auth_headers,
        ).json()["id"]

        resp = client.post(f"/api/products/publish-drafts/{draft_id}/confirm-etc-notice", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["etc_notice_confirmed_at"] is not None
        assert body["etc_notice_confirmed_category_code"] == "50000803"
        assert body["etc_notice_confirmed_notice_type"] == "ETC"
        assert body["etc_notice_confirmation_valid"] is True

    def test_confirm_rejects_when_notice_type_not_etc(self, client, auth_headers, seed_data):
        _, option_id = _create_option(client, auth_headers, "etc-2")
        draft_id = client.post(
            f"/api/products/options/{option_id}/publish-draft",
            json={
                "platform_id": seed_data["platform_id"],
                "category_code": "50000803",
                "channel_fields": _etc_channel_fields(notice_type="TOGETHER"),
            },
            headers=auth_headers,
        ).json()["id"]

        resp = client.post(f"/api/products/publish-drafts/{draft_id}/confirm-etc-notice", headers=auth_headers)
        assert resp.status_code == 400

    def test_confirm_missing_draft_returns_404(self, client, auth_headers):
        resp = client.post("/api/products/publish-drafts/999999/confirm-etc-notice", headers=auth_headers)
        assert resp.status_code == 404

    def test_changing_category_code_invalidates_confirmation_via_api(self, client, auth_headers, seed_data):
        _, option_id = _create_option(client, auth_headers, "etc-3")
        draft_id = client.post(
            f"/api/products/options/{option_id}/publish-draft",
            json={
                "platform_id": seed_data["platform_id"],
                "category_code": "50000803",
                "channel_fields": _etc_channel_fields(),
            },
            headers=auth_headers,
        ).json()["id"]
        client.post(f"/api/products/publish-drafts/{draft_id}/confirm-etc-notice", headers=auth_headers)

        updated = client.post(
            f"/api/products/options/{option_id}/publish-draft",
            json={"platform_id": seed_data["platform_id"], "category_code": "50000999"},
            headers=auth_headers,
        ).json()
        assert updated["etc_notice_confirmed_at"] is None
        assert updated["etc_notice_confirmation_valid"] is False


class TestSubmitDraft:
    def test_submit_returns_202_with_pending_command(self, client, auth_headers, seed_data):
        _, option_id = _create_option(client, auth_headers, "3")
        draft_id = client.post(
            f"/api/products/options/{option_id}/publish-draft",
            json={"platform_id": seed_data["platform_id"], "name": "등록상품"},
            headers=auth_headers,
        ).json()["id"]

        resp = client.post(f"/api/products/publish-drafts/{draft_id}/submit", headers=auth_headers)

        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "PENDING"
        assert body["already_processed"] is False

    def test_duplicate_submit_reuses_same_command(self, client, auth_headers, seed_data):
        _, option_id = _create_option(client, auth_headers, "4")
        draft_id = client.post(
            f"/api/products/options/{option_id}/publish-draft",
            json={"platform_id": seed_data["platform_id"], "name": "등록상품"},
            headers=auth_headers,
        ).json()["id"]

        first = client.post(f"/api/products/publish-drafts/{draft_id}/submit", headers=auth_headers).json()
        second = client.post(f"/api/products/publish-drafts/{draft_id}/submit", headers=auth_headers).json()

        assert first["command_id"] == second["command_id"]

    def test_submit_missing_draft_returns_404(self, client, auth_headers):
        resp = client.post("/api/products/publish-drafts/999999/submit", headers=auth_headers)
        assert resp.status_code == 404

    def test_submit_already_registered_returns_409(self, client, auth_headers, seed_data):
        _, option_id = _create_option(client, auth_headers, "5")
        client.post(
            f"/api/products/options/{option_id}/platform-map",
            json={"platform_id": seed_data["platform_id"], "platform_option_id": "ALREADY-REGISTERED-1"},
            headers=auth_headers,
        )
        draft_id = client.post(
            f"/api/products/options/{option_id}/publish-draft",
            json={"platform_id": seed_data["platform_id"], "name": "등록상품"},
            headers=auth_headers,
        ).json()["id"]

        resp = client.post(f"/api/products/publish-drafts/{draft_id}/submit", headers=auth_headers)

        assert resp.status_code == 409

    def test_submit_disabled_by_default_returns_503(self, client, auth_headers, seed_data, monkeypatch):
        monkeypatch.setattr(settings, "product_publish_enabled", False)
        _, option_id = _create_option(client, auth_headers, "6")
        draft_id = client.post(
            f"/api/products/options/{option_id}/publish-draft",
            json={"platform_id": seed_data["platform_id"], "name": "등록상품"},
            headers=auth_headers,
        ).json()["id"]

        resp = client.post(f"/api/products/publish-drafts/{draft_id}/submit", headers=auth_headers)

        assert resp.status_code == 503


class TestSyncCommandPollingCoversPublishCommands:
    def test_publish_command_is_pollable_via_shared_sync_commands_endpoint(self, client, auth_headers, seed_data):
        _, option_id = _create_option(client, auth_headers, "7")
        draft_id = client.post(
            f"/api/products/options/{option_id}/publish-draft",
            json={"platform_id": seed_data["platform_id"], "name": "등록상품"},
            headers=auth_headers,
        ).json()["id"]
        command_id = client.post(f"/api/products/publish-drafts/{draft_id}/submit", headers=auth_headers).json()[
            "command_id"
        ]

        resp = client.get(f"/api/products/sync-commands/{command_id}", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json()["status"] == "PENDING"


class TestUpdatePlatformMapInfo:
    def _create_mapping(self, client, auth_headers, seed_data, sku_suffix: str) -> int:
        _, option_id = _create_option(client, auth_headers, sku_suffix)
        mapping = client.post(
            f"/api/products/options/{option_id}/platform-map",
            json={"platform_id": seed_data["platform_id"], "platform_option_id": f"INFO-EXT-{sku_suffix}"},
            headers=auth_headers,
        ).json()
        return mapping["id"]

    def test_returns_202_with_pending_command(self, client, auth_headers, seed_data):
        mapping_id = self._create_mapping(client, auth_headers, seed_data, "8")

        resp = client.post(
            f"/api/products/platform-map/{mapping_id}/update-info", json={"name": "새 상품명"}, headers=auth_headers
        )

        assert resp.status_code == 202
        assert resp.json()["status"] == "PENDING"

    def test_no_fields_returns_400(self, client, auth_headers, seed_data):
        mapping_id = self._create_mapping(client, auth_headers, seed_data, "9")

        resp = client.post(f"/api/products/platform-map/{mapping_id}/update-info", json={}, headers=auth_headers)

        assert resp.status_code == 400

    def test_missing_mapping_returns_404(self, client, auth_headers):
        resp = client.post("/api/products/platform-map/999999/update-info", json={"name": "x"}, headers=auth_headers)
        assert resp.status_code == 404

    def test_disabled_by_default_returns_503(self, client, auth_headers, seed_data, monkeypatch):
        monkeypatch.setattr(settings, "product_info_update_enabled", False)
        mapping_id = self._create_mapping(client, auth_headers, seed_data, "10")

        resp = client.post(
            f"/api/products/platform-map/{mapping_id}/update-info", json={"name": "새이름"}, headers=auth_headers
        )

        assert resp.status_code == 503

    def test_publish_flag_alone_does_not_enable_info_update(self, client, auth_headers, seed_data, monkeypatch):
        """신규 등록 플래그(product_publish_enabled)만 켜져 있고 정보수정 전용
        플래그(product_info_update_enabled)가 꺼져 있으면, 정보수정 요청은 여전히
        차단돼야 한다(감사 지적: "신규 등록 플래그와도 독립적으로 검증")."""
        monkeypatch.setattr(settings, "product_info_update_enabled", False)
        monkeypatch.setattr(settings, "product_publish_enabled", True)
        mapping_id = self._create_mapping(client, auth_headers, seed_data, "11")

        resp = client.post(
            f"/api/products/platform-map/{mapping_id}/update-info", json={"name": "새이름"}, headers=auth_headers
        )

        assert resp.status_code == 503
