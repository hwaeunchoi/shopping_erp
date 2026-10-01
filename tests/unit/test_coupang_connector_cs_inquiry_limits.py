"""
tests/unit/test_coupang_connector_cs_inquiry_limits.py
-----------------------------------------------------------
CoupangConnector의 CS 문의(콜센터 문의/상품별 문의) 조회에 대한 호출량 안전
상한(상용 ERP 확장 5단계 B묶음 보완)을 MockTransport로 검증한다. 실제 외부
호출은 하지 않는다.

여기서 검증하는 것 - "현재의 무제한 pagination을 그대로 활성화하지 말 것"
요구사항의 코드 근거:
- max_pages에 도달했는데 응답이 다음 페이지가 더 있다고 알리면 성공/부분성공으로
  위장하지 않고 PAGE_LIMIT_EXCEEDED로 그 쿼리(상태/answeredType 1개 x 7일 창 1개)를
  즉시 실패시킨다 - 그 이후 페이지는 요청하지 않는다(요청 수로 직접 확인).
- max_retries는 페이지 1회 요청당 429 재시도 횟수를 그대로 제한한다(0이면 즉시
  RATE_LIMITED).
- request_budget은 최초 시도·재시도를 가리지 않고 실제로 네트워크로 나가는 모든
  요청에 소비되며, 소진되면 요청을 보내지 않고 즉시 REQUEST_BUDGET_EXCEEDED로 막는다
  (핸들러가 호출되지 않았음을 직접 확인한다).
"""

from datetime import date

import httpx
import pytest

from integrations.malls.coupang_connector import COUPANG_API_BASE, CoupangConnector
from integrations.malls.errors import MarketplaceExternalAPIError, RequestBudget
from services.settings_service import ApiCredentialService

VENDOR_ID = "A00012345"


def _register_credentials(db_session, platform):
    svc = ApiCredentialService(db_session)
    svc.upsert_credential("PLATFORM", platform.id, "access_key", "test-access-key")
    svc.upsert_credential("PLATFORM", platform.id, "secret_key", "test-secret-key")
    svc.upsert_credential("PLATFORM", platform.id, "vendor_id", VENDOR_ID)
    db_session.flush()


def _paged_response(items, current_page=1, total_pages=1, count_per_page=30):
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
                    "countPerPage": count_per_page,
                },
            },
        },
    )


def _call_center_item(inquiry_id=1):
    return {
        "inquiryId": inquiry_id,
        "inquiryStatus": "progress",
        "csPartnerCounselingStatus": "requestAnswer",
        "content": "문의 본문",
        "inquiryAt": "2026-01-10T09:00:00",
        "buyerPhone": "010-0000-0000",
        "orderId": None,
    }


def _product_item(inquiry_id=1):
    return {
        "inquiryId": inquiry_id,
        "productId": 1,
        "sellerProductId": 1,
        "content": "문의 본문",
        "inquiryAt": "2026-01-10T09:00:00",
        "orderIds": [],
        "commentDtoList": [],
    }


class TestCallCenterPageLimit:
    def test_page_limit_exceeded_when_more_pages_remain(self, db_session, platform):
        """page 1을 받은 뒤에도 total_pages>1인데 max_pages=1이면, page 2는 절대
        요청하지 않고 PAGE_LIMIT_EXCEEDED로 실패해야 한다."""
        _register_credentials(db_session, platform)
        requested_pages = []

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            if params.get("partnerCounselingStatus") != "NONE":
                return _paged_response([])
            requested_pages.append(int(params["pageNum"]))
            return _paged_response([_call_center_item()], current_page=1, total_pages=2)

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        with pytest.raises(MarketplaceExternalAPIError) as exc_info:
            connector.fetch_inquiries(date(2026, 1, 1), date(2026, 1, 7), max_pages=1)

        assert exc_info.value.reason_code == "PAGE_LIMIT_EXCEEDED"
        assert requested_pages == [1]  # page 2는 요청되지 않았다.

    def test_within_page_limit_succeeds_without_extra_request(self, db_session, platform):
        """마지막 페이지가 max_pages 이내면(여기서는 1페이지로 끝남) 정상 성공하고
        추가 페이지를 요청하지 않는다."""
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            if params.get("partnerCounselingStatus") != "NONE":
                return _paged_response([])
            return _paged_response([_call_center_item()], current_page=1, total_pages=1)

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        results = connector.fetch_inquiries(date(2026, 1, 1), date(2026, 1, 7), max_pages=1)

        assert len(results) == 1

    def test_exactly_at_page_limit_but_more_pages_exist_still_fails(self, db_session, platform):
        """max_pages=2이고 실제로 2페이지째까지 받았는데 totalPages=3(더 있음)이면
        2페이지는 정상적으로 받고 그 다음에 실패해야 한다(가져온 페이지는 DB에
        저장되지 않음 - services 레벨 테스트에서 별도 확인)."""
        _register_credentials(db_session, platform)
        requested_pages = []

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            if params.get("partnerCounselingStatus") != "NONE":
                return _paged_response([])
            page = int(params["pageNum"])
            requested_pages.append(page)
            return _paged_response([_call_center_item(inquiry_id=page)], current_page=page, total_pages=3)

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        with pytest.raises(MarketplaceExternalAPIError) as exc_info:
            connector.fetch_inquiries(date(2026, 1, 1), date(2026, 1, 7), max_pages=2)

        assert exc_info.value.reason_code == "PAGE_LIMIT_EXCEEDED"
        assert requested_pages == [1, 2]  # 3페이지는 요청되지 않았다.


