"""
tests/unit/test_coupang_connector_claims_settlement.py
--------------------------------------------------------------
CoupangConnector의 상용 ERP 확장(2단계) - 취소/반품/교환 클레임 및 정산(회차 요약/
주문단위 상세) 수집을 MockTransport로 검증한다. 실제 외부 호출은 하지 않는다.

응답 스키마는 공식 문서(developers.coupang.com, 2026-09 조회)의 Response 섹션에
실린 실제 예시 JSON을 그대로 재현한다:
- 반품/취소: /ko/api/returns/return-cancellation-request-list-query
- 교환: /ko/api/exchanges/query-a-list-of-exchange-requests
- 정산 회차: /ko/api/settlement/settlement-detail-query
- 정산 상세(매출내역): /ko/api/settlement/sales-detail-query
"""

from datetime import date
from decimal import Decimal

import httpx
import pytest

from integrations.malls.coupang_connector import COUPANG_API_BASE, RETURN_STATUS_CODES, CoupangConnector
from integrations.malls.errors import MarketplaceCredentialMissingError
from services.settings_service import ApiCredentialService

VENDOR_ID = "A00012345"


def _register_credentials(db_session, platform):
    svc = ApiCredentialService(db_session)
    svc.upsert_credential("PLATFORM", platform.id, "access_key", "test-access-key")
    svc.upsert_credential("PLATFORM", platform.id, "secret_key", "test-secret-key")
    svc.upsert_credential("PLATFORM", platform.id, "vendor_id", VENDOR_ID)
    db_session.flush()


def _return_item(
    order_id=28000008707838,
    receipt_id=50229613,
    receipt_type="RETURN",
    receipt_status="RETURNS_UNCHECKED",
    vendor_item_id=3187044096,
    cancel_count=1,
    shipping_units=-3000,
):
    """공식 문서 Response 예시(반품/취소 요청 목록 조회)를 그대로 재현한다."""
    return {
        "receiptId": receipt_id,
        "orderId": order_id,
        "paymentId": 28000009486604,
        "receiptType": receipt_type,
        "receiptStatus": receipt_status,
        "createdAt": "2025-01-15T14:17:13.973885-08:00",
        "modifiedAt": "2025-01-15T14:17:13.973885-08:00",
        "requesterName": "구*숙",
        "requesterPhoneNumber": "+1(555)444-1234",
        "requesterAddress": "서울특별시 송파구 송파대로 570 (신천동)",
        "requesterZipCode": "05510",
        "cancelReasonCategory1": "고객변심",
        "cancelReasonCategory2": "단순변심(사유없음)",
        "cancelReason": "",
        "cancelCountSum": cancel_count,
        "faultByType": "CUSTOMER",
        "returnItems": [
            {
                "vendorItemId": vendor_item_id,
                "vendorItemName": "스파오(SPAO) (#)시원하고 편안한 캉캉 롱스커트",
                "purchaseCount": 1,
                "cancelCount": cancel_count,
                "shipmentBoxId": 123456789012345678,
                "sellerProductId": 57623797,
            }
        ],
        "reasonCode": "CHANGEMIND",
        "reasonCodeText": "필요 없어짐 (단순 변심)",
        "returnShippingCharge": {"currencyCode": "KRW", "units": shipping_units, "nanos": 0},
    }


def _exchange_item(
    order_id=28000008707838,
    exchange_id=900001,
    exchange_status="RECEIPT",
    fault_type="CUSTOMER",
    vendor_item_id=3187044096,
    quantity=1,
):
    """공식 문서(교환 요청 목록 조회)로 확인된 필드 구조를 재현한다."""
    return {
        "exchangeId": exchange_id,
        "orderId": order_id,
        "vendorId": VENDOR_ID,
        "exchangeStatus": exchange_status,
        "faultType": fault_type,
        "exchangeAmount": 3000,
        "reasonCode": "SIZE",
        "reasonCodeText": "사이즈가 안 맞음",
        "createdAt": "2025-01-15T14:17:13.973885-08:00",
        "exchangeItemDtoV1s": [{"targetItemId": 1, "quantity": quantity, "orderItemId": 55}],
        "collectInformationsDto": {"returndeliveryItemDtos": [{"vendorItemId": vendor_item_id, "count": quantity}]},
    }


