"""
tests/unit/test_coupang_claim_volume_limits.py
--------------------------------------------------
쿠팡 반품·교환·취소 단건 조회의 호출량 제한(페이지 상한·nextToken 방어·retry·공유 요청 예산)을 MockTransport로
검증한다. 실제 외부 호출은 하지 않고, 요청이 "네트워크로 나갔는지"는 핸들러 호출 수로 센다.
"""

from datetime import date, timedelta
from typing import Callable

import httpx
import pytest

from integrations.malls import coupang_connector
from integrations.malls.coupang_connector import (
    COUPANG_API_BASE,
    EXCHANGE_MAX_RANGE_DAYS,
    RETURN_MAX_RANGE_DAYS,
    RETURN_STATUS_CODES,
    CoupangConnector,
)
from integrations.malls.errors import MarketplaceExternalAPIError, RequestBudget
from services.settings_service import ApiCredentialService

VENDOR_ID = "A00012345"
ACCESS_KEY = "test-access-key"
TODAY = date(2026, 10, 8)
START = TODAY - timedelta(days=3)


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    monkeypatch.setattr(coupang_connector.time_module, "sleep", lambda seconds: None)


def _connector(db_session, platform, handler: Callable[[httpx.Request], httpx.Response]) -> CoupangConnector:
    svc = ApiCredentialService(db_session)
    svc.upsert_credential("PLATFORM", platform.id, "access_key", ACCESS_KEY)
    svc.upsert_credential("PLATFORM", platform.id, "secret_key", "test-secret-key")
    svc.upsert_credential("PLATFORM", platform.id, "vendor_id", VENDOR_ID)
    db_session.flush()
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
    return CoupangConnector(session=db_session, platform_id=platform.id, http_client=client)


class _Server:
    """요청 수를 세고 응답을 만들어 주는 가짜 서버."""

    def __init__(self, respond: Callable[[int, httpx.Request], httpx.Response]):
        self.requests: list[httpx.Request] = []
        self._respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(len(self.requests), request)


def _ok(data=None, token=""):
    body: dict = {"code": 200, "data": data if data is not None else []}
    if token:
        body["nextToken"] = token
    return httpx.Response(200, json=body)


def _item(i: int) -> dict:
    return {"receiptId": i, "orderId": 1000 + i, "receiptType": "RETURN", "receiptStatus": "RETURNS_UNCHECKED"}


