"""
tests/unit/test_coupang_connector_cs_inquiry.py
--------------------------------------------------
CoupangConnector의 상용 ERP 확장(5단계, B묶음) - 콜센터 문의(CS) 조회를
MockTransport로 검증한다. 실제 외부 호출은 하지 않는다.

응답 스키마는 공식 문서(developers.coupang.com/hc/en-us/articles/
360033645354-Query-of-Coupang-Contact-Center-Inquiries, 2026-09 조회)의
Response 필드 설명을 그대로 재현한다. 답변(쓰기) API는 이번 단계에서
구현하지 않으므로(모듈 docstring 참고) 여기서 검증하지 않는다.
"""

from datetime import date

import httpx

from integrations.malls.coupang_connector import CALL_CENTER_INQUIRY_STATUSES, COUPANG_API_BASE, CoupangConnector
from services.settings_service import ApiCredentialService

VENDOR_ID = "A00012345"


def _register_credentials(db_session, platform):
    svc = ApiCredentialService(db_session)
    svc.upsert_credential("PLATFORM", platform.id, "access_key", "test-access-key")
    svc.upsert_credential("PLATFORM", platform.id, "secret_key", "test-secret-key")
    svc.upsert_credential("PLATFORM", platform.id, "vendor_id", VENDOR_ID)
    db_session.flush()


def _inquiry_item(
    inquiry_id=123456,
    order_id=28000008707838,
    content="배송이 너무 늦어요",
    inquiry_status="progress",
    cs_partner_status="requestAnswer",
    buyer_phone="010-1234-5678",
):
    return {
        "inquiryId": inquiry_id,
        "inquiryStatus": inquiry_status,
        "csPartnerCounselingStatus": cs_partner_status,
        "vendorItemId": [3187044096],
        "itemName": "테스트 상품",
        "content": content,
        "answeredAt": None,
        "replies": [],
        "inquiryAt": "2026-01-10T09:00:00",
        "buyerPhone": buyer_phone,
        "orderId": order_id,
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
                    "countPerPage": 30,
                },
            },
        },
    )


class TestFetchInquiries:
    def test_normalizes_inquiry_with_all_fields(self, db_session, platform):
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            if dict(request.url.params).get("partnerCounselingStatus") == "NONE":
                return _paged_response([_inquiry_item()])
            return _paged_response([])

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        results = connector.fetch_inquiries(date(2026, 1, 1), date(2026, 1, 7))

        assert len(results) == 1
        item = results[0]
        assert item["platform_inquiry_id"] == "123456"
        assert item["content"] == "배송이 너무 늦어요"
        assert item["platform_order_no"] == "28000008707838"
        assert item["needs_answer"] is True
        assert item["raw_status"] == "progress:requestAnswer"
        assert item["customer_phone"] == "010-1234-5678"
        assert item["inquiry_at"].year == 2026

    def test_sweeps_all_required_statuses(self, db_session, platform):
        _register_credentials(db_session, platform)
        captured_statuses = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_statuses.append(dict(request.url.params).get("partnerCounselingStatus"))
            return _paged_response([])

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        connector.fetch_inquiries(date(2026, 1, 1), date(2026, 1, 7))

        assert set(captured_statuses) == set(CALL_CENTER_INQUIRY_STATUSES)

    def test_paginates_until_last_page(self, db_session, platform):
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            if params.get("partnerCounselingStatus") != "NONE":
                return _paged_response([])
            page = int(params["pageNum"])
            if page == 1:
                return _paged_response([_inquiry_item(inquiry_id=1)], current_page=1, total_pages=2)
            return _paged_response([_inquiry_item(inquiry_id=2)], current_page=2, total_pages=2)

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        results = connector.fetch_inquiries(date(2026, 1, 1), date(2026, 1, 7))

        assert {r["platform_inquiry_id"] for r in results} == {"1", "2"}

    def test_chunks_range_longer_than_seven_days(self, db_session, platform):
        """조회기간이 7일을 넘으면 내부적으로 7일 단위로 나눠 호출해야 한다(공식
        문서: 최대 7일 range 제약)."""
        _register_credentials(db_session, platform)
        captured_ranges = []

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            if params.get("partnerCounselingStatus") == "NONE":
                captured_ranges.append((params["inquiryStartAt"], params["inquiryEndAt"]))
            return _paged_response([])

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        connector.fetch_inquiries(date(2026, 1, 1), date(2026, 1, 20))

        for start_str, end_str in captured_ranges:
            start = date.fromisoformat(start_str)
            end = date.fromisoformat(end_str)
            assert (end - start).days <= 6

    def test_missing_inquiry_id_excluded(self, db_session, platform):
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            if dict(request.url.params).get("partnerCounselingStatus") == "NONE":
                item = _inquiry_item()
                del item["inquiryId"]
                return _paged_response([item])
            return _paged_response([])

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        results = connector.fetch_inquiries(date(2026, 1, 1), date(2026, 1, 7))

        assert results == []
