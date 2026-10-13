"""
tests/unit/test_cancellation_response_diagnostics.py
-------------------------------------------------------
쿠팡 취소 "후보 주문 단건 조회"가 실패했을 때 운영자가 원인을 구분할 수 있도록 숫자 HTTP 상태코드만 안전하게 남기는지 검증한다.
실제 CoupangConnector 를 MockTransport 로 돌리고(외부 호출 없음), 로그 레코드의 message·args·예외 문자열 전체에 합성 vendor ID·주문번호·
키·서명·URL·query·응답 본문·오류 문구·JWT 가 없는지 검사한다. 실패 출력에 URL·주문번호가 나오지 않도록 단언은 불리언으로만 한다.
"""

import logging
import re
from datetime import datetime, timezone

import httpx
import pytest

from config import logging_config
from config.settings import settings
from integrations.malls import coupang_connector
from integrations.malls.coupang_connector import COUPANG_API_BASE, CoupangConnector
from integrations.malls.errors import RequestBudget
from models.order import ClaimCollectionCursor, Order
from services.claim_sync_service import ClaimSyncLimits, ClaimSyncService
from services.settings_service import ApiCredentialService

VENDOR = "A00088123"
ACCESS = "synthetic-access-diag-1"
SECRET = "synthetic-secret-diag-2"
BODY_MARK = "diag-body-marker-3"
ERROR_MARK = "diag-error-message-4"
ORDER_BASE = 28000000000100
LOOKUP_LOG = re.compile(
    r"^취소 후보 주문 조회 실패: platform_id=\d+, reason=[A-Z_]+, http_status=(\d{3}|none), batch_stop=(True|False)$"
)
JWT_LIKE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
FORBIDDEN_TEXT = (
    VENDOR,
    ACCESS,
    SECRET,
    BODY_MARK,
    ERROR_MARK,
    "Authorization",
    "signature",
    "CEA algorithm",
    "api-gateway",
    "/vendors/",
    "createdAt",
    "orderId=",
    "cancelType",
    "returnRequests",
)


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    monkeypatch.setattr(coupang_connector.time_module, "sleep", lambda seconds: None)