class TestProductInquiryPageLimit:
    def test_page_limit_exceeded_when_more_pages_remain(self, db_session, platform):
        _register_credentials(db_session, platform)
        requested_pages = []

        def handler(request: httpx.Request) -> httpx.Response:
            page = int(dict(request.url.params)["pageNum"])
            requested_pages.append(page)
            return _paged_response([_product_item()], current_page=1, total_pages=2)

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        with pytest.raises(MarketplaceExternalAPIError) as exc_info:
            connector.fetch_product_inquiries(date(2026, 1, 1), date(2026, 1, 1), max_pages=1)

        assert exc_info.value.reason_code == "PAGE_LIMIT_EXCEEDED"
        assert requested_pages == [1]

    def test_manual_limited_sync_shape_single_day_single_page_zero_retry(self, db_session, platform):
        """수동 제한 검증 경로(상품별 문의 1소스·1일·max_pages=1·retry=0)를 그대로
        재현한다 - 정상 응답(1페이지로 끝남)이면 그대로 성공해야 한다."""
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            assert params["inquiryStartAt"] == params["inquiryEndAt"] == "2026-01-01"
            return _paged_response([_product_item()], current_page=1, total_pages=1)

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        results = connector.fetch_product_inquiries(date(2026, 1, 1), date(2026, 1, 1), max_pages=1, max_retries=0)

        assert len(results) == 1


class TestRetryLimit:
    def test_zero_retries_raises_immediately_on_429(self, db_session, platform):
        _register_credentials(db_session, platform)
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"code": "429"})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        with pytest.raises(MarketplaceExternalAPIError) as exc_info:
            connector.fetch_product_inquiries(date(2026, 1, 1), date(2026, 1, 1), max_pages=1, max_retries=0)

        assert exc_info.value.reason_code == "RATE_LIMITED"
        assert len(attempts) == 1  # 재시도 없이 최초 시도 1회로 끝난다.

    def test_n_retries_allows_exactly_n_plus_one_attempts(self, db_session, platform):
        """max_retries=2면 최초 시도 + 재시도 2회 = 최대 3번 시도까지 허용된다 -
        세 번째 시도가 성공하면 그대로 성공 처리된다."""
        _register_credentials(db_session, platform)
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) < 3:
                return httpx.Response(429, headers={"Retry-After": "0"}, json={"code": "429"})
            return _paged_response([_product_item()], current_page=1, total_pages=1)

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        results = connector.fetch_product_inquiries(date(2026, 1, 1), date(2026, 1, 1), max_pages=1, max_retries=2)

        assert len(results) == 1
        assert len(attempts) == 3


class TestRequestBudgetEnforcement:
    def test_budget_blocks_request_before_it_is_sent(self, db_session, platform):
        """예산이 0이면 handler가 전혀 호출되지 않아야 한다(요청을 보낸 뒤 버리는
        방식이 아니라 보내기 전에 막는 방식임을 직접 증명)."""
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("예산이 0인데 실제 HTTP 요청이 전송되었습니다.")

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)
        budget = RequestBudget(max_requests=0)

        with pytest.raises(MarketplaceExternalAPIError) as exc_info:
            connector.fetch_product_inquiries(date(2026, 1, 1), date(2026, 1, 1), max_pages=1, request_budget=budget)

        assert exc_info.value.reason_code == "REQUEST_BUDGET_EXCEEDED"
        assert budget.used == 0

    def test_retries_consume_the_shared_budget(self, db_session, platform):
        """재시도도 예산을 소비한다 - 최초 시도 1회 + 재시도 1회로 성공하면 budget.used==2."""
        _register_credentials(db_session, platform)
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) == 1:
                return httpx.Response(429, headers={"Retry-After": "0"}, json={"code": "429"})
            return _paged_response([_product_item()], current_page=1, total_pages=1)

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)
        budget = RequestBudget(max_requests=5)

        connector.fetch_product_inquiries(
            date(2026, 1, 1), date(2026, 1, 1), max_pages=1, max_retries=1, request_budget=budget
        )

        assert budget.used == 2

    def test_call_center_four_statuses_plus_product_inquiry_absolute_max(self, db_session, platform):
        """콜센터 4상태 + 상품별 문의 1종을 합친 절대 요청 상한 - config/settings.py의
        계산식(기본값: 5개 쿼리 x max_pages x (1+max_retries))을 그대로 재현한다.
        여기서는 상태/쿼리별로 정확히 1페이지만 받도록 해 "성공 시 소비량"이 쿼리
        개수(5)와 정확히 일치하는지 확인한다."""
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            if "callCenterInquiries" in request.url.path:
                return _paged_response([_call_center_item()], current_page=1, total_pages=1)
            return _paged_response([_product_item()], current_page=1, total_pages=1)

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)
        budget = RequestBudget(max_requests=45)

        connector.fetch_inquiries(date(2026, 1, 1), date(2026, 1, 1), max_pages=3, max_retries=2, request_budget=budget)
        connector.fetch_product_inquiries(
            date(2026, 1, 1), date(2026, 1, 1), max_pages=3, max_retries=2, request_budget=budget
        )

        # 콜센터 상태 4종 + 상품별 문의 1종, 각 1요청(1페이지로 끝남) = 5.
        assert budget.used == 5
        assert budget.used <= 45  # 기본 설정(config/settings.py)의 절대 상한 안에 든다.
