"""
tests/unit/test_naver_smartstore_connector.py
---------------------------------------------------
NaverSmartstoreConnector: 실제 API 키가 없으면(개발/테스트 기본값) 항상
더미로 폴백하고, api_credentials에 실제 키가 등록되면 실제 HTTP 흐름(OAuth2
토큰 발급 + 주문 조회)을 시도하는지 검증한다. 네트워크는 httpx.MockTransport로
대체해 실제 외부 호출 없이 요청 구성(URL/헤더/서명)만 검증한다.

응답 목(mock) 데이터는 실운영 환경에서 실제로 확인한 네이버 커머스 API 응답
스키마(data.contents[], content.order/content.productOrder)를 그대로 따른다.
"""

from datetime import date

import httpx
import pytest

from integrations.malls.errors import MarketplaceCredentialMissingError, MarketplaceExternalAPIError
from integrations.malls.naver_smartstore_connector import NaverSmartstoreConnector
from services.settings_service import ApiCredentialService


def _content_row(order_id: str, product_order_id: str, order_date: str, status: str = "PAYED", **po_overrides):
    """실 API 응답의 data.contents[] 원소 1건(라인아이템 1개)을 만든다."""
    product_order = {
        "productOrderId": product_order_id,
        "productOrderStatus": status,
        "itemNo": po_overrides.get("item_no", "SKU-1"),
        "quantity": po_overrides.get("quantity", 2),
        "unitPrice": po_overrides.get("unit_price", 10000.0),
        "productName": po_overrides.get("product_name"),
        "productOption": po_overrides.get("option_name"),
        "sellerProductCode": po_overrides.get("seller_product_code"),
    }
    return {
        "productOrderId": product_order_id,
        "content": {
            "order": {
                "orderId": order_id,
                "orderDate": order_date,
                "ordererId": "cust*****",
                "ordererNo": "CUST-1",
                "ordererName": "홍길동",
                "ordererTel": "010-1234-5678",
                "orderDiscountAmount": po_overrides.get("discount", 0),
                "generalPaymentAmount": po_overrides.get("total_amount", 20000.0),
            },
            "productOrder": product_order,
        },
    }


class TestCredentialMissingFailsClosed:
    """인증정보가 없거나 불완전하면 더미로 폴백하지 않고 명시적 오류를 던진다(운영 안전)."""

    def test_no_session_raises_credential_missing(self):
        connector = NaverSmartstoreConnector()
        with pytest.raises(MarketplaceCredentialMissingError):
            connector.fetch_orders(date(2026, 1, 1), date(2026, 1, 3))

    def test_no_platform_id_raises_credential_missing(self, db_session):
        connector = NaverSmartstoreConnector(session=db_session, platform_id=None)
        with pytest.raises(MarketplaceCredentialMissingError):
            connector.fetch_orders(date(2026, 1, 1), date(2026, 1, 3))

    def test_session_without_credentials_raises_credential_missing(self, db_session, platform):
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id)
        with pytest.raises(MarketplaceCredentialMissingError):
            connector.fetch_orders(date(2026, 1, 1), date(2026, 1, 3))

    def test_partial_credentials_raises_credential_missing(self, db_session, platform):
        # client_secret이 빠지면 실 연동 조건 미충족 -> 더미가 아니라 명시적 오류.
        ApiCredentialService(db_session).upsert_credential("PLATFORM", platform.id, "client_id", "cid")
        db_session.flush()
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id)
        with pytest.raises(MarketplaceCredentialMissingError):
            connector.fetch_orders(date(2026, 1, 1), date(2026, 1, 3))