class TestCapabilityFlags:
    def test_return_exchange_settlement_are_supported(self):
        assert CoupangConnector.supports_return_sync is True
        assert CoupangConnector.supports_exchange_sync is True
        assert CoupangConnector.supports_settlement_sync is True
        assert CoupangConnector.supports_settlement_detail_sync is True

    def test_cancellation_bulk_collection_remains_unsupported(self):
        """쿠팡의 취소(CANCEL) 목록은 기간만으로 대량 조회할 공식 API 경로가 없다
        (status를 제외해야 하고, status가 없으면 orderId가 필수가 되기 때문 - 모듈
        상수 RETURN_STATUS_CODES 주석 참고). 추측으로 지원한다고 표시하지 않는다."""
        assert CoupangConnector.supports_cancellation_sync is False


class TestCredentialMissingFailsClosed:
    def test_fetch_returns_without_credentials_raises(self, db_session, platform):
        connector = CoupangConnector(session=db_session, platform_id=platform.id)
        with pytest.raises(MarketplaceCredentialMissingError):
            connector.fetch_returns(date(2026, 1, 1), date(2026, 1, 31))

    def test_fetch_exchanges_without_credentials_raises(self, db_session, platform):
        connector = CoupangConnector(session=db_session, platform_id=platform.id)
        with pytest.raises(MarketplaceCredentialMissingError):
            connector.fetch_exchanges(date(2026, 1, 1), date(2026, 1, 31))

    def test_fetch_settlements_without_credentials_raises(self, db_session, platform):
        connector = CoupangConnector(session=db_session, platform_id=platform.id)
        with pytest.raises(MarketplaceCredentialMissingError):
            connector.fetch_settlements(date(2026, 1, 1), date(2026, 1, 31))

    def test_fetch_settlement_details_without_credentials_raises(self, db_session, platform):
        connector = CoupangConnector(session=db_session, platform_id=platform.id)
        with pytest.raises(MarketplaceCredentialMissingError):
            connector.fetch_settlement_details(date(2026, 1, 1), date(2026, 1, 31))


