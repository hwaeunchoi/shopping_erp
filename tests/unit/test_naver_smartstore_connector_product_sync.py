"""
tests/unit/test_naver_smartstore_connector_product_sync.py
------------------------------------------------------------------
NaverSmartstoreConnector의 상용 ERP 확장(3단계, 첫 묶음) - 재고 수량/판매상태
전송을 MockTransport로 검증한다. 요청/응답 스키마는 commerce-api-naver/commerce-api
저장소 docs/2.0.0-RC.js에 포함된 공식 OpenAPI 스펙 파일에서 직접 확인한 값을
그대로 재현한다(2026-09 조회 - apicenter.commerce.naver.com 자체는 이 세션의
WebFetch로 접근할 수 없었으나, 저장소에 공개된 실제 스펙 파일 원문을 파싱해
확인했다):
  - PUT /v1/products/origin-products/{originProductNo}/change-status
    body: {statusType: "SALE"|"OUTOFSTOCK"|"SUSPENSION", stockQuantity?}
  - GET /v2/products/origin-products/{originProductNo} (재고만 바꿀 때 현재
    statusType을 보존하기 위해 먼저 조회) - 응답 {originProduct: {statusType, ...}}
실제 외부 호출은 하지 않는다.
"""

import json

import httpx
import pytest

from integrations.malls.base_mall_connector import SALE_STATUS_ON_SALE, SALE_STATUS_SUSPENDED
from integrations.malls.errors import MarketplaceCapabilityUnsupportedError, MarketplaceCredentialMissingError
from integrations.malls.naver_smartstore_connector import NaverSmartstoreConnector
from services.settings_service import ApiCredentialService

ORIGIN_PRODUCT_NO = "5000000001"
CHANNEL_PRODUCT_NO = "6000000001"  # platform_option_id - 이 API들에는 쓰이지 않는다.


def _register_credentials(db_session, platform):
    svc = ApiCredentialService(db_session)
    svc.upsert_credential("PLATFORM", platform.id, "client_id", "cid")
    svc.upsert_credential("PLATFORM", platform.id, "client_secret", "$2b$12$Cq/28lyv3wDDjELmomd4Me")
    db_session.flush()


def _make_handler(current_status_type: str, change_status_response: dict, captured: list):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
        if request.url.path.endswith(f"/origin-products/{ORIGIN_PRODUCT_NO}"):
            return httpx.Response(200, json={"originProduct": {"statusType": current_status_type}})
        if request.url.path.endswith("/change-status"):
            return httpx.Response(200, json=change_status_response)
        raise AssertionError(f"예상치 못한 요청: {request.url.path}")

    return handler


def _make_handler_with_option_info(
    current_status_type: str, option_info: dict, captured: list, allow_change_status: bool = False
):
    """detailAttribute.optionInfo가 포함된 원상품 응답을 반환한다(옵션 구조에 따른
    재고 전송 차단 검증용). allow_change_status=False(기본값)일 때 change-status가
    호출되면 AssertionError로 막는다 - 차단되어야 하는 경우를 검증하는 테스트용.
    차단되지 않아야 하는(정상 전송) 경우를 검증할 때는 allow_change_status=True로
    호출해 change-status도 정상 응답하도록 한다."""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
        if request.url.path.endswith(f"/origin-products/{ORIGIN_PRODUCT_NO}"):
            return httpx.Response(
                200,
                json={
                    "originProduct": {"statusType": current_status_type, "detailAttribute": {"optionInfo": option_info}}
                },
            )
        if request.url.path.endswith("/change-status"):
            if not allow_change_status:
                raise AssertionError(f"차단되어야 할 요청이 전송됨: {request.url.path}")
            return httpx.Response(200, json={"code": "SUCCESS", "message": "OK"})
        raise AssertionError(f"예상치 못한 요청: {request.url.path}")

    return handler


class TestCapabilityFlags:
    def test_inventory_and_sale_status_update_are_supported(self):
        assert NaverSmartstoreConnector.supports_inventory_update is True
        assert NaverSmartstoreConnector.supports_sale_status_update is True