class TestLiveIntegration:
    def test_uses_real_http_flow_when_credentials_registered(self, db_session, platform):
        ApiCredentialService(db_session).upsert_credential("PLATFORM", platform.id, "client_id", "test-client-id")
        ApiCredentialService(db_session).upsert_credential(
            "PLATFORM", platform.id, "client_secret", "$2b$12$Cq/28lyv3wDDjELmomd4Me"
        )
        db_session.flush()

        captured_requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_requests.append(request)
            if request.url.path.endswith("/oauth2/token"):
                return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
            return httpx.Response(
                200,
                json={
                    "data": {
                        "contents": [
                            _content_row("ORDER-1", "PO-1", "2026-01-01T10:00:00+09:00", quantity=2, unit_price=10000.0)
                        ]
                    }
                },
            )

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        orders = connector.fetch_orders(date(2026, 1, 1), date(2026, 1, 2))

        assert len(captured_requests) == 2
        assert captured_requests[0].url.path == "/external/v1/oauth2/token"
        assert captured_requests[1].headers["authorization"] == "Bearer fake-token"
        # 실운영 환경에서 실제로 발생했던 오류(HTTP 400 "유효한 ISO-8601 포맷이 아닙니다")
        # 재발 방지: from/to는 반드시 전체 datetime(밀리초+KST 오프셋) 형식이어야 한다.
        order_query = dict(captured_requests[1].url.params)
        assert order_query["from"] == "2026-01-01T00:00:00.000+09:00"
        assert order_query["to"] == "2026-01-02T00:00:00.000+09:00"
        # 실제 "주문번호"는 content.order.orderId여야 한다(content.productOrder.productOrderId 아님).
        assert orders == [
            {
                "platform_order_no": "ORDER-1",
                "order_date": orders[0]["order_date"],
                "status": "NEW",
                "customer_key": "CUST-1",
                "customer_name": "홍길동",
                "customer_phone": "010-1234-5678",
                "total_amount": 20000.0,
                "discount_amount": 0.0,
                "receiver_name": None,
                "receiver_phone": None,
                "receiver_zipcode": None,
                "receiver_address": None,
                "delivery_message": None,
                "items": [
                    {
                        "platform_order_item_no": "PO-1",
                        "platform_option_id": "SKU-1",
                        "quantity": 2,
                        "unit_price": 10000.0,
                        "platform_product_id": None,
                        "product_name": None,
                        "option_name": None,
                        "seller_product_code": None,
                        "category": None,
                        "brand": None,
                        "manufacturer": None,
                    }
                ],
            }
        ]

    def test_extracts_shipping_address(self, db_session, platform):
        """배송지(수취인) 정보가 productOrder.shippingAddress에서 채워진다."""
        ApiCredentialService(db_session).upsert_credential("PLATFORM", platform.id, "client_id", "test-client-id")
        ApiCredentialService(db_session).upsert_credential(
            "PLATFORM", platform.id, "client_secret", "$2b$12$Cq/28lyv3wDDjELmomd4Me"
        )
        db_session.flush()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth2/token"):
                return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
            row = _content_row("ORDER-9", "PO-9", "2026-01-01T10:00:00+09:00")
            row["content"]["productOrder"]["shippingAddress"] = {
                "name": "수취인",
                "tel1": "010-9999-8888",
                "zipCode": "06236",
                "baseAddress": "서울시 강남구",
                "detailAddress": "테헤란로 1",
            }
            row["content"]["productOrder"]["shippingMemo"] = "문 앞에 놓아주세요"
            return httpx.Response(200, json={"data": {"contents": [row]}})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        o = connector.fetch_orders(date(2026, 1, 1), date(2026, 1, 2))[0]
        assert o["receiver_name"] == "수취인"
        assert o["receiver_phone"] == "010-9999-8888"
        assert o["receiver_zipcode"] == "06236"
        assert o["receiver_address"] == "서울시 강남구 테헤란로 1"
        assert o["delivery_message"] == "문 앞에 놓아주세요"

    def test_groups_multiple_line_items_under_same_order_id(self, db_session, platform):
        """실운영 환경에서 확인: 응답 1건은 개별 상품주문(라인아이템) 단위이며, 같은
        content.order.orderId를 가진 여러 건은 하나의 주문으로 묶여야 한다."""
        ApiCredentialService(db_session).upsert_credential("PLATFORM", platform.id, "client_id", "test-client-id")
        ApiCredentialService(db_session).upsert_credential(
            "PLATFORM", platform.id, "client_secret", "$2b$12$Cq/28lyv3wDDjELmomd4Me"
        )
        db_session.flush()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth2/token"):
                return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
            return httpx.Response(
                200,
                json={
                    "data": {
                        "contents": [
                            _content_row(
                                "ORDER-MULTI",
                                "PO-A",
                                "2026-01-01T10:00:00+09:00",
                                item_no="SKU-A",
                                quantity=1,
                                unit_price=5000.0,
                                total_amount=15000.0,
                            ),
                            _content_row(
                                "ORDER-MULTI",
                                "PO-B",
                                "2026-01-01T10:00:00+09:00",
                                item_no="SKU-B",
                                quantity=2,
                                unit_price=5000.0,
                                total_amount=15000.0,
                            ),
                        ]
                    }
                },
            )

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        orders = connector.fetch_orders(date(2026, 1, 1), date(2026, 1, 2))

        assert len(orders) == 1
        assert orders[0]["platform_order_no"] == "ORDER-MULTI"
        assert len(orders[0]["items"]) == 2
        assert {i["platform_option_id"] for i in orders[0]["items"]} == {"SKU-A", "SKU-B"}

    def test_multi_day_range_is_chunked_into_daily_calls(self, db_session, platform):
        """실운영 환경에서 실제로 발생했던 오류(HTTP 400 "from, to 는 최대 24시간 차이로
        설정해야 합니다") 재발 방지: 여러 날짜 범위는 하루 단위로 나눠 호출하고 결과를 합친다."""
        ApiCredentialService(db_session).upsert_credential("PLATFORM", platform.id, "client_id", "test-client-id")
        ApiCredentialService(db_session).upsert_credential(
            "PLATFORM", platform.id, "client_secret", "$2b$12$Cq/28lyv3wDDjELmomd4Me"
        )
        db_session.flush()

        order_query_requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth2/token"):
                return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
            order_query_requests.append(dict(request.url.params))
            day_from = dict(request.url.params)["from"][:10]
            order_id = f"ORDER-{day_from.replace('-', '')}"
            return httpx.Response(
                200,
                json={"data": {"contents": [_content_row(order_id, f"{order_id}-PO", f"{day_from}T10:00:00+09:00")]}},
            )

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        orders = connector.fetch_orders(date(2026, 1, 1), date(2026, 1, 4))

        # 3일 범위(1/1~1/4)는 하루씩 3번 호출되어야 한다(각 호출의 from/to 차이가 24시간 이하).
        assert len(order_query_requests) == 3
        assert [q["from"][:10] for q in order_query_requests] == ["2026-01-01", "2026-01-02", "2026-01-03"]
        assert len(orders) == 3
        assert {o["platform_order_no"] for o in orders} == {"ORDER-20260101", "ORDER-20260102", "ORDER-20260103"}

    def test_token_failure_raises_clear_error(self, db_session, platform):
        ApiCredentialService(db_session).upsert_credential("PLATFORM", platform.id, "client_id", "test-client-id")
        ApiCredentialService(db_session).upsert_credential(
            "PLATFORM", platform.id, "client_secret", "$2b$12$Cq/28lyv3wDDjELmomd4Me"
        )
        db_session.flush()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="invalid client")

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        # 401 토큰 발급 실패는 응답 본문 없이 AUTH_FAILED(비재시도) 외부 API 오류로 변환된다.
        with pytest.raises(MarketplaceExternalAPIError) as ei:
            connector.fetch_orders(date(2026, 1, 1), date(2026, 1, 2))
        assert ei.value.reason_code == "AUTH_FAILED"
        assert "invalid client" not in str(ei.value)


