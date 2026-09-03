"""
tests/unit/test_naver_smartstore_connector_publish.py
------------------------------------------------------------------
NaverSmartstoreConnector의 상용 ERP 확장(3단계, 두 번째 묶음) - 옵션 조합 없는
단순 상품 신규 등록(create_product)과 제한된 정보 수정(update_product_info)을
MockTransport로 검증한다. 요청/응답 스키마는 commerce-api-naver/commerce-api
저장소 docs/2.0.0-RC.js에 포함된 공식 OpenAPI 스펙 파일에서 직접 확인한 값을
그대로 재현한다(2026-09 조회). 실제 외부 호출은 하지 않는다.
"""

import json

import httpx
import pytest

from integrations.malls.errors import MarketplaceCapabilityUnsupportedError, MarketplaceCredentialMissingError
from integrations.malls.naver_smartstore_connector import CREATE_PRODUCT_PATH, NaverSmartstoreConnector
from services.settings_service import ApiCredentialService

ORIGIN_PRODUCT_NO = "5000000001"
CHANNEL_PRODUCT_NO = "6000000001"


def _register_credentials(db_session, platform):
    svc = ApiCredentialService(db_session)
    svc.upsert_credential("PLATFORM", platform.id, "client_id", "cid")
    svc.upsert_credential("PLATFORM", platform.id, "client_secret", "$2b$12$Cq/28lyv3wDDjELmomd4Me")
    db_session.flush()


def _valid_channel_fields() -> dict:
    return {
        "minorPurchasable": False,
        "naverShoppingRegistration": True,
        "channelProductDisplayStatusType": "ON",
        "originAreaInfo": {"originAreaCode": "0200037"},
        "afterServiceInfo": {
            "afterServiceTelephoneNumber": "02-1234-5678",
            "afterServiceGuideContent": "평일 09:00~18:00 유선 문의",
        },
        "deliveryInfo": {
            "deliveryType": "DELIVERY",
            "deliveryAttributeType": "NORMAL",
            "deliveryFee": {"deliveryFeeType": "FREE"},
            "claimDeliveryInfo": {"returnDeliveryFee": 3000, "exchangeDeliveryFee": 6000},
        },
        "productInfoProvidedNotice": {
            "productInfoProvidedNoticeType": "ETC",
            "etc": {
                "itemName": "테스트상품",
                "manufacturer": "테스트제조사",
                "modelName": "MODEL-001",
                "qualityAssuranceStandard": "품질보증기준 안내",
                "compensationProcedure": "피해보상 절차 안내",
                "troubleShootingContents": "소비자상담 안내",
                "noRefundReason": "환불 불가 사유 없음",
                "returnCostReason": "반품비용 안내",
            },
        },
    }


def _valid_draft_snapshot() -> dict:
    return {
        "name": "테스트 등록 상품",
        "sale_price": 19900,
        "description_html": "<p>상세설명</p>",
        "category_code": "50000803",
        "image_urls": ["https://img.example.com/main.jpg", "https://img.example.com/sub1.jpg"],
        "stock_quantity": 100,
        "channel_fields": _valid_channel_fields(),
    }


def _make_create_handler(response_body: dict, captured: list):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
        if request.url.path == CREATE_PRODUCT_PATH:
            return httpx.Response(200, json=response_body)
        raise AssertionError(f"예상치 못한 요청: {request.url.path}")

    return handler


class TestCapabilityFlags:
    def test_product_create_and_info_update_are_supported(self):
        assert NaverSmartstoreConnector.supports_product_create is True
        assert NaverSmartstoreConnector.supports_product_info_update is True


