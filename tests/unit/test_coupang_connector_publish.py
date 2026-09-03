"""
tests/unit/test_coupang_connector_publish.py
------------------------------------------------------------------
CoupangConnector의 상용 ERP 확장(3단계, 두 번째 묶음) - 옵션 조합 없는 단순 상품
신규 등록 접수(create_product)와 카테고리 메타정보 기반 필수값 차단
(fetch_category_requirements)을 MockTransport로 검증한다. 등록 응답이 옵션
단위 식별자(vendorItemId)를 돌려주지 않는다는 확인된 계약(모듈 docstring 참고)에
따라 이 채널은 채널_option_id를 절대 채우지 않는다는 것도 함께 검증한다.
실제 외부 호출은 하지 않는다.
"""

import json

import httpx
import pytest

from integrations.malls.coupang_connector import CATEGORY_METADATA_PATH_TMPL, CREATE_PRODUCT_PATH, CoupangConnector
from integrations.malls.errors import MarketplaceCredentialMissingError
from services.settings_service import ApiCredentialService

CATEGORY_CODE = "56137"
SELLER_PRODUCT_ID = 427011919


def _register_credentials(db_session, platform):
    svc = ApiCredentialService(db_session)
    svc.upsert_credential("PLATFORM", platform.id, "access_key", "ak")
    svc.upsert_credential("PLATFORM", platform.id, "secret_key", "sk")
    svc.upsert_credential("PLATFORM", platform.id, "vendor_id", "A00012345")
    db_session.flush()


def _category_metadata_response() -> dict:
    return {
        "code": "SUCCESS",
        "message": "",
        "data": {
            "isAllowSingleItem": True,
            "attributes": [{"attributeTypeName": "용량", "required": "MANDATORY", "dataType": "STRING"}],
            "noticeCategories": [
                {
                    "noticeCategoryName": "기타 재화",
                    "noticeCategoryDetailNames": [{"noticeCategoryDetailName": "품명", "required": "MANDATORY"}],
                }
            ],
            "certifications": [{"certificationType": "NOT_REQUIRED", "name": "해당없음", "required": "OPTIONAL"}],
            "requiredDocumentNames": [],
            "allowedOfferConditions": ["NEW"],
        },
    }


def _valid_channel_fields() -> dict:
    return {
        "saleStartedAt": "2026-09-03T00:00:00",
        "saleEndedAt": "2099-12-31T23:59:59",
        "deliveryMethod": "SEQUENCIAL",
        "deliveryCompanyCode": "CJGLS",
        "deliveryChargeType": "FREE",
        "deliveryCharge": 0,
        "freeShipOverAmount": 0,
        "deliveryChargeOnReturn": 3000,
        "remoteAreaDeliverable": "N",
        "unionDeliveryType": "NOT_UNION_DELIVERY",
        "returnCenterCode": "NO_RETURN_CENTERCODE",
        "returnChargeName": "테스트 반품지",
        "companyContactNumber": "02-1234-5678",
        "returnZipCode": "06236",
        "returnAddress": "서울시 강남구",
        "returnAddressDetail": "테스트빌딩 1층",
        "returnCharge": 3500,
        "outboundShippingPlaceCode": 12345,
        "vendorUserId": "wing-id",
        "requested": False,
        "category_attribute_values": {"용량": "200ml", "품명": "테스트상품"},
        "item": {
            "itemName": "테스트옵션",
            "originalPrice": 25000,
            "maximumBuyForPerson": 0,
            "maximumBuyForPersonPeriod": 1,
            "outboundShippingTimeDay": 1,
            "unitCount": 1,
            "adultOnly": "EVERYONE",
            "taxType": "TAX",
            "parallelImported": "NOT_PARALLEL_IMPORTED",
            "overseasPurchased": "NOT_OVERSEAS_PURCHASED",
        },
    }


def _valid_draft_snapshot() -> dict:
    return {
        "name": "테스트 등록 상품",
        "sale_price": 19900,
        "description_html": "<p>상세설명</p>",
        "category_code": CATEGORY_CODE,
        "image_urls": ["https://img.example.com/main.jpg"],
        "stock_quantity": 100,
        "channel_fields": _valid_channel_fields(),
    }


def _make_handler(create_response: dict, captured: list):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        path = request.url.path
        if path == CATEGORY_METADATA_PATH_TMPL.format(code=CATEGORY_CODE):
            return httpx.Response(200, json=_category_metadata_response())
        if path == CREATE_PRODUCT_PATH:
            return httpx.Response(200, json=create_response)
        raise AssertionError(f"예상치 못한 요청: {path}")

    return handler