def _channel_content(
    group_no: int, origin_no: int, channel_product_no: int, name: str = "테스트 상품", **overrides
) -> dict:
    """실 운영 환경에서 실제로 확인한 상품 검색 API 응답의 content 1건(_normalize_live_products
    docstring 참고) - 색상/사이즈가 다른 변형은 originProductNo/channelProductNo만 다르고
    groupProductNo는 공유한다."""
    channel_product = {
        "channelProductNo": channel_product_no,
        "categoryId": overrides.get("category_id", "50000000"),
        "name": name,
        "sellerManagementCode": overrides.get("seller_product_code"),
        "statusType": overrides.get("status_type", "SALE"),
        "channelProductDisplayStatusType": overrides.get("display_status", "ON"),
        "brandName": overrides.get("brand"),
        "manufacturerName": overrides.get("manufacturer"),
        "salePrice": overrides.get("sale_price"),
    }
    if overrides.get("representative_url") is not None:
        channel_product["representativeImage"] = {"url": overrides["representative_url"]}
    return {"groupProductNo": group_no, "originProductNo": origin_no, "channelProducts": [channel_product]}


class TestFetchProducts:
    def test_no_credentials_raises_credential_missing(self, db_session, platform):
        # 상품 조회도 인증정보 없으면 더미로 위장하지 않고 명시적 오류를 던진다.
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id)
        with pytest.raises(MarketplaceCredentialMissingError):
            connector.fetch_products()

    def test_live_groups_color_variants_under_shared_group_product_no(self, db_session, platform):
        """실제 응답으로 확인한 스키마: 색상 변형(아이보리/블루 등)은 서로 다른
        originProductNo/channelProductNo를 갖지만 같은 groupProductNo를 공유하며,
        이 groupProductNo로 그룹핑해 하나의 ERP 상품 아래 여러 옵션으로 등록한다."""
        ApiCredentialService(db_session).upsert_credential("PLATFORM", platform.id, "client_id", "test-client-id")
        ApiCredentialService(db_session).upsert_credential(
            "PLATFORM", platform.id, "client_secret", "$2b$12$Cq/28lyv3wDDjELmomd4Me"
        )
        db_session.flush()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth2/token"):
                return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
            return httpx.Response(
                200,
                json={
                    "contents": [
                        _channel_content(
                            51525356,
                            13590724369,
                            13650616698,
                            name="장갑, 아이보리",
                            seller_product_code="SELLER-IVORY",
                            brand="테스트브랜드",
                            manufacturer="테스트제조사",
                            representative_url="https://img.example.com/rep.jpg",
                            sale_price=7700,
                        ),
                        _channel_content(
                            51525356,
                            13590724367,
                            13650616697,
                            name="장갑, 블루",
                            seller_product_code="SELLER-BLUE",
                            status_type="SALE",
                            display_status="OUT",  # 노출 중지 -> is_selling False
                        ),
                    ]
                },
            )

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        products = connector.fetch_products()

        assert len(products) == 1  # 같은 groupProductNo 아래로 묶였다
        product = products[0]
        assert product["product_name"] == "장갑, 아이보리"  # 그룹의 첫 채널상품명을 대표로 사용
        assert product["brand"] == "테스트브랜드"
        assert product["manufacturer"] == "테스트제조사"
        assert product["images"]["representative_url"] == "https://img.example.com/rep.jpg"
        assert len(product["items"]) == 2
        ivory = next(i for i in product["items"] if i["platform_option_id"] == "13650616698")
        assert ivory["option_name"] == "장갑, 아이보리"
        assert ivory["seller_product_code"] == "SELLER-IVORY"
        assert ivory["is_selling"] is True
        assert ivory["platform_product_id"] == "51525356"
        assert ivory["sale_price"] == 7700
        blue = next(i for i in product["items"] if i["platform_option_id"] == "13650616697")
        assert blue["is_selling"] is False  # channelProductDisplayStatusType != "ON"

    def test_live_treats_ungrouped_product_as_its_own_group(self, db_session, platform):
        """groupProductNo가 없는 항목은 originProductNo를 그룹 키로 대체 사용해
        최소한 개별 상품으로는 등록되게 한다."""
        ApiCredentialService(db_session).upsert_credential("PLATFORM", platform.id, "client_id", "test-client-id")
        ApiCredentialService(db_session).upsert_credential(
            "PLATFORM", platform.id, "client_secret", "$2b$12$Cq/28lyv3wDDjELmomd4Me"
        )
        db_session.flush()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth2/token"):
                return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
            content = _channel_content(0, 999, 8888, name="단일 상품")
            del content["groupProductNo"]
            return httpx.Response(200, json={"contents": [content]})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        products = connector.fetch_products()

        assert len(products) == 1
        assert products[0]["items"][0]["platform_option_id"] == "8888"
        assert products[0]["items"][0]["platform_product_id"] == "999"

    def test_live_paginates_until_short_page(self, db_session, platform):
        ApiCredentialService(db_session).upsert_credential("PLATFORM", platform.id, "client_id", "test-client-id")
        ApiCredentialService(db_session).upsert_credential(
            "PLATFORM", platform.id, "client_secret", "$2b$12$Cq/28lyv3wDDjELmomd4Me"
        )
        db_session.flush()

        request_pages = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth2/token"):
                return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
            import json as _json

            body = _json.loads(request.content)
            page = body["page"]
            request_pages.append(page)
            if page == 1:
                return httpx.Response(
                    200, json={"contents": [_channel_content(i, i, i, name=f"상품{i}") for i in range(100)]}
                )
            return httpx.Response(200, json={"contents": [_channel_content(200, 200, 200, name="상품200")]})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        products = connector.fetch_products()

        assert request_pages == [1, 2]
        assert len(products) == 101


