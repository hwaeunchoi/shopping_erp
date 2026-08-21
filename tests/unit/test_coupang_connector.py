"""
tests/unit/test_coupang_connector.py
------------------------------------------
CoupangConnector: 인증정보(access_key/secret_key/vendor_id)가 없거나 불완전하면
더미로 폴백하지 않고 MarketplaceCredentialMissingError를 던지고, 3개가 모두
등록되면 실제 HTTP 흐름(HMAC 서명 + 발주서 조회)을 시도하는지 검증한다. 네트워크는
httpx.MockTransport로 대체해 실제 외부 호출 없이 요청 구성(URL/헤더/서명 형식)과
정규화 결과만 검증한다.
"""

from datetime import date

import httpx
import pytest

from integrations.malls.coupang_connector import COUPANG_API_BASE, ORDERSHEET_STATUSES, CoupangConnector
from integrations.malls.errors import (
    MarketplaceCapabilityUnsupportedError,
    MarketplaceCredentialMissingError,
    MarketplaceExternalAPIError,
)
from services.settings_service import ApiCredentialService

VENDOR_ID = "A00012345"


def _order_box(order_id, box_id, status="ACCEPT", ordered_at="2026-01-01T09:00:00", order_items=None):
    """실 API 응답 data[]의 배송묶음 1건을 만든다."""
    return {
        "shipmentBoxId": box_id,
        "orderId": order_id,
        "orderedAt": ordered_at,
        "status": status,
        "orderer": {"name": "홍길동", "safeNumber": "0504-1234-5678", "email": "a*****@x.com"},
        "receiver": {
            "name": "수취인",
            "safeNumber": "0504-9999-8888",
            "postCode": "06236",
            "addr1": "서울시 강남구",
            "addr2": "테헤란로 1",
        },
        "parcelPrintMessage": "부재시 경비실",
        "orderItems": order_items
        or [
            {
                "vendorItemId": 700001,
                "vendorItemName": "블랙 / L",
                "sellerProductId": 500001,
                "sellerProductName": "테스트 상품",
                "externalVendorSkuCode": "SKU-1",
                "shippingCount": 2,
                "salesPrice": 10000,
                "discountPrice": 0,
            }
        ],
    }


def _register_credentials(db_session, platform):
    svc = ApiCredentialService(db_session)
    svc.upsert_credential("PLATFORM", platform.id, "access_key", "test-access-key")
    svc.upsert_credential("PLATFORM", platform.id, "secret_key", "test-secret-key")
    svc.upsert_credential("PLATFORM", platform.id, "vendor_id", VENDOR_ID)
    db_session.flush()