class TestRequestCountFollowsTheQueryLayout:
    def test_returns_use_one_request_per_status_in_a_three_day_window(self, db_session, platform):
        server = _Server(lambda n, r: _ok([]))
        conn = _connector(db_session, platform, server)
        budget = RequestBudget(max_requests=100)

        conn.fetch_returns(START, TODAY, max_pages=3, max_retries=2, request_budget=budget)

        assert len(server.requests) == len(RETURN_STATUS_CODES) == budget.used == 4
        statuses = [r.url.params["status"] for r in server.requests]
        assert statuses == list(RETURN_STATUS_CODES)

    def test_exchanges_use_one_request_in_a_three_day_window(self, db_session, platform):
        server = _Server(lambda n, r: _ok([]))
        conn = _connector(db_session, platform, server)
        budget = RequestBudget(max_requests=100)

        conn.fetch_exchanges(START, TODAY, max_pages=3, max_retries=2, request_budget=budget)

        assert len(server.requests) == budget.used == 1

    def test_worst_case_estimates_match_the_default_budget_arithmetic(self, db_session, platform):
        conn = _connector(db_session, platform, _Server(lambda n, r: _ok([])))
        assert conn.estimate_claim_requests("returns", START, TODAY, 3, 2) == 4 * 1 * 3 * 3 == 36
        assert conn.estimate_claim_requests("exchanges", START, TODAY, 3, 2) == 1 * 3 * 3 == 9
        assert conn.estimate_claim_requests("cancellation_lookup", START, TODAY, 3, 2) == 3
        # 기본값 총합: 목록 45 + 취소 단건 20 x 3 = 105
        assert 36 + 9 + 20 * 3 == 105

    def test_estimates_grow_with_the_window_exactly_like_the_loops(self, db_session, platform):
        conn = _connector(db_session, platform, _Server(lambda n, r: _ok([])))
        base = TODAY - timedelta(days=RETURN_MAX_RANGE_DAYS)
        assert conn.estimate_claim_requests("returns", base, TODAY, 1, 0) == 4  # 31일 창 1개
        assert conn.estimate_claim_requests("returns", base - timedelta(days=1), TODAY, 1, 0) == 8  # 창 2개
        week = TODAY - timedelta(days=EXCHANGE_MAX_RANGE_DAYS)
        assert conn.estimate_claim_requests("exchanges", week, TODAY, 1, 0) == 1
        assert conn.estimate_claim_requests("exchanges", week - timedelta(days=1), TODAY, 1, 0) == 2
        assert conn.estimate_claim_requests("returns", TODAY, TODAY, 3, 2) == 0  # 빈 기간 = 요청 없음
        assert conn.estimate_claim_requests("unknown", START, TODAY, 3, 2) is None

    def test_estimate_equals_the_real_number_of_requests_when_every_query_hits_the_page_limit(
        self, db_session, platform
    ):
        """쿼리마다 max_pages까지 꽉 차는 경우의 실제 요청 수가 추정치의 "페이지" 항과 일치한다(교환 1창)."""
        server = _Server(lambda n, r: _ok([_item(n)], token=f"tok{n}"))
        conn = _connector(db_session, platform, server)
        with pytest.raises(MarketplaceExternalAPIError):
            conn.fetch_exchanges(START, TODAY, max_pages=3, max_retries=0, request_budget=RequestBudget(100))
        assert len(server.requests) == 3


class TestPageLimitAndTokenDefenses:
    def test_page_limit_stops_without_requesting_the_next_page(self, db_session, platform):
        server = _Server(lambda n, r: _ok([_item(n)], token=f"tok{n}"))
        conn = _connector(db_session, platform, server)

        with pytest.raises(MarketplaceExternalAPIError) as exc:
            conn.fetch_returns(START, TODAY, max_pages=3, max_retries=2, request_budget=RequestBudget(100))

        assert exc.value.reason_code == "PAGE_LIMIT_EXCEEDED" and exc.value.retryable is False
        assert len(server.requests) == 3  # 4번째 페이지 요청은 보내지 않는다

    def test_repeated_next_token_is_a_loop_and_stops_immediately(self, db_session, platform):
        server = _Server(lambda n, r: _ok([_item(n)], token="same-token"))
        conn = _connector(db_session, platform, server)

        with pytest.raises(MarketplaceExternalAPIError) as exc:
            conn.fetch_returns(START, TODAY, max_pages=50, max_retries=2, request_budget=RequestBudget(1000))

        assert exc.value.reason_code == "PAGINATION_LOOP"
        assert len(server.requests) == 2  # 첫 요청 + 같은 토큰으로 간 두 번째 요청에서 반복 확인

    def test_token_returned_on_an_empty_page_is_an_anomaly(self, db_session, platform):
        server = _Server(lambda n, r: _ok([], token="tok"))
        conn = _connector(db_session, platform, server)

        with pytest.raises(MarketplaceExternalAPIError) as exc:
            conn.fetch_exchanges(START, TODAY, max_pages=50, max_retries=2, request_budget=RequestBudget(1000))

        assert exc.value.reason_code == "PAGINATION_ANOMALY"
        assert len(server.requests) == 1

    def test_loop_is_blocked_even_without_a_page_limit(self, db_session, platform):
        """max_pages를 주지 않는 기존 호출부도 무한 순회하지 않는다."""
        server = _Server(lambda n, r: _ok([_item(n)], token="same-token"))
        conn = _connector(db_session, platform, server)
        with pytest.raises(MarketplaceExternalAPIError) as exc:
            conn.fetch_returns(START, TODAY)
        assert exc.value.reason_code == "PAGINATION_LOOP"
        assert len(server.requests) == 2

    def test_cancellation_lookup_is_limited_to_a_single_page(self, db_session, platform):
        server = _Server(lambda n, r: _ok([_item(n)], token="more"))
        conn = _connector(db_session, platform, server)
        with pytest.raises(MarketplaceExternalAPIError) as exc:
            conn.fetch_cancellation_status("ORDER-X", START, max_retries=2, request_budget=RequestBudget(100))
        assert exc.value.reason_code == "PAGE_LIMIT_EXCEEDED"
        assert len(server.requests) == 1