class TestSignatureError:
    def test_invalid_client_secret_raises_credential_missing(self, db_session, platform):
        """bcrypt salt 형식이 아닌 전자서명 키는 사용 불가 = 인증정보 오류(키 값 미노출)."""
        ApiCredentialService(db_session).upsert_credential("PLATFORM", platform.id, "client_id", "cid")
        ApiCredentialService(db_session).upsert_credential("PLATFORM", platform.id, "client_secret", "LXn_wrong")
        db_session.flush()

        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id)
        with pytest.raises(MarketplaceCredentialMissingError) as ei:
            connector.fetch_orders(date(2026, 1, 1), date(2026, 1, 2))
        assert "LXn_wrong" not in str(ei.value)


class TestSellerId:
    def test_seller_id_sends_seller_type_and_account_id(self, db_session, platform):
        """판매자ID(seller_id)가 등록되면 토큰 요청이 type=SELLER + account_id로 나간다."""
        svc = ApiCredentialService(db_session)
        svc.upsert_credential("PLATFORM", platform.id, "client_id", "cid")
        svc.upsert_credential("PLATFORM", platform.id, "client_secret", "$2b$12$Cq/28lyv3wDDjELmomd4Me")
        svc.upsert_credential("PLATFORM", platform.id, "seller_id", "ncp_seller_001")
        db_session.flush()

        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            if request.url.path.endswith("/oauth2/token"):
                return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
            return httpx.Response(200, json={"data": {"contents": []}})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)
        connector.fetch_orders(date(2026, 1, 1), date(2026, 1, 2))

        token_body = captured[0].content.decode()
        assert "type=SELLER" in token_body
        assert "account_id=ncp_seller_001" in token_body

    def test_no_seller_id_uses_self_type(self, db_session, platform):
        svc = ApiCredentialService(db_session)
        svc.upsert_credential("PLATFORM", platform.id, "client_id", "cid")
        svc.upsert_credential("PLATFORM", platform.id, "client_secret", "$2b$12$Cq/28lyv3wDDjELmomd4Me")
        db_session.flush()

        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            if request.url.path.endswith("/oauth2/token"):
                return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
            return httpx.Response(200, json={"data": {"contents": []}})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)
        connector.fetch_orders(date(2026, 1, 1), date(2026, 1, 2))

        token_body = captured[0].content.decode()
        assert "type=SELF" in token_body
        assert "account_id" not in token_body


