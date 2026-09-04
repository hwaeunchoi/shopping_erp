"""
tests/unit/test_coupang_connector_option_publish.py
------------------------------------------------------------------
CoupangConnector의 상용 ERP 확장(3단계, 세 번째 묶음) - 옵션 조합 상품 등록
(create_product_with_options)과 승인 후 옵션 단위 식별자(vendorItemId) 확인
조회(fetch_option_registration_status)를 MockTransport로 검증한다. 등록 응답이
옵션 단위 식별자를 돌려주지 않는다는 확인된 계약(create_product와 동일)에
따라 이 채널은 항상 후속 조회가 필요하다는 것도 함께 검증한다. 실제 외부
호출은 하지 않는다.
"""

import json
from typing import Any

import httpx
import pytest

from integrations.malls.coupang_connector import (
    CATEGORY_METADATA_PATH_TMPL,
    CREATE_PRODUCT_PATH,
    PRODUCT_QUERY_PATH_TMPL,
    CoupangConnector,
)
from integrations.malls.errors import MarketplaceCredentialMissingError, MarketplaceValidationError
from tests.unit.test_coupang_connector_publish import (
    CATEGORY_CODE,
    _category_metadata_response,
    _register_credentials,
    _valid_channel_fields,
)

SELLER_PRODUCT_ID = 427011919


def _item(product_option_id: int, capacity: str, sale_price: float, stock: int, code: str) -> dict:
    # 쿠팡은 옵션 축(attributes)을 아이템마다 각자 선언한다 - 카테고리 필수 속성
    # ("용량"/"품명")은 축마다 값이 달라도, 같아도 매 아이템에 함께 실려야 한다.
    return {
        "product_option_id": product_option_id,
        "option_values": [["용량", capacity], ["품명", "테스트상품"]],
        "seller_product_code": code,
        "sale_price": sale_price,
        "stock_quantity": stock,
    }


def _valid_channel_fields_for_group() -> dict:
    cf = _valid_channel_fields()
    del cf["category_attribute_values"]  # 옵션조합 등록은 품목(item)의 option_values로 대신한다.
    return cf


def _valid_group_snapshot() -> dict:
    return {
        "name": "테스트 옵션조합 상품",
        "category_code": CATEGORY_CODE,
        "image_urls": ["https://img.example.com/main.jpg"],
        "channel_fields": _valid_channel_fields_for_group(),
        "items": [_item(1, "100ml", 19900, 10, "SKU-000001"), _item(2, "200ml", 24900, 5, "SKU-000002")],
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


class TestCapabilityFlag:
    def test_product_option_create_is_supported(self):
        assert CoupangConnector.supports_product_option_create is True


class TestCreateProductWithOptions:
    def test_sends_multi_item_payload_with_absolute_prices_and_parses_nested_response(self, db_session, platform):
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

        result = connector.create_product_with_options(_valid_group_snapshot())

        assert result.accepted is True
        assert result.channel_product_id == str(SELLER_PRODUCT_ID)
        assert result.channel_option_id is None
        assert [i.seller_product_code for i in result.items] == ["SKU-000001", "SKU-000002"]
        assert all(i.channel_option_id is None for i in result.items)

        post_req = next(r for r in captured if r.method == "POST" and r.url.path == CREATE_PRODUCT_PATH)
        body = json.loads(post_req.content)
        assert len(body["items"]) == 2
        item0, item1 = body["items"]
        # 쿠팡은 아이템별 salePrice가 그 자체로 절대 판매가다(추가금이 아니다).
        assert item0["salePrice"] == 19900
        assert item0["maximumBuyCount"] == 10
        assert item0["externalVendorSku"] == "SKU-000001"
        assert {"attributeTypeName": "용량", "attributeValueName": "100ml"} in item0["attributes"]
        assert item1["salePrice"] == 24900
        assert item0["itemName"] != item1["itemName"]  # 겹치지 않게 자동 구성된다.

    def test_blocks_when_item_missing_mandatory_category_attribute(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured: list = []
        handler = _make_handler({}, captured)
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api-gateway.coupang.com")
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)
        snapshot = _valid_group_snapshot()
        snapshot["items"][0]["option_values"] = []

        with pytest.raises(MarketplaceValidationError, match="용량"):
            connector.create_product_with_options(snapshot)
        assert not any(r.url.path == CREATE_PRODUCT_PATH for r in captured)

    def test_blocks_duplicate_option_combination(self, db_session, platform):
        _register_credentials(db_session, platform)
        connector = CoupangConnector(session=db_session, platform_id=platform.id)
        snapshot = _valid_group_snapshot()
        snapshot["items"][1]["option_values"] = snapshot["items"][0]["option_values"]

        with pytest.raises(MarketplaceValidationError, match="중복"):
            connector.create_product_with_options(snapshot)

    def test_blocks_duplicate_seller_product_code(self, db_session, platform):
        _register_credentials(db_session, platform)
        connector = CoupangConnector(session=db_session, platform_id=platform.id)
        snapshot = _valid_group_snapshot()
        snapshot["items"][1]["seller_product_code"] = snapshot["items"][0]["seller_product_code"]

        with pytest.raises(MarketplaceValidationError, match="판매자 관리코드"):
            connector.create_product_with_options(snapshot)

    def test_blocks_when_credentials_missing(self, db_session, platform):
        with pytest.raises(MarketplaceCredentialMissingError):
            CoupangConnector(session=db_session, platform_id=platform.id).create_product_with_options(
                _valid_group_snapshot()
            )


def _make_query_handler(items: list[dict[str, Any]], status_name: str = "APPROVED"):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == PRODUCT_QUERY_PATH_TMPL.format(seller_product_id=SELLER_PRODUCT_ID):
            return httpx.Response(
                200, json={"code": "SUCCESS", "message": "", "data": {"statusName": status_name, "items": items}}
            )
        raise AssertionError(f"예상치 못한 요청: {request.url.path}")

    return handler


class TestFetchOptionRegistrationStatus:
    def test_matches_items_by_external_vendor_sku(self, db_session, platform):
        _register_credentials(db_session, platform)
        items: list[dict[str, Any]] = [
            {"vendorItemId": 5001, "externalVendorSku": "SKU-000001", "itemName": "100ml"},
            {"vendorItemId": None, "externalVendorSku": "SKU-000002", "itemName": "200ml"},  # 아직 승인 전.
        ]
        handler = _make_query_handler(items)
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api-gateway.coupang.com")
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        result = connector.fetch_option_registration_status(str(SELLER_PRODUCT_ID))

        assert result.status_name == "APPROVED"
        by_code = {i.seller_product_code: i.channel_option_id for i in result.items}
        assert by_code == {"SKU-000001": "5001", "SKU-000002": None}

    def test_skips_items_without_external_vendor_sku(self, db_session, platform):
        _register_credentials(db_session, platform)
        items = [{"vendorItemId": 1, "itemName": "레거시"}]
        handler = _make_query_handler(items)
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api-gateway.coupang.com")
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        result = connector.fetch_option_registration_status(str(SELLER_PRODUCT_ID))

        assert result.items == []
