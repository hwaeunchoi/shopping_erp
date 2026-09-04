"""
tests/unit/test_naver_smartstore_connector_option_publish.py
------------------------------------------------------------------
NaverSmartstoreConnector의 상용 ERP 확장(3단계, 세 번째 묶음) - 옵션 조합 상품
등록(create_product_with_options)과 조합별 옵션 단위 식별자 후속 조회
(fetch_option_registration_status)를 MockTransport로 검증한다. 요청/응답
스키마는 commerce-api-naver/commerce-api 저장소 docs/2.0.0-RC.js에 포함된 공식
OpenAPI 스펙 파일에서 직접 확인한 값을 그대로 재현한다(2026-09 조회). 실제
외부 호출은 하지 않는다.
"""

import json

import httpx
import pytest

from integrations.malls.errors import MarketplaceCredentialMissingError, MarketplaceValidationError
from integrations.malls.naver_smartstore_connector import CREATE_PRODUCT_PATH, NaverSmartstoreConnector
from tests.unit.test_naver_smartstore_connector_publish import _register_credentials, _valid_channel_fields

ORIGIN_PRODUCT_NO = "5000000001"
CHANNEL_PRODUCT_NO = "6000000001"


def _item(product_option_id: int, color: str, size: str, sale_price: float, stock: int, code: str) -> dict:
    return {
        "product_option_id": product_option_id,
        "option_values": [["색상", color], ["사이즈", size]],
        "seller_product_code": code,
        "sale_price": sale_price,
        "stock_quantity": stock,
    }


def _valid_group_snapshot() -> dict:
    return {
        "name": "테스트 옵션조합 상품",
        "description_html": "<p>상세설명</p>",
        "category_code": "50000803",
        "image_urls": ["https://img.example.com/main.jpg"],
        "base_sale_price": 20000,
        "channel_fields": _valid_channel_fields(),
        "items": [
            _item(1, "블랙", "S", 20000, 10, "SKU-000001"),
            _item(2, "블랙", "M", 21000, 5, "SKU-000002"),
            _item(3, "화이트", "S", 19000, 8, "SKU-000003"),
        ],
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


class TestCapabilityFlag:
    def test_product_option_create_is_supported(self):
        assert NaverSmartstoreConnector.supports_product_option_create is True


class TestCreateProductWithOptions:
    def test_sends_option_combinations_with_add_on_price_and_parses_response(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured: list = []
        handler = _make_create_handler(
            {"originProductNo": int(ORIGIN_PRODUCT_NO), "smartstoreChannelProductNo": int(CHANNEL_PRODUCT_NO)}, captured
        )
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        result = connector.create_product_with_options(_valid_group_snapshot())

        assert result.accepted is True
        assert result.channel_product_id == ORIGIN_PRODUCT_NO
        assert result.channel_option_id == CHANNEL_PRODUCT_NO
        # 등록 응답에는 조합별 식별자가 없어 items는 seller_product_code만 채워진다.
        assert [i.seller_product_code for i in result.items] == ["SKU-000001", "SKU-000002", "SKU-000003"]
        assert all(i.channel_option_id is None for i in result.items)

        post_req = next(r for r in captured if r.method == "POST" and r.url.path == CREATE_PRODUCT_PATH)
        body = json.loads(post_req.content)
        option_info = body["originProduct"]["detailAttribute"]["optionInfo"]
        assert option_info["useStockManagement"] is True
        assert option_info["optionCombinationGroupNames"] == {"optionGroupName1": "색상", "optionGroupName2": "사이즈"}
        combos = option_info["optionCombinations"]
        assert len(combos) == 3
        # 옵션가는 기준 판매가(20000)에 대한 "추가금"이어야 한다 - 절대가가 아니다.
        assert combos[0] == {
            "optionName1": "블랙",
            "optionName2": "S",
            "stockQuantity": 10,
            "price": 0,
            "sellerManagerCode": "SKU-000001",
            "usable": True,
        }
        assert combos[1]["price"] == 1000  # 21000 - 20000
        assert combos[2]["price"] == -1000  # 19000 - 20000
        # 조합형 옵션 상품은 원상품 전체 재고가 옵션별 재고 합으로 자동 계산된다.
        assert body["originProduct"]["stockQuantity"] == 10 + 5 + 8
        assert body["originProduct"]["salePrice"] == 20000

    def test_blocks_when_base_sale_price_missing(self, db_session, platform):
        _register_credentials(db_session, platform)
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id)
        snapshot = _valid_group_snapshot()
        snapshot["base_sale_price"] = None

        with pytest.raises(ValueError, match="base_sale_price"):
            connector.create_product_with_options(snapshot)

    def test_blocks_duplicate_option_combination(self, db_session, platform):
        _register_credentials(db_session, platform)
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id)
        snapshot = _valid_group_snapshot()
        snapshot["items"][1]["option_values"] = snapshot["items"][0]["option_values"]

        with pytest.raises(MarketplaceValidationError, match="중복"):
            connector.create_product_with_options(snapshot)

    def test_blocks_duplicate_seller_product_code(self, db_session, platform):
        _register_credentials(db_session, platform)
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id)
        snapshot = _valid_group_snapshot()
        snapshot["items"][1]["seller_product_code"] = snapshot["items"][0]["seller_product_code"]

        with pytest.raises(MarketplaceValidationError, match="판매자 관리코드"):
            connector.create_product_with_options(snapshot)

    def test_blocks_when_axis_order_differs_between_items(self, db_session, platform):
        _register_credentials(db_session, platform)
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id)
        snapshot = _valid_group_snapshot()
        snapshot["items"][1]["option_values"] = [["사이즈", "M"], ["색상", "블랙"]]

        with pytest.raises(MarketplaceValidationError, match="같은 옵션축 순서"):
            connector.create_product_with_options(snapshot)

    def test_blocks_when_more_than_three_axes(self, db_session, platform):
        _register_credentials(db_session, platform)
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id)
        snapshot = _valid_group_snapshot()
        for item in snapshot["items"]:
            item["option_values"] = [*item["option_values"], ["소재", "면"], ["패턴", "무지"]]

        with pytest.raises(MarketplaceValidationError, match="1~3개"):
            connector.create_product_with_options(snapshot)

    def test_blocks_when_credentials_missing(self, db_session, platform):
        with pytest.raises(MarketplaceCredentialMissingError):
            NaverSmartstoreConnector(session=db_session, platform_id=platform.id).create_product_with_options(
                _valid_group_snapshot()
            )


