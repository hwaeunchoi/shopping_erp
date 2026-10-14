"""
tests/unit/test_cancellation_candidate_policy.py
---------------------------------------------------
쿠팡 취소 "후보 주문 단건 조회"의 후보 검증·오류 분류·결과 상태·검증 전용 후보 선택 정책을 실제 CoupangConnector(MockTransport)와 스텁으로 검증한다.

- orderId 는 공식 문서상 number 이므로 ASCII 숫자만 HTTP 로 보낸다(길이 조건 없음). 무효 후보는 HTTP 0회, cursor 는 그 후보를 지나간다.
- 400 등 원인 불명 비-200 은 fail-closed(배치 중단, cursor 는 실패한 주문 앞), 412 는 공식 문서상 read timeout 이라 설정 retry 범위에서만 재시도한다.
- 한 건도 확인하지 못했는데 실패·무효 후보가 있으면 SUCCESS 가 아니다. 검증 전용 선택은 운영 cursor 와 독립이다.
외부 호출은 없고 설정은 전부 명시한다. 실패 출력에 주문번호가 나오지 않도록 단언은 불리언·개수로만 한다.
"""

import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from config import logging_config
from integrations.malls import coupang_connector
from integrations.malls.coupang_connector import COUPANG_API_BASE, CoupangConnector
from integrations.malls.errors import MarketplaceExternalAPIError, RequestBudget
from models.order import ClaimCollectionCursor, Order
from services.claim_sync_service import ClaimSyncLimits, ClaimSyncService
from services.settings_service import ApiCredentialService

BASE_TIME = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
LEGACY = "C202607030001"


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    monkeypatch.setattr(coupang_connector.time_module, "sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def _production_http_log_suppression():
    """운영은 setup_logging() 이 httpx/httpcore/urllib3 를 WARNING 으로 막는다 - 단위 테스트도 같은 조건에서 로그를 검사한다."""
    names = ("httpx", "httpcore", "urllib3")
    saved = {n: logging.getLogger(n).level for n in names}
    logging_config.quiet_outbound_http_loggers()
    yield
    for n, level in saved.items():
        logging.getLogger(n).setLevel(level)


def _limits(retries=0, budget=105):
    return ClaimSyncLimits(max_pages=1, max_retries=retries, budget=RequestBudget(max_requests=budget))


def _add(db_session, platform, numbers, *, same_time=False, status="NEW"):
    rows = []
    for i, no in enumerate(numbers):
        o = Order(
            platform_id=platform.id,
            platform_order_no=no,
            status=status,
            order_date=BASE_TIME if same_time else BASE_TIME + timedelta(minutes=i),
            total_amount=1000,
            discount_amount=0,
        )
        db_session.add(o)
        rows.append(o)
    db_session.flush()
    return rows


def _num(i):
    return str(28000000000000 + i)


def _connector(db_session, platform, handler):
    svc = ApiCredentialService(db_session)
    svc.upsert_credential("PLATFORM", platform.id, "access_key", "synthetic-access-policy")
    svc.upsert_credential("PLATFORM", platform.id, "secret_key", "synthetic-secret-policy")
    svc.upsert_credential("PLATFORM", platform.id, "vendor_id", "A00099001")
    db_session.flush()
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
    return CoupangConnector(session=db_session, platform_id=platform.id, http_client=client)


def _empty_ok(request):
    return httpx.Response(200, json={"code": 200, "message": "OK", "data": []})


def _cursor(db_session, platform):
    return db_session.query(ClaimCollectionCursor.last_order_id).filter_by(platform_id=platform.id).scalar()


class _Recorder:
    def __init__(self, respond=_empty_ok):
        self.respond = respond
        self.orders: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.orders.append(request.url.params["orderId"])
        return self.respond(request)


class TestAsciiDigitValidation:
    @pytest.mark.parametrize("value", ["1", "0123", "28000008707838", "9" * 5, "9" * 30])
    def test_ascii_digit_strings_of_any_length_are_valid(self, db_session, platform, value):
        conn = _connector(db_session, platform, _empty_ok)
        assert conn.is_valid_cancellation_lookup_order_no(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            "",
            LEGACY,
            " 28000008707838",
            "28000008707838 ",
            "+28000008707838",
            "-1",
            "1.5",
            "12 3",
            "123\n",
            "１２３４",  # 전각 숫자
            "١٢٣٤",  # 아라비아-인도 숫자: str.isdigit() 은 True 지만 ASCII 숫자가 아니다
            "²³",  # 위첨자 숫자
        ],
    )
    def test_everything_else_is_invalid(self, db_session, platform, value):
        conn = _connector(db_session, platform, _empty_ok)
        assert conn.is_valid_cancellation_lookup_order_no(value) is False

    def test_the_connector_refuses_to_send_an_invalid_order_number_even_if_called_directly(self, db_session, platform):
        rec = _Recorder()
        conn = _connector(db_session, platform, rec)
        with pytest.raises(MarketplaceExternalAPIError) as info:
            conn.fetch_cancellation_status(LEGACY, BASE_TIME.date(), max_retries=0, request_budget=RequestBudget(5))
        assert info.value.reason_code == "INVALID_PLATFORM_ORDER_NO"
        assert rec.orders == []

    def test_other_platforms_have_no_numeric_rule(self, db_session, platform):
        from integrations.malls.base_mall_connector import BaseMallConnector

        assert BaseMallConnector.is_valid_cancellation_lookup_order_no(object(), LEGACY) is True  # type: ignore[arg-type]


