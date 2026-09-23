"""
tests/unit/test_coupang_connector_product_inquiry.py
--------------------------------------------------------
CoupangConnector의 상용 ERP 확장(5단계, B묶음 후속) - 상품별 문의
(onlineInquiries) 조회를 MockTransport로 검증한다. 실제 외부 호출은 하지 않는다.

응답 스키마는 공식 문서(developers.coupang.com/hc/en-us/articles/
360033400754-Customer-Inquiry-Query-by-Product, 2026-09 재조회)의 Response
필드 설명을 그대로 재현한다 - 콜센터 문의(callCenterInquiries)와 달리
orderIds는 List이고, buyerPhone 등 PII 필드가 없으며, 항목별 답변여부 상태
필드가 없다(commentDtoList로만 파생). 답변(쓰기) API는 이번 단계에서도
구현하지 않으므로(모듈 docstring 참고) 여기서 검증하지 않는다.
"""

from datetime import date

import httpx

from integrations.malls.coupang_connector import COUPANG_API_BASE, ONLINE_INQUIRY_ANSWERED_TYPE_ALL, CoupangConnector
from services.settings_service import ApiCredentialService

VENDOR_ID = "A00012345"


def _register_credentials(db_session, platform):
    svc = ApiCredentialService(db_session)
    svc.upsert_credential("PLATFORM", platform.id, "access_key", "test-access-key")
    svc.upsert_credential("PLATFORM", platform.id, "secret_key", "test-secret-key")
    svc.upsert_credential("PLATFORM", platform.id, "vendor_id", VENDOR_ID)
    db_session.flush()


def _inquiry_item(
    inquiry_id=987654,
    product_id=1234567890,
    seller_product_id=1122334455,
    content="이 상품 재입고 언제 되나요?",
    order_ids=None,
    comments=None,
):
    return {
        "inquiryId": inquiry_id,
        "productId": product_id,
        "sellerProductId": seller_product_id,
        "content": content,
        "inquiryAt": "2026-01-10T09:00:00",
        "orderIds": order_ids if order_ids is not None else [],
        "commentDtoList": comments if comments is not None else [],
    }


def _paged_response(items, current_page=1, total_pages=1):
    return httpx.Response(
        200,
        json={
            "code": 200,
            "message": "OK",
            "data": {
                "content": items,
                "pagination": {
                    "currentPage": current_page,
                    "totalPages": total_pages,
                    "totalElements": len(items),
                    "countPerPage": 50,
                },
            },
        },
    )