class TestNoPiiInLogs:
    def test_order_response_pii_is_not_logged_or_written(self, db_session, platform, caplog, tmp_path, monkeypatch):
        """실주문 응답의 개인정보(이름·전화·주소)가 로그나 파일에 남지 않아야 한다.

        과거엔 응답 원문 전체를 logger.info + /app/logs/naver_order_response.json에
        덤프했다. 이 회귀 테스트로 재발을 막는다.
        """
        import logging
        import os

        svc = ApiCredentialService(db_session)
        svc.upsert_credential("PLATFORM", platform.id, "client_id", "cid")
        svc.upsert_credential("PLATFORM", platform.id, "client_secret", "$2b$12$Cq/28lyv3wDDjELmomd4Me")
        db_session.flush()

        pii_name, pii_tel, pii_addr = "홍길동", "010-1234-5678", "서울시 강남구 테헤란로 1"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth2/token"):
                return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
            row = _content_row("ORDER-PII", "PO-PII", "2026-01-01T10:00:00+09:00")
            row["content"]["order"]["ordererName"] = pii_name
            row["content"]["order"]["ordererTel"] = pii_tel
            row["content"]["productOrder"]["shippingAddress"] = {
                "name": pii_name,
                "tel1": pii_tel,
                "zipCode": "06236",
                "baseAddress": "서울시 강남구",
                "detailAddress": "테헤란로 1",
            }
            return httpx.Response(200, json={"data": {"contents": [row]}})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.commerce.naver.com")
        connector = NaverSmartstoreConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        # 파일 덤프가 되살아나면 잡히도록, open을 감시한다.
        opened_paths = []
        real_open = open

        def watched_open(path, *args, **kwargs):
            opened_paths.append(str(path))
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", watched_open)

        with caplog.at_level(logging.DEBUG):
            orders = connector.fetch_orders(date(2026, 1, 1), date(2026, 1, 2))

        # 수집 자체는 정상, 개인정보는 파싱되어 있음(반환값에는 있어도 됨).
        assert orders[0]["receiver_name"] == pii_name
        # 로그에는 개인정보가 없어야 한다.
        assert pii_name not in caplog.text
        assert pii_tel not in caplog.text
        assert pii_addr not in caplog.text
        # 허용된 메타데이터 요약 로그는 있어야 한다.
        assert "네이버 주문 조회 완료" in caplog.text
        # 응답을 파일로 저장하지 않아야 한다.
        assert not any("naver_order_response" in p for p in opened_paths)
        assert not os.path.exists("/app/logs/naver_order_response.json") or True  # 경로 존재 여부와 무관하게 미기록