class TestRotationSkipsInvalidCandidatesWithoutHttp:
    def test_invalid_candidates_cost_no_request_and_do_not_starve_the_valid_ones(self, db_session, platform):
        _add(db_session, platform, [LEGACY, "C202607030002", _num(1), _num(2), _num(3)])
        rec = _Recorder()
        conn = _connector(db_session, platform, rec)
        result = ClaimSyncService(db_session).sync_cancellations_by_candidate_orders(conn, platform.id, 2, _limits())
        assert rec.orders == [_num(1), _num(2)]  # 무효 2건을 건너뛰고도 이번 실행의 유효 후보 2건을 채운다
        assert result["status"] == "SUCCESS" and result["checked"] == 2 and result["skipped_invalid_order_no"] == 2

    def test_the_cursor_moves_past_invalid_candidates(self, db_session, platform):
        rows = _add(db_session, platform, [LEGACY, "C202607030002", _num(1)])
        rec = _Recorder()
        conn = _connector(db_session, platform, rec)
        service = ClaimSyncService(db_session)
        service.sync_cancellations_by_candidate_orders(conn, platform.id, 2, _limits())
        assert rec.orders == [_num(1)] and _cursor(db_session, platform) == rows[2].id

    def test_all_invalid_is_not_reported_as_an_external_success_and_differs_from_no_candidates(
        self, db_session, platform
    ):
        rows = _add(db_session, platform, [LEGACY, "C202607030002"])
        rec = _Recorder()
        conn = _connector(db_session, platform, rec)
        service = ClaimSyncService(db_session)
        all_invalid = service.sync_cancellations_by_candidate_orders(conn, platform.id, 5, _limits())
        assert rec.orders == []
        assert all_invalid["status"] == "FAILED" and all_invalid["reason_code"] == "INVALID_PLATFORM_ORDER_NO"
        assert all_invalid["skipped_invalid_order_no"] == 2 and all_invalid["checked"] == 0
        assert _cursor(db_session, platform) == rows[1].id  # 다음 실행이 같은 무효 후보에 갇히지 않는다

        db_session.query(Order).delete()
        db_session.flush()
        none_at_all = service.sync_cancellations_by_candidate_orders(conn, platform.id, 5, _limits())
        assert (
            none_at_all["status"] == "SUCCESS"
            and none_at_all["skipped_invalid_order_no"] == 0
            and none_at_all["checked"] == 0
        )

    def test_the_rotation_still_wraps_around_after_the_end(self, db_session, platform):
        _add(db_session, platform, [LEGACY, _num(1), _num(2), _num(3)])
        service = ClaimSyncService(db_session)
        seen: list[str] = []
        for _ in range(3):
            rec = _Recorder()
            conn = _connector(db_session, platform, rec)
            service.sync_cancellations_by_candidate_orders(conn, platform.id, 2, _limits())
            seen += rec.orders
        assert seen == [_num(1), _num(2), _num(3), _num(1), _num(2), _num(3)]  # 한 바퀴 돌고 다시 처음부터

    def test_logs_and_results_never_contain_the_invalid_order_numbers(self, db_session, platform, caplog):
        _add(db_session, platform, [LEGACY, _num(1)])
        conn = _connector(db_session, platform, _empty_ok)
        with caplog.at_level(logging.DEBUG):
            result = ClaimSyncService(db_session).sync_cancellations_by_candidate_orders(
                conn, platform.id, 5, _limits()
            )
        text = " ".join(r.getMessage() for r in caplog.records) + repr(result)
        assert (LEGACY in text) is False and ("C2026" in text) is False
        assert "reason=INVALID_PLATFORM_ORDER_NO" in text and "count=1" in text