class _Cap(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def texts(self) -> list[str]:
        out = []
        for r in self.records:
            text = f"{r.name}|{r.getMessage()}|{r.args!r}"
            if r.exc_info:
                text += "|" + logging.Formatter().formatException(r.exc_info)
            out.append(text)
        return out


@pytest.fixture
def logs(tmp_path, monkeypatch):
    """setup_logging() 을 debug 모드로 실제 호출하고(가장 많이 남기는 경우) 모든 레코드를 모은다. 끝나면 logging 상태를 복원한다."""
    root = logging.getLogger()
    names = ("httpx", "httpcore", "urllib3")
    saved_handlers, saved_level = list(root.handlers), root.level
    saved_levels = {n: logging.getLogger(n).level for n in names}
    monkeypatch.setattr(settings, "logs_dir", tmp_path)
    monkeypatch.setattr(settings, "debug", True)
    logging_config.setup_logging()
    cap = _Cap()
    root.addHandler(cap)
    try:
        yield cap
    finally:
        for h in list(root.handlers):
            if h not in saved_handlers:
                root.removeHandler(h)
                h.close()
        root.setLevel(saved_level)
        for n, lvl in saved_levels.items():
            logging.getLogger(n).setLevel(lvl)


def _orders(db_session, platform, numbers):
    for no in numbers:
        db_session.add(
            Order(
                platform_id=platform.id,
                platform_order_no=no,
                status="NEW",
                order_date=datetime.now(timezone.utc),
                total_amount=1000,
                discount_amount=0,
            )
        )
    db_session.flush()


def _numeric(n):
    return [str(ORDER_BASE + i) for i in range(1, n + 1)]


def _connector(db_session, platform, handler):
    svc = ApiCredentialService(db_session)
    svc.upsert_credential("PLATFORM", platform.id, "access_key", ACCESS)
    svc.upsert_credential("PLATFORM", platform.id, "secret_key", SECRET)
    svc.upsert_credential("PLATFORM", platform.id, "vendor_id", VENDOR)
    db_session.flush()
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
    return CoupangConnector(session=db_session, platform_id=platform.id, http_client=client)


def _status_response(code):
    return httpx.Response(
        code, json={"code": code, "message": f"{ERROR_MARK} {VENDOR} {ORDER_BASE + 1} {BODY_MARK}", "data": []}
    )


def _run(db_session, platform, handler, n=2, batch=2, budget=105):
    conn = _connector(db_session, platform, handler)
    limits = ClaimSyncLimits(max_pages=1, max_retries=0, budget=RequestBudget(max_requests=budget))
    return ClaimSyncService(db_session).sync_cancellations_by_candidate_orders(conn, platform.id, batch, limits)


def _lookup_lines(cap):
    return [r.getMessage() for r in cap.records if r.getMessage().startswith("취소 후보 주문 조회 실패")]


class TestNumericHttpStatusIsReported:
    @pytest.mark.parametrize(
        "code, reason, batch_stop",
        [
            (400, "BAD_RESPONSE", False),
            (404, "BAD_RESPONSE", False),
            (412, "BAD_RESPONSE", False),
            (401, "AUTH_FAILED", True),
            (403, "AUTH_FAILED", True),
            (429, "RATE_LIMITED", True),
            (500, "SERVER_ERROR", True),
            (503, "SERVER_ERROR", True),
        ],
    )
    def test_status_and_the_existing_reason_code_are_both_present(
        self, db_session, platform, logs, code, reason, batch_stop
    ):
        _orders(db_session, platform, _numeric(3))
        result = _run(db_session, platform, lambda request: _status_response(code))

        assert result["last_http_status"] == code
        line_ok = [LOOKUP_LOG.match(ln) is not None for ln in _lookup_lines(logs)]
        assert line_ok and all(line_ok), "lookup warning must match the strict safe format"
        first = _lookup_lines(logs)[0]
        assert f"reason={reason}," in first and f"http_status={code}," in first and f"batch_stop={batch_stop}" in first
        if batch_stop:
            assert result["status"] == "FAILED" and result["reason_code"] == reason
        else:
            assert result["status"] == "SUCCESS" and result["skipped_failed"] == 2 and result["checked"] == 0

    def test_400_and_412_are_recorded_as_different_values(self, db_session, platform, logs):
        _orders(db_session, platform, _numeric(3))
        seen = {"n": 0}

        def handler(request):
            seen["n"] += 1
            return _status_response(400 if seen["n"] == 1 else 412)

        result = _run(db_session, platform, handler)
        lines = _lookup_lines(logs)
        assert "http_status=400," in lines[0] and "http_status=412," in lines[1]
        assert result["last_http_status"] == 412

    @pytest.mark.parametrize("exc", [httpx.ReadTimeout("t"), httpx.ConnectError("c")])
    def test_timeout_and_connection_errors_have_no_http_status(self, db_session, platform, logs, exc):
        _orders(db_session, platform, _numeric(2))

        def handler(request):
            raise type(exc)(str(exc), request=request)

        result = _run(db_session, platform, handler)
        assert "last_http_status" not in result
        assert result["reason_code"] in ("TIMEOUT", "CONNECT_FAILED")
        assert "http_status=none," in _lookup_lines(logs)[0]

    def test_a_successful_lookup_has_no_error_status_field(self, db_session, platform, logs):
        _orders(db_session, platform, _numeric(2))
        result = _run(
            db_session, platform, lambda request: httpx.Response(200, json={"code": 200, "message": "OK", "data": []})
        )
        assert result["status"] == "SUCCESS" and result["checked"] == 2
        assert "last_http_status" not in result and _lookup_lines(logs) == []


class TestNothingSensitiveIsLogged:
    @pytest.mark.parametrize("code", [400, 412, 401, 429, 503])
    def test_status_responses_leave_no_identifier_url_body_or_secret(self, db_session, platform, logs, code):
        _orders(db_session, platform, _numeric(3))
        _run(db_session, platform, lambda request: _status_response(code))
        self._assert_clean(logs)

    @pytest.mark.parametrize("exc", [httpx.ReadTimeout, httpx.ConnectError])
    def test_transport_errors_whose_message_contains_the_url_leave_nothing(self, db_session, platform, logs, exc):
        _orders(db_session, platform, _numeric(2))

        def handler(request):
            raise exc(f"failed {request.url}", request=request)

        _run(db_session, platform, handler)
        self._assert_clean(logs)

    def test_an_unparseable_body_leaves_nothing(self, db_session, platform, logs):
        _orders(db_session, platform, _numeric(2))
        _run(db_session, platform, lambda request: httpx.Response(200, text=f"<html>{BODY_MARK} {VENDOR}</html>"))
        self._assert_clean(logs)

    @staticmethod
    def _assert_clean(cap):
        texts = cap.texts()
        assert texts, "the run must have produced log records"
        leaked_text = [t for t in texts if any(f in t for f in FORBIDDEN_TEXT)]
        leaked_orders = [t for t in texts if re.search(r"\d{14}", t)]
        leaked_jwt = [t for t in texts if JWT_LIKE.search(t)]
        httpx_records = [t for t in texts if t.startswith(("httpx|", "httpcore|", "urllib3|"))]
        assert not leaked_text, "a log record contains a forbidden identifier/URL/secret/body"
        assert not leaked_orders, "a log record contains an order number"
        assert not leaked_jwt, "a log record contains a JWT-like string"
        assert not httpx_records, "the HTTP client logged a request"
        assert all(
            LOOKUP_LOG.match(r.getMessage())
            for r in cap.records
            if r.getMessage().startswith("취소 후보 주문 조회 실패")
        )


class TestRequestContractOfTheLookup:
    """실제 connector 가 만든 요청의 구조만 단언한다(값은 출력하지 않는다) - 현재 요청 형식을 고정하는 특성 테스트."""

    def test_structure_of_the_request(self, db_session, platform, logs):
        _orders(db_session, platform, _numeric(3))
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"code": 200, "message": "OK", "data": [], "nextToken": ""})

        _run(db_session, platform, handler)
        assert len(seen) == 2
        path_ok = all(
            re.fullmatch(r"/v2/providers/openapi/apis/api/v6/vendors/[^/]+/returnRequests", r.url.path) for r in seen
        )
        names_ok = all(
            set(r.url.params.keys()) == {"createdAtFrom", "createdAtTo", "cancelType", "orderId"} for r in seen
        )
        dates_ok = all(
            re.fullmatch(r"\d{4}-\d{2}-\d{2}", r.url.params["createdAtFrom"])
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", r.url.params["createdAtTo"])
            for r in seen
        )
        order_ok = all(r.url.params["orderId"] in _numeric(3) for r in seen)
        assert path_ok, "endpoint path shape"
        assert names_ok, "parameter name set (no searchType, no status, no maxPerPage)"
        assert dates_ok, "date strings are yyyy-MM-dd"
        assert all(r.url.params["cancelType"] == "CANCEL" for r in seen), "cancelType"
        assert order_ok, "orderId is used"

    def test_one_page_no_retry_and_the_budget_caps_the_requests(self, db_session, platform, logs):
        _orders(db_session, platform, _numeric(6))
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"code": 200, "message": "OK", "data": [], "nextToken": "more"})

        result = _run(db_session, platform, handler, n=6, batch=6, budget=2)
        assert len(seen) == 2, "budget 2 allows at most two requests"
        assert not any("nextToken" in r.url.params for r in seen), "a next page is never requested"
        assert result["skipped_failed"] == 2

    def test_a_429_is_not_retried(self, db_session, platform, logs):
        _orders(db_session, platform, _numeric(3))
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(429, json={})

        _run(db_session, platform, handler)
        assert len(seen) == 1


