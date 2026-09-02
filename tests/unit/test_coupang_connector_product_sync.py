"""
tests/unit/test_coupang_connector_product_sync.py
------------------------------------------------------
CoupangConnector의 상용 ERP 확장(3단계, 첫 묶음) - 재고 수량/판매상태 전송을
MockTransport로 검증한다. 공식 문서(developers.coupang.com/hc/ko/articles/
360034156253(수량)/360033645154(재개)/360034156313(중지), 2026-09 조회) 기준
경로/응답 형식({code, message})을 그대로 재현한다. 실제 외부 호출은 하지 않는다.
"""

from typing import Any

import httpx
import pytest

from integrations.malls.base_mall_connector import SALE_STATUS_ON_SALE, SALE_STATUS_SUSPENDED
from integrations.malls.coupang_connector import COUPANG_API_BASE, CoupangConnector
from integrations.malls.errors import MarketplaceCredentialMissingError
from services.settings_service import ApiCredentialService

VENDOR_ITEM_ID = "3187044096"


def _register_credentials(db_session, platform):
    svc = ApiCredentialService(db_session)
    svc.upsert_credential("PLATFORM", platform.id, "access_key", "test-access-key")
    svc.upsert_credential("PLATFORM", platform.id, "secret_key", "test-secret-key")
    svc.upsert_credential("PLATFORM", platform.id, "vendor_id", "A00012345")
    db_session.flush()


class TestCapabilityFlags:
    def test_inventory_and_sale_status_update_are_supported(self):
        assert CoupangConnector.supports_inventory_update is True
        assert CoupangConnector.supports_sale_status_update is True


class TestCredentialMissingFailsClosed:
    def test_update_inventory_without_credentials_raises(self, db_session, platform):
        connector = CoupangConnector(session=db_session, platform_id=platform.id)
        with pytest.raises(MarketplaceCredentialMissingError):
            connector.update_inventory(VENDOR_ITEM_ID, 10)

    def test_update_sale_status_without_credentials_raises(self, db_session, platform):
        connector = CoupangConnector(session=db_session, platform_id=platform.id)
        with pytest.raises(MarketplaceCredentialMissingError):
            connector.update_sale_status(VENDOR_ITEM_ID, SALE_STATUS_ON_SALE)


class TestUpdateInventory:
    def test_sends_correct_path_and_no_body(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["path"] = request.url.path
            captured["body"] = request.content
            return httpx.Response(200, json={"code": "SUCCESS", "message": "재고 변경을 완료했습니다."})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        result = connector.update_inventory(VENDOR_ITEM_ID, 25)

        assert result.accepted is True
        assert result.platform_result_code == "SUCCESS"
        assert captured["method"] == "PUT"
        assert (
            captured["path"]
            == f"/v2/providers/seller_api/apis/api/v1/marketplace/vendor-items/{VENDOR_ITEM_ID}/quantities/25"
        )
        assert captured["body"] == b""  # 요청 바디 없음(공식 문서 확인).

    def test_zero_quantity_is_sent_as_is(self, db_session, platform):
        """수량 0은 계약 위반이 아니라 정상 값이다(품절 처리) - 별도 취급 없이 그대로 전송."""
        _register_credentials(db_session, platform)
        captured_path = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_path["path"] = request.url.path
            return httpx.Response(200, json={"code": "SUCCESS", "message": "OK"})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        result = connector.update_inventory(VENDOR_ITEM_ID, 0)

        assert result.accepted is True
        assert captured_path["path"].endswith("/quantities/0")

    def test_error_response_is_not_accepted(self, db_session, platform):
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": "ERROR", "message": "vendoritemid not found"})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        result = connector.update_inventory(VENDOR_ITEM_ID, 10)

        assert result.accepted is False
        assert result.platform_result_code == "ERROR"


class TestUpdateSaleStatus:
    def test_on_sale_calls_resume_endpoint(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured_path = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_path["path"] = request.url.path
            return httpx.Response(200, json={"code": "SUCCESS", "message": "Sales resumed."})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        result = connector.update_sale_status(VENDOR_ITEM_ID, SALE_STATUS_ON_SALE)

        assert result.accepted is True
        assert captured_path["path"].endswith(f"/vendor-items/{VENDOR_ITEM_ID}/sales/resume")

    def test_suspended_calls_stop_endpoint(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured_path = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_path["path"] = request.url.path
            return httpx.Response(200, json={"code": "SUCCESS", "message": "Sale has been suspended."})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        result = connector.update_sale_status(VENDOR_ITEM_ID, SALE_STATUS_SUSPENDED)

        assert result.accepted is True
        assert captured_path["path"].endswith(f"/vendor-items/{VENDOR_ITEM_ID}/sales/stop")

    def test_unknown_target_status_raises_value_error(self, db_session, platform):
        _register_credentials(db_session, platform)
        connector = CoupangConnector(session=db_session, platform_id=platform.id)
        with pytest.raises(ValueError):
            connector.update_sale_status(VENDOR_ITEM_ID, "OUTOFSTOCK")