class TestHttpErrorPolicy:
    def test_a_400_stops_the_batch_after_one_request_and_the_cursor_stays_before_the_failed_order(
        self, db_session, platform
    ):
        _add(db_session, platform, [_num(i) for i in range(1, 6)])
        rec = _Recorder(lambda r: httpx.Response(400, json={}))
        conn = _connector(db_session, platform, rec)
        result = ClaimSyncService(db_session).sync_cancellations_by_candidate_orders(
            conn, platform.id, 5, _limits(retries=2)
        )
        assert rec.orders == [_num(1)]  # 다음 후보를 호출하지 않고 retry 도 없다
        assert (
            result["status"] == "FAILED"
            and result["reason_code"] == "BAD_RESPONSE"
            and result["last_http_status"] == 400
        )
        assert _cursor(db_session, platform) == 0  # 실패한 후보를 지나가지 않는다 - 다음 실행이 같은 주문부터 다시 시도

    @pytest.mark.parametrize("code", [404, 405, 422])
    def test_other_unconfirmed_4xx_are_fail_closed_too(self, db_session, platform, code):
        _add(db_session, platform, [_num(1), _num(2)])
        rec = _Recorder(lambda r: httpx.Response(code, json={}))
        conn = _connector(db_session, platform, rec)
        result = ClaimSyncService(db_session).sync_cancellations_by_candidate_orders(conn, platform.id, 2, _limits())
        assert len(rec.orders) == 1 and result["status"] == "FAILED" and result["last_http_status"] == code
        assert _cursor(db_session, platform) == 0

    def test_412_is_retried_only_within_the_configured_retries_then_the_batch_stops(self, db_session, platform):
        _add(db_session, platform, [_num(1), _num(2)])
        rec = _Recorder(lambda r: httpx.Response(412, json={}))
        conn = _connector(db_session, platform, rec)
        result = ClaimSyncService(db_session).sync_cancellations_by_candidate_orders(
            conn, platform.id, 2, _limits(retries=2)
        )
        assert len(rec.orders) == 3  # 최초 1 + retry 2 (모두 같은 주문)
        assert set(rec.orders) == {_num(1)}
        assert result["status"] == "FAILED" and result["reason_code"] == "TIMEOUT" and result["retryable"] is True
        assert result["last_http_status"] == 412 and _cursor(db_session, platform) == 0

    def test_412_with_zero_retries_sends_exactly_one_request_and_leaves_the_cursor_alone(self, db_session, platform):
        _add(db_session, platform, [_num(1), _num(2)])
        rec = _Recorder(lambda r: httpx.Response(412, json={}))
        conn = _connector(db_session, platform, rec)
        limits = _limits(retries=0, budget=2)
        result = ClaimSyncService(db_session).sync_cancellations_by_candidate_orders(conn, platform.id, 2, limits)
        assert len(rec.orders) == 1 and limits.budget.used == 1
        assert result["reason_code"] == "TIMEOUT" and _cursor(db_session, platform) == 0

    def test_a_412_that_recovers_on_retry_counts_as_checked(self, db_session, platform):
        _add(db_session, platform, [_num(1)])
        seen = {"n": 0}

        def respond(request):
            seen["n"] += 1
            return (
                httpx.Response(412, json={}) if seen["n"] == 1 else httpx.Response(200, json={"code": 200, "data": []})
            )

        rec = _Recorder(respond)
        conn = _connector(db_session, platform, rec)
        result = ClaimSyncService(db_session).sync_cancellations_by_candidate_orders(
            conn, platform.id, 1, _limits(retries=2)
        )
        assert len(rec.orders) == 2 and result["status"] == "SUCCESS" and result["checked"] == 1

    @pytest.mark.parametrize(
        "code, reason",
        [
            (401, "AUTH_FAILED"),
            (403, "AUTH_FAILED"),
            (429, "RATE_LIMITED"),
            (500, "SERVER_ERROR"),
            (503, "SERVER_ERROR"),
        ],
    )
    def test_existing_classifications_are_unchanged(self, db_session, platform, code, reason):
        _add(db_session, platform, [_num(1), _num(2)])
        rec = _Recorder(lambda r: httpx.Response(code, json={}))
        conn = _connector(db_session, platform, rec)
        result = ClaimSyncService(db_session).sync_cancellations_by_candidate_orders(conn, platform.id, 2, _limits())
        assert result["reason_code"] == reason and result["last_http_status"] == code and len(rec.orders) == 1
        assert _cursor(db_session, platform) == 0

    def test_a_transient_failure_is_retried_first_on_the_next_run(self, db_session, platform):
        _add(db_session, platform, [_num(1), _num(2), _num(3)])
        failing = _Recorder(lambda r: httpx.Response(412, json={}))
        service = ClaimSyncService(db_session)
        service.sync_cancellations_by_candidate_orders(
            _connector(db_session, platform, failing), platform.id, 2, _limits()
        )
        healthy = _Recorder()
        service.sync_cancellations_by_candidate_orders(
            _connector(db_session, platform, healthy), platform.id, 2, _limits()
        )
        assert healthy.orders == [_num(1), _num(2)]

    def test_nothing_sensitive_is_logged_on_a_400(self, db_session, platform, caplog):
        _add(db_session, platform, [_num(1)])
        rec = _Recorder(lambda r: httpx.Response(400, json={"message": "leaky-body-marker"}))
        conn = _connector(db_session, platform, rec)
        with caplog.at_level(logging.DEBUG):
            ClaimSyncService(db_session).sync_cancellations_by_candidate_orders(conn, platform.id, 1, _limits())
        text = " ".join(r.getMessage() for r in caplog.records)
        assert ("leaky-body-marker" in text) is False and (_num(1) in text) is False and ("A00099001" in text) is False
        assert "reason=BAD_RESPONSE, http_status=400" in text