class TestFetchProductInquiries:
    def test_normalizes_inquiry_without_answer(self, db_session, platform):
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            return _paged_response([_inquiry_item()])

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        results = connector.fetch_product_inquiries(date(2026, 1, 1), date(2026, 1, 7))

        assert len(results) == 1
        item = results[0]
        assert item["platform_inquiry_id"] == "987654"
        assert item["content"] == "이 상품 재입고 언제 되나요?"
        assert item["platform_order_no"] is None
        assert item["needs_answer"] is True
        assert item["raw_status"] == "NOANSWER"
        assert item["customer_phone"] is None
        assert item["inquiry_at"].year == 2026

    def test_answered_inquiry_derives_status_from_comments(self, db_session, platform):
        """항목별 답변여부 상태 필드가 응답 스키마에 없어(공식 문서 확인) commentDtoList
        존재 여부로만 파생한다."""
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            return _paged_response(
                [
                    _inquiry_item(
                        comments=[
                            {
                                "inquiryCommentId": 1,
                                "inquiryId": 987654,
                                "content": "빠른 시일 내 재입고 예정입니다.",
                                "inquiryCommentAt": "2026-01-10T10:00:00.000000+09:00",
                            }
                        ]
                    )
                ]
            )

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        results = connector.fetch_product_inquiries(date(2026, 1, 1), date(2026, 1, 7))

        assert results[0]["raw_status"] == "ANSWERED"
        assert results[0]["needs_answer"] is False

    def test_single_order_id_is_linked(self, db_session, platform):
        """orderIds가 정확히 1건이면 platform_order_no로 채운다."""
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            return _paged_response([_inquiry_item(order_ids=[28000008707838])])

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        results = connector.fetch_product_inquiries(date(2026, 1, 1), date(2026, 1, 7))

        assert results[0]["platform_order_no"] == "28000008707838"

    def test_zero_order_ids_is_not_linked(self, db_session, platform):
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            return _paged_response([_inquiry_item(order_ids=[])])

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        results = connector.fetch_product_inquiries(date(2026, 1, 1), date(2026, 1, 7))

        assert results[0]["platform_order_no"] is None

    def test_multiple_order_ids_is_not_linked(self, db_session, platform):
        """2건 이상이면 어느 주문이 진짜 관련 있는지 확인할 근거가 없어 임의로
        첫 번째를 고르지 않는다(요구사항: 확인된 식별자가 있을 때만 연결)."""
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            return _paged_response([_inquiry_item(order_ids=[111, 222])])

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        results = connector.fetch_product_inquiries(date(2026, 1, 1), date(2026, 1, 7))

        assert results[0]["platform_order_no"] is None

    def test_uses_answered_type_all_single_pass(self, db_session, platform):
        """콜센터 문의와 달리 상태별 순회가 필요 없다(answeredType=ALL 단일 호출)."""
        _register_credentials(db_session, platform)
        captured_answered_types = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_answered_types.append(dict(request.url.params).get("answeredType"))
            return _paged_response([])

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        connector.fetch_product_inquiries(date(2026, 1, 1), date(2026, 1, 7))

        assert captured_answered_types == [ONLINE_INQUIRY_ANSWERED_TYPE_ALL]

    def test_request_matches_official_contract(self, db_session, platform):
        """공식 문서: GET .../vendors/{vendorId}/onlineInquiries, 필수 쿼리
        (vendorId/answeredType/inquiryStartAt/inquiryEndAt), pageSize 최대 50."""
        _register_credentials(db_session, platform)
        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return _paged_response([])

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        connector.fetch_product_inquiries(date(2026, 1, 1), date(2026, 1, 7))

        request = captured[0]
        assert request.method == "GET"
        assert request.url.path.endswith("/vendors/" + request.url.params["vendorId"] + "/onlineInquiries")
        assert request.url.path.startswith("/v2/providers/openapi/apis/api/v5/vendors/")
        params = dict(request.url.params)
        assert {"vendorId", "answeredType", "inquiryStartAt", "inquiryEndAt"} <= set(params)
        assert params["inquiryStartAt"] == "2026-01-01"
        assert params["inquiryEndAt"] == "2026-01-07"
        assert 1 <= int(params["pageSize"]) <= 50
        assert request.headers["Authorization"].startswith("CEA ")

    def test_paginates_until_last_page(self, db_session, platform):
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            page = int(dict(request.url.params)["pageNum"])
            if page == 1:
                return _paged_response([_inquiry_item(inquiry_id=1)], current_page=1, total_pages=2)
            return _paged_response([_inquiry_item(inquiry_id=2)], current_page=2, total_pages=2)

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        results = connector.fetch_product_inquiries(date(2026, 1, 1), date(2026, 1, 7))

        assert {r["platform_inquiry_id"] for r in results} == {"1", "2"}

    def test_chunks_range_longer_than_seven_days(self, db_session, platform):
        """조회기간이 7일을 넘으면 내부적으로 7일 단위로 나눠 호출해야 한다(공식
        문서: inquiryEndAt - inquiryStartAt <= 7일)."""
        _register_credentials(db_session, platform)
        captured_ranges = []

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            captured_ranges.append((params["inquiryStartAt"], params["inquiryEndAt"]))
            return _paged_response([])

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        connector.fetch_product_inquiries(date(2026, 1, 1), date(2026, 1, 20))

        for start_str, end_str in captured_ranges:
            start = date.fromisoformat(start_str)
            end = date.fromisoformat(end_str)
            assert (end - start).days <= 6

    def test_missing_inquiry_id_excluded(self, db_session, platform):
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            item = _inquiry_item()
            del item["inquiryId"]
            return _paged_response([item])

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        results = connector.fetch_product_inquiries(date(2026, 1, 1), date(2026, 1, 7))

        assert results == []

    def test_no_pii_field_ever_present_in_normalized_output(self, db_session, platform):
        """이 응답에는 고객 이름/전화번호가 없다는 것을 공식 문서로 재확인했다 -
        원본 응답에 그런 필드가 우연히 섞여 들어와도 정규화 결과는 항상
        customer_phone=None이어야 한다(방어적)."""
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            item = _inquiry_item()
            item["buyerPhone"] = "010-9999-8888"  # 실제로는 없는 필드지만 방어적으로 확인.
            return _paged_response([item])

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        results = connector.fetch_product_inquiries(date(2026, 1, 1), date(2026, 1, 7))

        assert results[0]["customer_phone"] is None