class TestFetchReturns:
    def test_normalizes_return_with_all_fields(self, db_session, platform):
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            if dict(request.url.params).get("status") == "UC":
                return httpx.Response(200, json={"data": [_return_item()], "nextToken": ""})
            return httpx.Response(200, json={"data": [], "nextToken": ""})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        results = connector.fetch_returns(date(2026, 1, 1), date(2026, 1, 31))

        assert len(results) == 1
        r = results[0]
        assert r["platform_order_no"] == "28000008707838"
        assert r["platform_claim_id"] == "50229613"
        assert r["platform_order_item_no"] == "3187044096"
        assert r["status"] == "REQUESTED"  # RETURNS_UNCHECKED -> REQUESTED
        assert r["raw_status"] == "RETURNS_UNCHECKED"
        assert r["quantity"] == 1
        assert r["shipping_fee"] == Decimal("-3000")  # 배송비는 채널이 준 부호 그대로(고객 청구).
        assert r["refund_amount"] is None  # 이 API는 환불액을 제공하지 않는다 - 0으로 추정 금지.
        assert r["fault_type"] == "CUSTOMER"
        assert r["reason"] == "필요 없어짐 (단순 변심)"

    def test_excludes_cancel_receipt_type(self, db_session, platform):
        """같은 엔드포인트가 CANCEL 유형을 섞어 줄 가능성에 대비해 RETURN만 남긴다."""
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            if dict(request.url.params).get("status") == "UC":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            _return_item(receipt_id=1, receipt_type="RETURN"),
                            _return_item(receipt_id=2, receipt_type="CANCEL"),
                        ],
                        "nextToken": "",
                    },
                )
            return httpx.Response(200, json={"data": [], "nextToken": ""})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        results = connector.fetch_returns(date(2026, 1, 1), date(2026, 1, 31))

        assert [r["platform_claim_id"] for r in results] == ["1"]

    def test_sweeps_all_status_codes(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured_statuses = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_statuses.append(dict(request.url.params).get("status"))
            return httpx.Response(200, json={"data": [], "nextToken": ""})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        connector.fetch_returns(date(2026, 1, 1), date(2026, 1, 2))

        assert set(captured_statuses) == set(RETURN_STATUS_CODES)

    def test_unknown_receipt_status_maps_to_review_not_completed(self, db_session, platform):
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            if dict(request.url.params).get("status") == "UC":
                return httpx.Response(
                    200, json={"data": [_return_item(receipt_status="SOME_NEW_STATUS")], "nextToken": ""}
                )
            return httpx.Response(200, json={"data": [], "nextToken": ""})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        results = connector.fetch_returns(date(2026, 1, 1), date(2026, 1, 31))

        assert results[0]["status"] == "REVIEW"
        assert results[0]["raw_status"] == "SOME_NEW_STATUS"

    def test_no_pii_in_normalized_result(self, db_session, platform):
        """개인정보(수취인명/연락처/주소)가 정규화 결과에 포함되지 않는다."""
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            if dict(request.url.params).get("status") == "UC":
                return httpx.Response(200, json={"data": [_return_item()], "nextToken": ""})
            return httpx.Response(200, json={"data": [], "nextToken": ""})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        results = connector.fetch_returns(date(2026, 1, 1), date(2026, 1, 31))

        blob = str(results)
        assert "구*숙" not in blob
        assert "송파대로" not in blob
        assert "+1(555)444-1234" not in blob


class TestFetchExchanges:
    def test_normalizes_exchange_with_all_fields(self, db_session, platform):
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [_exchange_item()], "nextToken": ""})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        results = connector.fetch_exchanges(date(2026, 1, 1), date(2026, 1, 5))

        assert len(results) >= 1
        e = results[0]
        assert e["platform_order_no"] == "28000008707838"
        assert e["platform_claim_id"] == "900001"
        assert e["platform_order_item_no"] == "3187044096"
        assert e["status"] == "REQUESTED"  # RECEIPT -> REQUESTED
        assert e["raw_status"] == "RECEIPT"
        assert e["quantity"] == 1
        assert e["shipping_fee"] == Decimal("3000")
        assert e["fault_type"] == "CUSTOMER"

    def test_chunks_query_window_to_max_range_days(self, db_session, platform):
        """조회기간이 EXCHANGE_MAX_RANGE_DAYS(7일)를 넘으면 여러 번 나눠 호출한다."""
        _register_credentials(db_session, platform)
        call_windows = []

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            call_windows.append((params.get("createdAtFrom"), params.get("createdAtTo")))
            return httpx.Response(200, json={"data": [], "nextToken": ""})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        connector.fetch_exchanges(date(2026, 1, 1), date(2026, 1, 20))  # 19일 -> 최소 3회 분할

        assert len(call_windows) >= 3

    def test_missing_vendor_item_id_leaves_order_item_link_empty(self, db_session, platform):
        """회수정보(collectInformationsDto)가 아직 없는 초기 단계 교환은 라인 연결 없이
        주문 단위로만 남긴다(추측 금지)."""
        _register_credentials(db_session, platform)
        item = _exchange_item()
        del item["collectInformationsDto"]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [item], "nextToken": ""})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        results = connector.fetch_exchanges(date(2026, 1, 1), date(2026, 1, 5))

        assert results[0]["platform_order_item_no"] is None
        assert results[0]["platform_order_no"] == "28000008707838"  # 주문 단위 연결은 유지.