class TestResultStatusContract:
    def test_nothing_checked_but_a_lookup_failed_is_never_success(self, db_session, platform):
        _add(db_session, platform, [_num(1), _num(2)])
        rec = _Recorder(
            lambda r: httpx.Response(200, json={"code": 200, "data": [], "nextToken": "more"})
        )  # 빈 페이지 + nextToken = 이상 응답
        conn = _connector(db_session, platform, rec)
        result = ClaimSyncService(db_session).sync_cancellations_by_candidate_orders(conn, platform.id, 2, _limits())
        assert result["checked"] == 0 and result["skipped_failed"] == 2
        assert result["status"] == "FAILED" and result["reason_code"] == "PAGINATION_ANOMALY"

    def test_some_checked_and_some_failed_is_partial(self, db_session, platform):
        _add(db_session, platform, [_num(1), _num(2)])
        seen = {"n": 0}

        def respond(request):
            seen["n"] += 1
            return (
                httpx.Response(200, json={"code": 200, "data": [], "nextToken": "more"})
                if seen["n"] == 1
                else _empty_ok(request)
            )

        conn = _connector(db_session, platform, _Recorder(respond))
        result = ClaimSyncService(db_session).sync_cancellations_by_candidate_orders(conn, platform.id, 2, _limits())
        assert result["status"] == "PARTIAL" and result["checked"] == 1 and result["skipped_failed"] == 1

    def test_verification_levels_follow_the_status_contract(self):
        from api.routers.orders import _cancellation_verification_level as level

        assert level({"status": "SUCCESS", "count": 0, "checked": 2}) == "LIVE_CANCEL_AUTH_AND_ENVELOPE_VALIDATED"
        assert level({"status": "SUCCESS", "count": 1, "checked": 2}) == "LIVE_CANCEL_PERSISTENCE_VALIDATED"
        for bad in (
            {"status": "FAILED", "checked": 0},
            {"status": "PARTIAL", "count": 3, "checked": 2},
            {"status": "SUCCESS", "count": 0, "checked": 0},
        ):
            assert level(bad) == "NOT_VALIDATED"