class TestCreateProduct:
    def test_sends_official_request_and_parses_response(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured: list = []
        handler = _make_create_handler(
            {"originProductNo": int(ORIGIN_PRODUCT_NO), "smartstoreChannelProductNo": int(CHANNEL_PRODUCT_NO)}, captured
        )
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        result = connector.create_product(_valid_draft_snapshot())

        assert result.accepted is True
        assert result.channel_product_id == ORIGIN_PRODUCT_NO
        assert result.channel_option_id == CHANNEL_PRODUCT_NO
        post_req = next(r for r in captured if r.method == "POST" and r.url.path == CREATE_PRODUCT_PATH)
        body = json.loads(post_req.content)
        assert body["originProduct"]["statusType"] == "SALE"
        assert body["originProduct"]["name"] == "테스트 등록 상품"
        assert body["originProduct"]["images"]["representativeImage"]["url"] == "https://img.example.com/main.jpg"
        assert body["originProduct"]["images"]["optionalImages"] == [{"url": "https://img.example.com/sub1.jpg"}]
        assert (
            body["originProduct"]["detailAttribute"]["afterServiceInfo"]["afterServiceTelephoneNumber"]
            == "02-1234-5678"
        )
        assert body["smartstoreChannelProduct"]["channelProductDisplayStatusType"] == "ON"
        assert body["smartstoreChannelProduct"]["naverShoppingRegistration"] is True

    def test_single_image_omits_optional_images(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured: list = []
        handler = _make_create_handler(
            {"originProductNo": int(ORIGIN_PRODUCT_NO), "smartstoreChannelProductNo": int(CHANNEL_PRODUCT_NO)}, captured
        )
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)
        snapshot = _valid_draft_snapshot()
        snapshot["image_urls"] = ["https://img.example.com/main.jpg"]

        connector.create_product(snapshot)

        post_req = next(r for r in captured if r.method == "POST" and r.url.path == CREATE_PRODUCT_PATH)
        body = json.loads(post_req.content)
        assert "optionalImages" not in body["originProduct"]["images"]

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda s: s.update(name=None),
            lambda s: s.update(sale_price=None),
            lambda s: s.update(description_html=None),
            lambda s: s.update(category_code=None),
            lambda s: s.update(image_urls=[]),
            lambda s: s.update(stock_quantity=None),
        ],
    )
    def test_blocks_when_core_field_missing_without_http_call(self, db_session, platform, mutate):
        _register_credentials(db_session, platform)
        captured: list = []
        handler = _make_create_handler({}, captured)
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)
        snapshot = _valid_draft_snapshot()
        mutate(snapshot)

        with pytest.raises(ValueError):
            connector.create_product(snapshot)
        assert captured == []

    def test_blocks_when_after_service_info_missing_without_http_call(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured: list = []
        handler = _make_create_handler({}, captured)
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)
        snapshot = _valid_draft_snapshot()
        del snapshot["channel_fields"]["afterServiceInfo"]["afterServiceGuideContent"]

        with pytest.raises(ValueError, match="afterServiceInfo"):
            connector.create_product(snapshot)
        assert captured == []

    def test_blocks_unsupported_notice_type_without_http_call(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured: list = []
        handler = _make_create_handler({}, captured)
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)
        snapshot = _valid_draft_snapshot()
        snapshot["channel_fields"]["productInfoProvidedNotice"] = {"productInfoProvidedNoticeType": "FOOD"}

        with pytest.raises(ValueError, match="ETC"):
            connector.create_product(snapshot)
        assert captured == []

    def test_blocks_when_credentials_missing(self, db_session, platform):
        with pytest.raises(MarketplaceCredentialMissingError):
            NaverSmartstoreConnector(session=db_session, platform_id=platform.id).create_product(
                _valid_draft_snapshot()
            )

    def test_parse_failed_when_response_missing_identifiers(self, db_session, platform):
        from integrations.malls.errors import MarketplaceExternalAPIError

        _register_credentials(db_session, platform)
        captured: list = []
        handler = _make_create_handler({"originProductNo": None}, captured)
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        with pytest.raises(MarketplaceExternalAPIError):
            connector.create_product(_valid_draft_snapshot())