class TestRetriesAndBudgetAreChargedBeforeSending:
    def test_every_429_retry_consumes_budget_and_retries_stop_at_the_limit(self, db_session, platform):
        server = _Server(lambda n, r: httpx.Response(429))
        conn = _connector(db_session, platform, server)
        budget = RequestBudget(max_requests=100)

        with pytest.raises(MarketplaceExternalAPIError) as exc:
            conn.fetch_exchanges(START, TODAY, max_pages=3, max_retries=2, request_budget=budget)

        assert exc.value.reason_code == "RATE_LIMITED"
        assert len(server.requests) == 3 == budget.used  # 최초 1 + retry 2

    def test_budget_blocks_the_request_before_it_reaches_the_network(self, db_session, platform):
        server = _Server(lambda n, r: httpx.Response(429))
        conn = _connector(db_session, platform, server)
        budget = RequestBudget(max_requests=2)

        with pytest.raises(MarketplaceExternalAPIError) as exc:
            conn.fetch_exchanges(START, TODAY, max_pages=3, max_retries=5, request_budget=budget)

        assert exc.value.reason_code == "REQUEST_BUDGET_EXCEEDED"
        assert len(server.requests) == 2  # 세 번째 시도는 전송되지 않았다

    def test_one_budget_is_shared_across_the_four_return_statuses(self, db_session, platform):
        server = _Server(lambda n, r: _ok([]))
        conn = _connector(db_session, platform, server)
        budget = RequestBudget(max_requests=3)

        with pytest.raises(MarketplaceExternalAPIError) as exc:
            conn.fetch_returns(START, TODAY, max_pages=3, max_retries=2, request_budget=budget)

        assert exc.value.reason_code == "REQUEST_BUDGET_EXCEEDED"
        assert len(server.requests) == 3

    def test_budget_is_shared_between_list_queries_and_cancellation_lookups(self, db_session, platform):
        server = _Server(lambda n, r: _ok([]))
        conn = _connector(db_session, platform, server)
        budget = RequestBudget(max_requests=6)

        conn.fetch_returns(START, TODAY, max_pages=3, max_retries=2, request_budget=budget)  # 4
        conn.fetch_exchanges(START, TODAY, max_pages=3, max_retries=2, request_budget=budget)  # 1
        assert budget.used == 5
        conn.fetch_cancellation_status("ORDER-A", START, max_retries=2, request_budget=budget)  # 1
        with pytest.raises(MarketplaceExternalAPIError) as exc:
            conn.fetch_cancellation_status("ORDER-B", START, max_retries=2, request_budget=budget)
        assert exc.value.reason_code == "REQUEST_BUDGET_EXCEEDED"
        assert len(server.requests) == 6


class TestNothingSensitiveLeaksIntoErrors:
    def test_error_text_has_no_credential_vendor_or_order_values(self, db_session, platform):
        server = _Server(lambda n, r: _ok([_item(n)], token="secret-looking-token"))
        conn = _connector(db_session, platform, server)
        with pytest.raises(MarketplaceExternalAPIError) as exc:
            conn.fetch_cancellation_status(
                "ORDER-SENSITIVE-777", START, max_retries=2, request_budget=RequestBudget(10)
            )
        text = f"{exc.value} {exc.value.reason_code!r} {exc.value.args!r}"
        for forbidden in (ACCESS_KEY, "test-secret-key", VENDOR_ID, "ORDER-SENSITIVE-777", "secret-looking-token"):
            assert forbidden not in text