class TestFetchSettlements:
    def test_normalizes_monthly_settlement(self, db_session, platform):
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "settlementType": "MONTHLY",
                        "settlementDate": "2026-02-15",
                        "totalSale": 1000000,
                        "serviceFee": 100000,
                        "settlementTargetAmount": 900000,
                        "settlementAmount": 900000,
                        "finalAmount": 895000,
                        "lastAmount": 0,
                        "status": "DONE",
                    }
                ],
            )

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        results = connector.fetch_settlements(date(2026, 1, 1), date(2026, 1, 31))

        assert len(results) == 1
        s = results[0]
        assert s["settlement_type"] == "MONTHLY"
        assert s["status"] == "COMPLETED"
        assert s["settled_amount"] == Decimal("895000")
        assert s["expected_amount"] == Decimal("900000")
        assert s["settled_date"] == date(2026, 2, 15)

    def test_scheduled_settlement_not_yet_done(self, db_session, platform):
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "settlementType": "WEEKLY",
                        "settlementDate": "2026-02-20",
                        "settlementTargetAmount": 500000,
                        "status": "SUBJECT",
                    }
                ],
            )

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        results = connector.fetch_settlements(date(2026, 1, 1), date(2026, 1, 31))

        assert results[0]["status"] == "SCHEDULED"
        assert results[0]["settled_amount"] == Decimal("0")
        assert results[0]["settled_date"] is None

    def test_queries_each_month_in_range(self, db_session, platform):
        _register_credentials(db_session, platform)
        year_months = []

        def handler(request: httpx.Request) -> httpx.Response:
            year_months.append(dict(request.url.params).get("revenueRecognitionYearMonth"))
            return httpx.Response(200, json=[])

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        connector.fetch_settlements(date(2026, 1, 15), date(2026, 3, 5))

        assert year_months == ["2026-01", "2026-02", "2026-03"]


class TestFetchSettlementDetails:
    def test_normalizes_order_and_line_level_fields(self, db_session, platform):
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "orderId": 28000008707838,
                            "saleType": "SALE",
                            "saleDate": "2026-01-10",
                            "recognitionDate": "2026-01-12",
                            "settlementDate": "2026-01-20",
                            "finalSettlementDate": "2026-01-20",
                            "items": [
                                {
                                    "vendorItemId": 3187044096,
                                    "salePrice": 10000,
                                    "quantity": 2,
                                    "saleAmount": 20000,
                                    "serviceFee": 2000,
                                    "serviceFeeVat": 200,
                                    "settlementAmount": 17800,
                                }
                            ],
                        }
                    ],
                    "hasNext": False,
                    "nextToken": "",
                },
            )

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        results = connector.fetch_settlement_details(date(2026, 1, 1), date(2026, 1, 31))

        assert len(results) == 1
        d = results[0]
        assert d["platform_order_no"] == "28000008707838"
        assert d["platform_order_item_no"] == "3187044096"
        assert d["sale_type"] == "SALE"
        assert d["recognition_date"] == date(2026, 1, 12)
        assert d["gross_amount"] == Decimal("20000")
        assert d["fee_amount"] == Decimal("2200")
        assert d["net_amount"] == Decimal("17800")

    def test_refund_sale_type_keeps_sign_as_provided(self, db_session, platform):
        """REFUND 라인의 금액 부호는 채널이 준 값 그대로다 - 임의로 반전하지 않는다."""
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "orderId": 1,
                            "saleType": "REFUND",
                            "recognitionDate": "2026-01-12",
                            "settlementDate": "2026-01-20",
                            "items": [
                                {
                                    "vendorItemId": 55,
                                    "saleAmount": -20000,
                                    "serviceFee": -2000,
                                    "serviceFeeVat": -200,
                                    "settlementAmount": -17800,
                                }
                            ],
                        }
                    ],
                    "hasNext": False,
                    "nextToken": "",
                },
            )

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        results = connector.fetch_settlement_details(date(2026, 1, 1), date(2026, 1, 31))

        assert results[0]["sale_type"] == "REFUND"
        assert results[0]["net_amount"] == Decimal("-17800")

    def test_pagination_follows_next_token(self, db_session, platform):
        _register_credentials(db_session, platform)
        pages = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            if not params.get("token"):
                pages["n"] += 1
                return httpx.Response(
                    200,
                    json={
                        "data": [{"orderId": 1, "saleType": "SALE", "items": [{"vendorItemId": 1}]}],
                        "hasNext": True,
                        "nextToken": "TOKEN2",
                    },
                )
            pages["n"] += 1
            return httpx.Response(
                200,
                json={
                    "data": [{"orderId": 2, "saleType": "SALE", "items": [{"vendorItemId": 2}]}],
                    "hasNext": False,
                    "nextToken": "",
                },
            )

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        results = connector.fetch_settlement_details(date(2026, 1, 1), date(2026, 1, 15))

        assert pages["n"] == 2
        assert {r["platform_order_no"] for r in results} == {"1", "2"}