def _full_origin_product() -> dict:
    return {
        "statusType": "SALE",
        "name": "기존 상품명",
        "salePrice": 10000,
        "detailContent": "<p>기존 설명</p>",
        "stockQuantity": 50,
        "leafCategoryId": "50000803",
        "images": {"representativeImage": {"url": "https://img.example.com/existing.jpg"}},
        "deliveryInfo": {"deliveryType": "DELIVERY", "deliveryFee": {"deliveryFeeType": "FREE"}},
        "detailAttribute": {"originAreaInfo": {"originAreaCode": "0200037"}},
    }


def _make_update_handler(current_status_type: str, captured: list, put_response: "dict | None" = None):
    origin_product = _full_origin_product()
    origin_product["statusType"] = current_status_type

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
        if request.method == "GET" and request.url.path.endswith(f"/origin-products/{ORIGIN_PRODUCT_NO}"):
            return httpx.Response(
                200,
                json={
                    "originProduct": origin_product,
                    "smartstoreChannelProduct": {"channelProductDisplayStatusType": "ON"},
                },
            )
        if request.method == "PUT" and request.url.path.endswith(f"/origin-products/{ORIGIN_PRODUCT_NO}"):
            return httpx.Response(
                200, json=put_response or {"originProductNo": int(ORIGIN_PRODUCT_NO), "smartstoreChannelProductNo": 1}
            )
        raise AssertionError(f"예상치 못한 요청: {request.url.path}")

    return handler


class TestUpdateProductInfo:
    def test_updates_only_requested_fields_and_preserves_the_rest(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured: list = []
        handler = _make_update_handler("SALE", captured)
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        result = connector.update_product_info(
            CHANNEL_PRODUCT_NO, ORIGIN_PRODUCT_NO, name="새 상품명", sale_price=None, description=None
        )

        assert result.accepted is True
        put_req = next(r for r in captured if r.method == "PUT")
        body = json.loads(put_req.content)
        assert body["originProduct"]["name"] == "새 상품명"
        # 바꾸지 않은 필드는 GET에서 받은 값 그대로 보존되어야 한다.
        assert body["originProduct"]["salePrice"] == 10000
        assert body["originProduct"]["detailContent"] == "<p>기존 설명</p>"
        assert body["originProduct"]["images"] == {
            "representativeImage": {"url": "https://img.example.com/existing.jpg"}
        }
        assert body["originProduct"]["deliveryInfo"] == {
            "deliveryType": "DELIVERY",
            "deliveryFee": {"deliveryFeeType": "FREE"},
        }
        assert body["smartstoreChannelProduct"] == {"channelProductDisplayStatusType": "ON"}

    def test_updates_price_and_description_only(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured: list = []
        handler = _make_update_handler("SALE", captured)
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        connector.update_product_info(
            CHANNEL_PRODUCT_NO, ORIGIN_PRODUCT_NO, sale_price=29900, description="<p>새 설명</p>"
        )

        put_req = next(r for r in captured if r.method == "PUT")
        body = json.loads(put_req.content)
        assert body["originProduct"]["name"] == "기존 상품명"  # 요청에 없었으니 그대로.
        assert body["originProduct"]["salePrice"] == 29900
        assert body["originProduct"]["detailContent"] == "<p>새 설명</p>"

    def test_requires_at_least_one_field(self, db_session, platform):
        _register_credentials(db_session, platform)
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id)
        with pytest.raises(ValueError):
            connector.update_product_info(CHANNEL_PRODUCT_NO, ORIGIN_PRODUCT_NO)

    def test_blocks_without_origin_product_id(self, db_session, platform):
        _register_credentials(db_session, platform)
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id)
        with pytest.raises(MarketplaceCapabilityUnsupportedError):
            connector.update_product_info(CHANNEL_PRODUCT_NO, None, name="새 이름")


class TestFetchRegistrationStatus:
    def test_returns_current_status_type(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured: list = []
        handler = _make_update_handler("SALE", captured)
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        result = connector.fetch_registration_status(ORIGIN_PRODUCT_NO)

        assert result == {"status_name": "SALE", "channel_option_ids": []}