class TestCurrentCursorPolicyIsCharacterised:
    """BAD_RESPONSE 의 cursor 정책은 별도 검토 대상이다 - 이 테스트들은 "현재 동작"을 고정해 정책을 바꿀 때 눈에 띄게 한다."""

    def _cursor(self, db_session, platform):
        return db_session.query(ClaimCollectionCursor.last_order_id).filter_by(platform_id=platform.id).scalar()

    def test_three_consecutive_400s_stop_the_batch_and_the_cursor_passes_all_three(self, db_session, platform, logs):
        _orders(db_session, platform, _numeric(6))
        ids = [o.id for o in db_session.query(Order).filter_by(platform_id=platform.id).order_by(Order.id)]
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return _status_response(400)

        result = _run(db_session, platform, handler, n=6, batch=6)
        assert len(seen) == 3 and result["status"] == "FAILED" and result["reason_code"] == "BAD_RESPONSE"
        assert result["last_http_status"] == 400
        assert self._cursor(db_session, platform) == ids[2]

    def test_skipped_candidates_are_retried_on_the_next_rotation(self, db_session, platform, logs):
        _orders(db_session, platform, _numeric(2))
        seen: list[str] = []

        def handler(request):
            seen.append(request.url.params["orderId"])
            return _status_response(400)

        _run(db_session, platform, handler)
        _run(db_session, platform, handler)  # 후보 2건뿐이라 cursor 가 되감겨 같은 주문을 다시 시도한다
        assert seen == _numeric(2) + _numeric(2)

    def test_known_gap_legacy_dummy_style_order_numbers_are_still_candidates_and_sent_as_orderId(
        self, db_session, platform, logs
    ):
        """알려진 한계(수정 아님): 더미 차단(a20fd80) 이전에 만들어진 "C"+날짜+순번 형태의 비숫자 주문번호가 후보에 남아 있고, 공식 문서가
        숫자(number)로 규정한 orderId 로 그대로 전송된다. 후보 선택 정책을 바꾸면 이 테스트를 함께 갱신한다."""
        legacy = "C202607030001"
        _orders(db_session, platform, [legacy] + _numeric(1))
        seen: list[str] = []

        def handler(request):
            seen.append(request.url.params["orderId"])
            return httpx.Response(200, json={"code": 200, "message": "OK", "data": []})

        _run(db_session, platform, handler)
        assert (legacy in seen) is True, "the non-numeric legacy order number is currently sent as orderId"
        assert (not legacy.isdigit()) is True