class TestVerificationOnlySelection:
    def test_recent_valid_candidates_newest_first_at_most_two_and_invalid_excluded(self, db_session, platform):
        # 가장 최근 2건은 무효(레거시) 번호 - 제외되고 그 다음으로 최근인 유효 주문이 선택된다
        _add(db_session, platform, [_num(1), _num(2), _num(3), LEGACY, "C202607030002"])
        rec = _Recorder()
        conn = _connector(db_session, platform, rec)
        result = ClaimSyncService(db_session).verify_cancellations_recent_valid(conn, platform.id, 2, _limits(budget=2))
        assert rec.orders == [_num(3), _num(2)]
        assert (result["candidate_selection"], result["candidate_count"], result["cursor_affected"]) == (
            "RECENT_VALID",
            2,
            False,
        )
        assert result["skipped_invalid_order_no"] == 2 and result["status"] == "SUCCESS"

    def test_ties_on_the_order_time_are_broken_by_larger_id_first(self, db_session, platform):
        rows = _add(db_session, platform, [_num(1), _num(2), _num(3)], same_time=True)
        rec = _Recorder()
        conn = _connector(db_session, platform, rec)
        ClaimSyncService(db_session).verify_cancellations_recent_valid(conn, platform.id, 2, _limits(budget=2))
        assert rec.orders == [rows[2].platform_order_no, rows[1].platform_order_no]

    def test_the_limit_is_two_and_the_budget_caps_the_requests(self, db_session, platform):
        _add(db_session, platform, [_num(i) for i in range(1, 8)])
        rec = _Recorder()
        conn = _connector(db_session, platform, rec)
        ClaimSyncService(db_session).verify_cancellations_recent_valid(conn, platform.id, 2, _limits(budget=2))
        assert len(rec.orders) == 2

    def test_the_production_cursor_is_not_read_created_or_changed(self, db_session, platform):
        rows = _add(db_session, platform, [_num(i) for i in range(1, 6)])
        before = _cursor(db_session, platform)
        conn = _connector(db_session, platform, _Recorder())
        ClaimSyncService(db_session).verify_cancellations_recent_valid(conn, platform.id, 2, _limits(budget=2))
        assert before is None and _cursor(db_session, platform) is None

        db_session.add(
            ClaimCollectionCursor(
                platform_id=platform.id,
                claim_type="CANCELLATION_ORDER_LOOKUP",
                last_order_id=rows[2].id,
                updated_at=BASE_TIME,
            )
        )
        db_session.flush()
        ClaimSyncService(db_session).verify_cancellations_recent_valid(conn, platform.id, 2, _limits(budget=2))
        assert _cursor(db_session, platform) == rows[2].id

    def test_the_rotation_after_a_verification_is_unaffected(self, db_session, platform):
        _add(db_session, platform, [_num(i) for i in range(1, 6)])
        service = ClaimSyncService(db_session)
        service.verify_cancellations_recent_valid(
            _connector(db_session, platform, _Recorder()), platform.id, 2, _limits(budget=2)
        )
        rec = _Recorder()
        service.sync_cancellations_by_candidate_orders(_connector(db_session, platform, rec), platform.id, 2, _limits())
        assert rec.orders == [_num(1), _num(2)]  # 회전은 여전히 맨 앞(가장 오래된 후보)부터 시작한다

    def test_only_cancellation_candidate_statuses_are_considered(self, db_session, platform):
        _add(db_session, platform, [_num(1)], status="DELIVERED")
        _add(db_session, platform, [_num(2)], status="NEW")
        rec = _Recorder()
        conn = _connector(db_session, platform, rec)
        ClaimSyncService(db_session).verify_cancellations_recent_valid(conn, platform.id, 2, _limits(budget=2))
        assert rec.orders == [_num(2)]

    def test_no_candidates_and_all_invalid_are_distinguishable(self, db_session, platform):
        conn = _connector(db_session, platform, _Recorder())
        service = ClaimSyncService(db_session)
        empty = service.verify_cancellations_recent_valid(conn, platform.id, 2, _limits(budget=2))
        assert empty["status"] == "SUCCESS" and empty["candidate_count"] == 0 and empty["skipped_invalid_order_no"] == 0
        _add(db_session, platform, [LEGACY])
        invalid_only = service.verify_cancellations_recent_valid(conn, platform.id, 2, _limits(budget=2))
        assert invalid_only["status"] == "FAILED" and invalid_only["reason_code"] == "INVALID_PLATFORM_ORDER_NO"
        assert invalid_only["candidate_count"] == 0 and invalid_only["skipped_invalid_order_no"] == 1