class TestCredentialMissingFailsClosed:
    def test_update_inventory_without_credentials_raises(self, db_session, platform):
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id)
        with pytest.raises(MarketplaceCredentialMissingError):
            connector.update_inventory(CHANNEL_PRODUCT_NO, 10, platform_origin_product_id=ORIGIN_PRODUCT_NO)

    def test_update_sale_status_without_credentials_raises(self, db_session, platform):
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id)
        with pytest.raises(MarketplaceCredentialMissingError):
            connector.update_sale_status(
                CHANNEL_PRODUCT_NO, SALE_STATUS_ON_SALE, platform_origin_product_id=ORIGIN_PRODUCT_NO
            )


class TestMissingOriginProductId:
    """origin_product_no 없이는(과거 데이터 등) 어느 API도 호출할 수 없다 - 채널
    호출 자체를 하지 않고 명시적으로 차단한다(추측 금지)."""

    def test_update_inventory_without_origin_product_id_blocks_without_http(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        with pytest.raises(MarketplaceCapabilityUnsupportedError):
            connector.update_inventory(CHANNEL_PRODUCT_NO, 10, platform_origin_product_id=None)
        assert captured == []

    def test_update_sale_status_without_origin_product_id_blocks_without_http(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        with pytest.raises(MarketplaceCapabilityUnsupportedError):
            connector.update_sale_status(CHANNEL_PRODUCT_NO, SALE_STATUS_ON_SALE, platform_origin_product_id=None)
        assert captured == []


class TestUpdateInventory:
    def test_preserves_current_status_and_sends_quantity(self, db_session, platform):
        """재고만 바꿀 때는 먼저 현재 statusType을 조회해 그대로 함께 보낸다 -
        change-status가 statusType을 필수로 요구하기 때문에 판매상태를 의도치 않게
        바꾸지 않기 위함이다."""
        _register_credentials(db_session, platform)
        captured: list = []
        handler = _make_handler("SALE", {"code": "SUCCESS", "message": "OK"}, captured)
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        result = connector.update_inventory(CHANNEL_PRODUCT_NO, 25, platform_origin_product_id=ORIGIN_PRODUCT_NO)

        assert result.accepted is True
        get_req = next(r for r in captured if r.method == "GET")
        put_req = next(r for r in captured if r.method == "PUT")
        assert get_req.url.path.endswith(f"/origin-products/{ORIGIN_PRODUCT_NO}")
        assert put_req.url.path.endswith(f"/origin-products/{ORIGIN_PRODUCT_NO}/change-status")
        body = json.loads(put_req.content)
        assert body["statusType"] == "SALE"  # 조회된 현재 상태 그대로 유지.
        assert body["stockQuantity"] == 25

    def test_zero_quantity_is_sent_as_is(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured: list = []
        handler = _make_handler("SALE", {"code": "SUCCESS", "message": "OK"}, captured)
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        result = connector.update_inventory(CHANNEL_PRODUCT_NO, 0, platform_origin_product_id=ORIGIN_PRODUCT_NO)

        assert result.accepted is True
        put_req = next(r for r in captured if r.method == "PUT")
        assert json.loads(put_req.content)["stockQuantity"] == 0

    def test_blocks_when_current_status_is_not_inputable(self, db_session, platform):
        """조회된 현재 상태가 UNADMISSION(승인대기) 등 시스템 상태면, 재고만 바꾸려는
        시도라도 추측으로 SALE/SUSPENSION 중 하나로 강제 전환하지 않고 차단한다."""
        _register_credentials(db_session, platform)
        captured: list = []
        handler = _make_handler("UNADMISSION", {"code": "SUCCESS", "message": "OK"}, captured)
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        with pytest.raises(MarketplaceCapabilityUnsupportedError):
            connector.update_inventory(CHANNEL_PRODUCT_NO, 10, platform_origin_product_id=ORIGIN_PRODUCT_NO)
        assert not any(r.method == "PUT" for r in captured)  # 조회만 하고 전송은 하지 않았다.

    def test_error_response_is_not_accepted(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured: list = []
        handler = _make_handler("SALE", {"code": "ERROR", "message": "실패"}, captured)
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        result = connector.update_inventory(CHANNEL_PRODUCT_NO, 10, platform_origin_product_id=ORIGIN_PRODUCT_NO)

        assert result.accepted is False
        assert result.platform_result_code == "ERROR"


class TestUpdateInventoryBlocksOptionManagedStock:
    """조합형/표준형 옵션 또는 옵션 재고 관리 사용 상품은 원상품 stockQuantity가
    옵션(조합)별 재고의 합으로 자동 계산되므로(공식 GitHub discussion #1194/#562
    확인, 2026-09 조회), 이 ERP가 추측으로 원상품 단위 값을 보내지 않고 명시적으로
    차단하는지 검증한다. 어느 경우든 change-status(PUT)는 절대 호출되지 않는다."""

    def test_blocks_when_option_combinations_present(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured: list = []
        handler = _make_handler_with_option_info("SALE", {"optionCombinations": [{"id": 1}]}, captured)
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        with pytest.raises(MarketplaceCapabilityUnsupportedError):
            connector.update_inventory(CHANNEL_PRODUCT_NO, 10, platform_origin_product_id=ORIGIN_PRODUCT_NO)
        assert not any(r.method == "PUT" for r in captured)

    def test_blocks_when_standard_option_groups_present(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured: list = []
        handler = _make_handler_with_option_info("SALE", {"standardOptionGroups": [{"groupName": "색상"}]}, captured)
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        with pytest.raises(MarketplaceCapabilityUnsupportedError):
            connector.update_inventory(CHANNEL_PRODUCT_NO, 10, platform_origin_product_id=ORIGIN_PRODUCT_NO)
        assert not any(r.method == "PUT" for r in captured)

    def test_blocks_when_use_stock_management_is_true(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured: list = []
        handler = _make_handler_with_option_info("SALE", {"useStockManagement": True}, captured)
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        with pytest.raises(MarketplaceCapabilityUnsupportedError):
            connector.update_inventory(CHANNEL_PRODUCT_NO, 10, platform_origin_product_id=ORIGIN_PRODUCT_NO)
        assert not any(r.method == "PUT" for r in captured)

    def test_does_not_block_simple_option_products(self, db_session, platform):
        """단독형 옵션(optionSimple)만 있는 상품은 원상품 단위 재고 관리 대상이므로
        차단되지 않는다."""
        _register_credentials(db_session, platform)
        captured: list = []
        handler = _make_handler_with_option_info(
            "SALE", {"optionSimple": [{"id": 1}], "useStockManagement": False}, captured, allow_change_status=True
        )
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        result = connector.update_inventory(CHANNEL_PRODUCT_NO, 10, platform_origin_product_id=ORIGIN_PRODUCT_NO)

        assert result.accepted is True


class TestUpdateSaleStatus:
    def test_on_sale_sends_sale_status_without_quantity_lookup(self, db_session, platform):
        """판매상태만 바꿀 때는 현재 상태를 조회하지 않는다(재고는 stockQuantity를
        생략해 현재값을 유지 - 공식 필드 설명 근거)."""
        _register_credentials(db_session, platform)
        captured: list = []
        handler = _make_handler("SUSPENSION", {"code": "SUCCESS", "message": "OK"}, captured)
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        result = connector.update_sale_status(
            CHANNEL_PRODUCT_NO, SALE_STATUS_ON_SALE, platform_origin_product_id=ORIGIN_PRODUCT_NO
        )

        assert result.accepted is True
        assert not any(
            r.url.path.endswith(f"/origin-products/{ORIGIN_PRODUCT_NO}") for r in captured if r.method == "GET"
        )
        put_req = next(r for r in captured if r.method == "PUT")
        body = json.loads(put_req.content)
        assert body["statusType"] == "SALE"
        assert "stockQuantity" not in body

    def test_suspended_sends_suspension_status(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured: list = []
        handler = _make_handler("SALE", {"code": "SUCCESS", "message": "OK"}, captured)
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        result = connector.update_sale_status(
            CHANNEL_PRODUCT_NO, SALE_STATUS_SUSPENDED, platform_origin_product_id=ORIGIN_PRODUCT_NO
        )

        assert result.accepted is True
        put_req = next(r for r in captured if r.method == "PUT")
        assert json.loads(put_req.content)["statusType"] == "SUSPENSION"

    def test_unknown_target_status_raises_value_error(self, db_session, platform):
        _register_credentials(db_session, platform)
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id)
        with pytest.raises(ValueError):
            connector.update_sale_status(CHANNEL_PRODUCT_NO, "OUTOFSTOCK", platform_origin_product_id=ORIGIN_PRODUCT_NO)