class TestCapabilityFlags:
    def test_product_create_is_supported_but_info_update_is_not(self):
        assert CoupangConnector.supports_product_create is True
        assert CoupangConnector.supports_product_info_update is False


class TestFetchCategoryRequirements:
    def test_parses_mandatory_flags_from_metadata_response(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured: list = []
        handler = _make_handler({}, captured)
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api-gateway.coupang.com")
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        result = connector.fetch_category_requirements(CATEGORY_CODE)

        assert result.mandatory_names() == ["용량", "품명"]
        assert result.certifications[0].mandatory is False


class TestCreateProduct:
    def test_sends_official_request_and_parses_nested_response(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured: list = []
        create_response = {
            "code": "200",
            "message": "",
            "data": {"code": "SUCCESS", "message": "", "data": SELLER_PRODUCT_ID},
        }
        handler = _make_handler(create_response, captured)
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api-gateway.coupang.com")
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        result = connector.create_product(_valid_draft_snapshot())

        assert result.accepted is True
        assert result.channel_product_id == str(SELLER_PRODUCT_ID)
        # 등록 응답은 옵션 단위 식별자를 돌려주지 않는다 - 추측해서 채우지 않는다.
        assert result.channel_option_id is None

        post_req = next(r for r in captured if r.method == "POST" and r.url.path == CREATE_PRODUCT_PATH)
        body = json.loads(post_req.content)
        assert body["displayCategoryCode"] == CATEGORY_CODE
        assert body["sellerProductName"] == "테스트 등록 상품"
        assert body["vendorId"] == "A00012345"
        assert body["requested"] is False
        assert body["images"][0]["imageType"] == "REPRESENTATION"
        assert body["images"][0]["vendorPath"] == "https://img.example.com/main.jpg"
        item = body["items"][0]
        assert item["salePrice"] == 19900
        assert item["maximumBuyCount"] == 100
        assert {"attributeTypeName": "용량", "attributeValueName": "200ml"} in item["attributes"]

    def test_error_response_is_not_accepted(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured: list = []
        create_response = {"code": "200", "message": "", "data": {"code": "ERROR", "message": "실패", "data": None}}
        handler = _make_handler(create_response, captured)
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api-gateway.coupang.com")
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        result = connector.create_product(_valid_draft_snapshot())

        assert result.accepted is False
        assert result.channel_product_id is None

    def test_blocks_when_category_mandatory_attribute_missing_without_registration_call(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured: list = []
        handler = _make_handler({}, captured)
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api-gateway.coupang.com")
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)
        snapshot = _valid_draft_snapshot()
        del snapshot["channel_fields"]["category_attribute_values"]["용량"]

        with pytest.raises(ValueError, match="용량"):
            connector.create_product(snapshot)
        assert not any(r.url.path == CREATE_PRODUCT_PATH for r in captured)

    @pytest.mark.parametrize("missing_field", ["returnCenterCode", "deliveryChargeType", "outboundShippingPlaceCode"])
    def test_blocks_when_core_top_level_field_missing_without_registration_call(
        self, db_session, platform, missing_field
    ):
        _register_credentials(db_session, platform)
        captured: list = []
        handler = _make_handler({}, captured)
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api-gateway.coupang.com")
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)
        snapshot = _valid_draft_snapshot()
        del snapshot["channel_fields"][missing_field]

        with pytest.raises(ValueError, match=missing_field):
            connector.create_product(snapshot)
        assert not any(r.url.path == CREATE_PRODUCT_PATH for r in captured)

    def test_blocks_when_no_images(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured: list = []
        handler = _make_handler({}, captured)
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api-gateway.coupang.com")
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)
        snapshot = _valid_draft_snapshot()
        snapshot["image_urls"] = []

        with pytest.raises(ValueError, match="image_urls"):
            connector.create_product(snapshot)
        assert not any(r.url.path == CREATE_PRODUCT_PATH for r in captured)

    def test_blocks_without_category_code(self, db_session, platform):
        _register_credentials(db_session, platform)
        connector = CoupangConnector(session=db_session, platform_id=platform.id)
        snapshot = _valid_draft_snapshot()
        snapshot["category_code"] = None

        with pytest.raises(ValueError, match="category_code"):
            connector.create_product(snapshot)

    def test_blocks_when_credentials_missing(self, db_session, platform):
        with pytest.raises(MarketplaceCredentialMissingError):
            CoupangConnector(session=db_session, platform_id=platform.id).create_product(_valid_draft_snapshot())