class _LiveVerify:
    """실계정 첫 검증 프리셋(1일, 1페이지, retry 0, 최대 5회)이 어떤 응답에서도 5회를 넘지 못함을 실제 커넥터로 확인."""

    @staticmethod
    def run(db_session, platform, respond):
        from services.claim_sync_service import ClaimSyncLimits, ClaimSyncService

        server = _Server(respond)
        conn = _connector(db_session, platform, server)
        limits = ClaimSyncLimits(max_pages=1, max_retries=0, budget=RequestBudget(max_requests=5))
        result = ClaimSyncService(db_session).sync_claims(
            conn, platform.id, TODAY - timedelta(days=1), TODAY, limits, frozenset({"returns", "exchanges"})
        )
        return server, limits, result


class TestLiveVerifyNeverExceedsFiveRequests:
    def test_all_empty_responses_use_exactly_four_returns_plus_one_exchange(self, db_session, platform):
        server, limits, result = _LiveVerify.run(db_session, platform, lambda n, r: _ok([]))
        assert len(server.requests) == limits.budget.used == 5
        assert result["overall_status"] == "SUCCESS" and result["requests_used"] == 5
        assert all("returnRequests" in r.url.path for r in server.requests[:4])
        assert "exchangeRequests" in server.requests[4].url.path
        assert not any("cancel" in str(r.url).lower() and "orderId" in str(r.url) for r in server.requests)

    def test_rate_limited_server_costs_one_request_per_feature_because_retry_is_zero(self, db_session, platform):
        server, limits, result = _LiveVerify.run(db_session, platform, lambda n, r: httpx.Response(429))
        assert len(server.requests) == 2  # 반품 첫 쿼리 1 + 교환 1, 재시도 없음
        assert (
            result["returns"]["reason_code"] == "RATE_LIMITED" and result["exchanges"]["reason_code"] == "RATE_LIMITED"
        )

    def test_a_next_page_is_never_requested(self, db_session, platform):
        server, limits, result = _LiveVerify.run(db_session, platform, lambda n, r: _ok([_item(n)], token=f"tok{n}"))
        assert len(server.requests) == 2
        assert result["returns"]["reason_code"] == "PAGE_LIMIT_EXCEEDED"
        assert limits.budget.used <= 5


class TestPagesBeforeThePageLimitAreNeverPartiallySaved:
    def _return_item(self, i: int) -> dict:
        return {
            "receiptId": 9000 + i,
            "orderId": 7000 + i,
            "receiptType": "RETURN",
            "receiptStatus": "RETURNS_UNCHECKED",
            "createdAt": "2025-01-15T14:17:13.973885-08:00",
            "cancelCountSum": 1,
            "returnItems": [{"vendorItemId": 1, "cancelCount": 1}],
            "reasonCodeText": "사유",
        }

    def test_valid_items_received_on_pages_one_to_three_are_not_saved_when_the_limit_is_hit(self, db_session, platform):
        from datetime import datetime, timezone

        from models.order import Order, Return
        from services.claim_sync_service import ClaimSyncLimits, ClaimSyncService

        for i in range(1, 4):  # 받은 3페이지의 항목이 전부 저장 가능한 상태(주문이 이미 있다)
            db_session.add(
                Order(
                    platform_id=platform.id,
                    platform_order_no=str(7000 + i),
                    status="DELIVERED",
                    order_date=datetime.now(timezone.utc),
                    total_amount=1000,
                    discount_amount=0,
                )
            )
        db_session.flush()
        server = _Server(lambda n, r: _ok([self._return_item(n)], token=f"tok{n}"))
        conn = _connector(db_session, platform, server)
        limits = ClaimSyncLimits(max_pages=3, max_retries=2, budget=RequestBudget(max_requests=105))

        result = ClaimSyncService(db_session).sync_claims(
            conn, platform.id, START, TODAY, limits, frozenset({"returns"})
        )
        db_session.flush()

        assert len(server.requests) == 3  # 3페이지를 받았고 4번째는 요청하지 않았다
        assert result["returns"]["reason_code"] == "PAGE_LIMIT_EXCEEDED"
        assert db_session.query(Return).count() == 0  # 받은 페이지의 항목이 부분 저장되지 않았다