def _make_get_origin_product_handler(combinations: list[dict], status_type: str = "SALE"):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
        if request.method == "GET" and request.url.path.endswith(f"/origin-products/{ORIGIN_PRODUCT_NO}"):
            return httpx.Response(
                200,
                json={
                    "originProduct": {
                        "statusType": status_type,
                        "detailAttribute": {"optionInfo": {"optionCombinations": combinations}},
                    },
                    "smartstoreChannelProduct": {"channelProductDisplayStatusType": "ON"},
                },
            )
        raise AssertionError(f"예상치 못한 요청: {request.url.path}")

    return handler


class TestFetchOptionRegistrationStatus:
    def test_matches_combinations_by_seller_manager_code(self, db_session, platform):
        _register_credentials(db_session, platform)
        combinations = [
            {"id": 111, "optionName1": "블랙", "optionName2": "S", "sellerManagerCode": "SKU-000001"},
            {"id": 112, "optionName1": "블랙", "optionName2": "M", "sellerManagerCode": "SKU-000002"},
        ]
        handler = _make_get_origin_product_handler(combinations)
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        result = connector.fetch_option_registration_status(ORIGIN_PRODUCT_NO)

        assert result.status_name == "SALE"
        by_code = {i.seller_product_code: i.channel_option_id for i in result.items}
        assert by_code == {"SKU-000001": "111", "SKU-000002": "112"}

    def test_skips_combinations_without_seller_manager_code(self, db_session, platform):
        _register_credentials(db_session, platform)
        combinations = [{"id": 999, "optionName1": "레거시"}]
        handler = _make_get_origin_product_handler(combinations)
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        result = connector.fetch_option_registration_status(ORIGIN_PRODUCT_NO)

        assert result.items == []