class TestShipmentUpdateUnsupported:
    """미구현 송장 전송은 True 성공으로 위장하지 않고 미지원 오류를 던진다(외부 호출 없음)."""

    def test_update_shipment_raises_capability_unsupported_without_http(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        with pytest.raises(MarketplaceCapabilityUnsupportedError) as ei:
            connector.update_shipment("ORDER-SECRET-1", "CJ대한통운", "TRACK-SECRET-9")

        assert ei.value.marketplace_code == "coupang"
        assert captured == []  # 외부 HTTP 요청이 발생하지 않는다.
        msg = str(ei.value)
        assert "ORDER-SECRET-1" not in msg and "TRACK-SECRET-9" not in msg


class TestCredentialMissingFailsClosed:
    """인증정보가 없거나 불완전하면 더미로 폴백하지 않고 명시적 오류를 던진다(운영 안전)."""

    def test_no_session_raises_credential_missing(self):
        connector = CoupangConnector()
        with pytest.raises(MarketplaceCredentialMissingError):
            connector.fetch_orders(date(2026, 1, 1), date(2026, 1, 3))

    def test_no_platform_id_raises_credential_missing(self, db_session):
        connector = CoupangConnector(session=db_session, platform_id=None)
        with pytest.raises(MarketplaceCredentialMissingError):
            connector.fetch_orders(date(2026, 1, 1), date(2026, 1, 3))

    def test_session_without_credentials_raises_credential_missing(self, db_session, platform):
        connector = CoupangConnector(session=db_session, platform_id=platform.id)
        with pytest.raises(MarketplaceCredentialMissingError):
            connector.fetch_orders(date(2026, 1, 1), date(2026, 1, 3))

    def test_partial_credentials_raises_credential_missing(self, db_session, platform):
        # vendor_id가 빠지면 실 연동 조건 미충족 -> 더미가 아니라 명시적 오류.
        svc = ApiCredentialService(db_session)
        svc.upsert_credential("PLATFORM", platform.id, "access_key", "ak")
        svc.upsert_credential("PLATFORM", platform.id, "secret_key", "sk")
        db_session.flush()
        connector = CoupangConnector(session=db_session, platform_id=platform.id)
        with pytest.raises(MarketplaceCredentialMissingError):
            connector.fetch_orders(date(2026, 1, 1), date(2026, 1, 2))


class TestLiveIntegration:
    def test_uses_hmac_signed_request_when_credentials_registered(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            # 쿠팡 발주서 조회는 status가 필수 - status=ACCEPT일 때만 주문을 준다.
            if dict(request.url.params).get("status") == "ACCEPT":
                return httpx.Response(200, json={"data": [_order_box("30001", 1)], "nextToken": ""})
            return httpx.Response(200, json={"data": [], "nextToken": ""})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        orders = connector.fetch_orders(date(2026, 1, 1), date(2026, 1, 2))

        # status가 필수이므로 상태별로 스윕한다(ACCEPT..FINAL_DELIVERY = 5회).
        assert len(captured) == len(ORDERSHEET_STATUSES)
        assert {dict(r.url.params)["status"] for r in captured} == set(ORDERSHEET_STATUSES)
        req = captured[0]
        # vendorId가 경로에 포함돼야 한다.
        assert f"/vendors/{VENDOR_ID}/ordersheets" in req.url.path
        # HMAC(CEA) Authorization 헤더 형식.
        auth = req.headers["authorization"]
        assert auth.startswith("CEA algorithm=HmacSHA256")
        assert "access-key=test-access-key" in auth
        assert "signed-date=" in auth and "signature=" in auth
        # 날짜 파라미터는 yyyy-MM-dd, status 필수 파라미터 포함.
        q = dict(req.url.params)
        assert q["createdAtFrom"] == "2026-01-01"
        assert q["createdAtTo"] == "2026-01-02"
        assert q["status"] in ORDERSHEET_STATUSES

        # 배송지(수취인) 정보가 receiver + parcelPrintMessage에서 채워진다.
        assert orders[0]["receiver_name"] == "수취인"
        assert orders[0]["receiver_zipcode"] == "06236"
        assert orders[0]["receiver_address"] == "서울시 강남구 테헤란로 1"
        assert orders[0]["delivery_message"] == "부재시 경비실"
        assert orders == [
            {
                "platform_order_no": "30001",
                "order_date": orders[0]["order_date"],
                "status": "NEW",
                "customer_key": "30001",
                "customer_name": "홍길동",
                "customer_phone": "0504-1234-5678",
                "total_amount": 20000.0,
                "discount_amount": 0.0,
                "receiver_name": "수취인",
                "receiver_phone": "0504-9999-8888",
                "receiver_zipcode": "06236",
                "receiver_address": "서울시 강남구 테헤란로 1",
                "delivery_message": "부재시 경비실",
                "items": [
                    {
                        "platform_option_id": "700001",
                        "quantity": 2,
                        "unit_price": 10000.0,
                        "platform_product_id": "500001",
                        "product_name": "테스트 상품",
                        "option_name": "블랙 / L",
                        "seller_product_code": "SKU-1",
                        "category": None,
                        "brand": None,
                        "manufacturer": None,
                    }
                ],
            }
        ]

    def test_signature_query_matches_sent_query(self, db_session, platform):
        """서명에 쓴 쿼리와 실제 전송 쿼리가 동일해야 401이 나지 않는다.

        헤더에 담긴 signature를, 전송된 URL의 쿼리로 다시 계산한 값과 비교한다.
        """
        _register_credentials(db_session, platform)
        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"data": [], "nextToken": ""})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)
        connector.fetch_orders(date(2026, 1, 1), date(2026, 1, 2))

        import hashlib
        import hmac

        req = captured[0]
        auth = req.headers["authorization"]
        parts = dict(p.strip().split("=", 1) for p in auth.replace("CEA algorithm=HmacSHA256, ", "").split(", "))
        sent_query = str(req.url.query, "utf-8") if isinstance(req.url.query, bytes) else req.url.query
        message = parts["signed-date"] + "GET" + req.url.path + sent_query
        expected = hmac.new(b"test-secret-key", message.encode("utf-8"), hashlib.sha256).hexdigest()
        assert parts["signature"] == expected

    def test_groups_multiple_boxes_under_same_order_id(self, db_session, platform):
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            # 두 배송묶음이 서로 다른 상태(ACCEPT/INSTRUCT)로 내려와도 같은 주문으로 묶여야 한다.
            status_value = dict(request.url.params).get("status")
            if status_value == "ACCEPT":
                data = [
                    _order_box(
                        "ORDER-MULTI",
                        1,
                        order_items=[
                            {"vendorItemId": 1, "vendorItemName": "A", "shippingCount": 1, "salesPrice": 5000}
                        ],
                    )
                ]
            elif status_value == "INSTRUCT":
                data = [
                    _order_box(
                        "ORDER-MULTI",
                        2,
                        order_items=[
                            {"vendorItemId": 2, "vendorItemName": "B", "shippingCount": 2, "salesPrice": 5000}
                        ],
                    )
                ]
            else:
                data = []
            return httpx.Response(200, json={"data": data, "nextToken": ""})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        orders = connector.fetch_orders(date(2026, 1, 1), date(2026, 1, 2))

        assert len(orders) == 1
        assert orders[0]["platform_order_no"] == "ORDER-MULTI"
        assert len(orders[0]["items"]) == 2
        assert orders[0]["total_amount"] == 15000.0

    def test_pagination_follows_next_token(self, db_session, platform):
        _register_credentials(db_session, platform)
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            calls.append(params)
            # ACCEPT 상태에서만 2페이지로 나눠 주고, 나머지 상태는 빈 응답.
            if params.get("status") != "ACCEPT":
                return httpx.Response(200, json={"data": [], "nextToken": ""})
            if "nextToken" not in params:
                return httpx.Response(200, json={"data": [_order_box("P1", 1)], "nextToken": "TOKEN2"})
            return httpx.Response(200, json={"data": [_order_box("P2", 2)], "nextToken": ""})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        orders = connector.fetch_orders(date(2026, 1, 1), date(2026, 1, 2))

        # ACCEPT는 2페이지(2회) + 나머지 4개 상태 각 1회 = 6회.
        assert len(calls) == len(ORDERSHEET_STATUSES) + 1
        accept_calls = [c for c in calls if c.get("status") == "ACCEPT"]
        assert len(accept_calls) == 2
        assert accept_calls[1]["nextToken"] == "TOKEN2"
        assert {o["platform_order_no"] for o in orders} == {"P1", "P2"}

    def test_non_200_raises(self, db_session, platform):
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="Unauthorized")

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        # 401은 응답 본문 없이 AUTH_FAILED(비재시도) 외부 API 오류로 변환된다(더미 폴백 없음).
        with pytest.raises(MarketplaceExternalAPIError) as ei:
            connector.fetch_orders(date(2026, 1, 1), date(2026, 1, 2))
        assert ei.value.reason_code == "AUTH_FAILED"
        assert ei.value.retryable is False
        assert "Unauthorized" not in str(ei.value)
