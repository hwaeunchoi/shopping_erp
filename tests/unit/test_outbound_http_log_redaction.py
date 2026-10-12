"""
tests/unit/test_outbound_http_log_redaction.py
---------------------------------------------------
httpx/httpcore는 INFO에 요청 전체 URL(쇼핑몰 vendorId, 경로 식별자, 조회 조건)을 남긴다. 운영 로그에 판매자 식별자가
평문으로 쌓이지 않도록 중앙 logging 설정이 해당 logger를 WARNING 이상으로 막는지, 그리고 정상·429·5xx·인증 오류·연결 오류의
모든 경로에서 식별자가 로그와 예외 문자열에 남지 않는지 MockTransport(실제 외부 호출 없음)로 검증한다.
"""

import logging
from datetime import date, timedelta

import httpx
import pytest

from config import logging_config
from config.settings import settings
from integrations.malls import coupang_connector
from integrations.malls.coupang_connector import COUPANG_API_BASE, CoupangConnector
from integrations.malls.errors import MarketplaceExternalAPIError, RequestBudget
from services.settings_service import ApiCredentialService

VENDOR_ID = "A00099887"
ACCESS_KEY = "synthetic-access-key-0001"
SECRET_KEY = "synthetic-secret-key-0002"
ORDER_MARK = "770001122"
TODAY = date(2026, 10, 8)
START = TODAY - timedelta(days=3)
SENSITIVE = (VENDOR_ID, ACCESS_KEY, SECRET_KEY, ORDER_MARK)


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        text = record.getMessage()
        if record.exc_info:
            text += " " + logging.Formatter().formatException(record.exc_info)
        self.lines.append(f"{record.name}: {text}")


@pytest.fixture
def captured(tmp_path, monkeypatch):
    """setup_logging()을 실제로 호출하고, 그 뒤 root로 올라오는 모든 레코드를 모은다. 끝나면 logging 상태를 복원한다."""
    root = logging.getLogger()
    names = ("httpx", "httpcore", "urllib3")
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_levels = {n: logging.getLogger(n).level for n in names}
    monkeypatch.setattr(settings, "logs_dir", tmp_path)
    cap = _Capture()
    try:
        yield cap, root
    finally:
        for h in list(root.handlers):
            if h not in saved_handlers:
                root.removeHandler(h)
                h.close()
        root.setLevel(saved_level)
        for n, lvl in saved_levels.items():
            logging.getLogger(n).setLevel(lvl)


def _connector(db_session, platform, handler) -> CoupangConnector:
    svc = ApiCredentialService(db_session)
    svc.upsert_credential("PLATFORM", platform.id, "access_key", ACCESS_KEY)
    svc.upsert_credential("PLATFORM", platform.id, "secret_key", SECRET_KEY)
    svc.upsert_credential("PLATFORM", platform.id, "vendor_id", VENDOR_ID)
    db_session.flush()
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
    return CoupangConnector(session=db_session, platform_id=platform.id, http_client=client)


def _leaks(lines: list[str]) -> list[str]:
    return [ln for ln in lines if any(s in ln for s in SENSITIVE) or "/vendors/" in ln or "vendorId=" in ln]


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    monkeypatch.setattr(coupang_connector.time_module, "sleep", lambda seconds: None)


class TestCentralLoggingConfig:
    @pytest.mark.parametrize("debug", [False, True])
    def test_http_client_loggers_are_raised_to_warning_in_every_environment(self, captured, monkeypatch, debug):
        _, _ = captured
        monkeypatch.setattr(settings, "debug", debug)
        for n in ("httpx", "httpcore", "urllib3"):
            logging.getLogger(n).setLevel(logging.NOTSET)  # 라이브러리 기본(루트 상속)으로 되돌린 상태에서 시작

        logging_config.setup_logging()

        for n in ("httpx", "httpcore", "urllib3"):
            assert logging.getLogger(n).getEffectiveLevel() >= logging.WARNING, (n, debug)
        assert logging.getLogger().level == (logging.DEBUG if debug else logging.INFO)  # 앱 자체 로그 수준은 그대로

    def test_a_plain_request_url_never_reaches_any_handler(self, captured, monkeypatch):
        cap, root = captured
        monkeypatch.setattr(settings, "debug", True)  # DEBUG가 가장 많이 남기는 경우
        logging_config.setup_logging()
        root.addHandler(cap)

        url = f"https://api-gateway.example.test/v2/providers/vendors/{VENDOR_ID}/ordersheets?vendorId={VENDOR_ID}&orderId={ORDER_MARK}"
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))) as client:
            client.get(url, headers={"Authorization": f"CEA algorithm=HmacSHA256, access-key={ACCESS_KEY}"})

        assert _leaks(cap.lines) == [], cap.lines
        assert not any(ln.startswith(("httpx", "httpcore")) for ln in cap.lines)

    def test_application_warnings_still_appear(self, captured):
        cap, root = captured
        logging_config.setup_logging()
        root.addHandler(cap)
        logging.getLogger("integrations.malls.coupang_connector").warning("알 수 없는 쿠팡 상태 - REVIEW로 보존")
        assert any("REVIEW로 보존" in ln for ln in cap.lines)

    def test_httpx_warnings_and_errors_are_not_silenced(self, captured):
        cap, root = captured
        logging_config.setup_logging()
        root.addHandler(cap)
        logging.getLogger("httpx").warning("synthetic warning without identifiers")
        assert any("synthetic warning" in ln for ln in cap.lines)


class TestEveryConnectorPathLeavesNoIdentifier:
    """실제 CoupangConnector가 정상/재시도/오류/연결 실패를 겪는 동안 로그와 예외 문자열을 모두 검사한다."""

    @pytest.fixture
    def wired(self, captured):
        cap, root = captured
        logging_config.setup_logging()
        root.addHandler(cap)
        return cap

    def test_success_and_pagination(self, wired, db_session, platform):
        def handler(request):
            return httpx.Response(200, json={"code": 200, "data": [], "nextToken": ""})

        conn = _connector(db_session, platform, handler)
        conn.fetch_returns(START, TODAY, max_pages=3, max_retries=2, request_budget=RequestBudget(max_requests=50))
        conn.fetch_exchanges(START, TODAY, max_pages=3, max_retries=2, request_budget=RequestBudget(max_requests=50))
        assert _leaks(wired.lines) == [], wired.lines

    def test_429_retry_and_5xx(self, wired, db_session, platform):
        seen = {"n": 0}

        def handler(request):
            seen["n"] += 1
            return httpx.Response(429 if seen["n"] <= 2 else 500, json={"message": f"{VENDOR_ID} {ORDER_MARK}"})

        conn = _connector(db_session, platform, handler)
        with pytest.raises(MarketplaceExternalAPIError) as info:
            conn.fetch_returns(START, TODAY, max_pages=1, max_retries=2, request_budget=RequestBudget(max_requests=50))
        assert _leaks(wired.lines) == [], wired.lines
        assert _leaks([str(info.value), repr(info.value)]) == []

    @pytest.mark.parametrize("status", [401, 403, 404])
    def test_auth_and_client_errors(self, wired, db_session, platform, status):
        conn = _connector(
            db_session,
            platform,
            lambda request: httpx.Response(status, json={"message": f"{VENDOR_ID} {ACCESS_KEY} {ORDER_MARK}"}),
        )
        with pytest.raises(MarketplaceExternalAPIError) as info:
            conn.fetch_exchanges(
                START, TODAY, max_pages=1, max_retries=0, request_budget=RequestBudget(max_requests=50)
            )
        assert _leaks(wired.lines) == [], wired.lines
        assert _leaks([str(info.value), repr(info.value)]) == []

    def test_connection_error_whose_own_message_contains_the_url(self, wired, db_session, platform):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

        conn = _connector(db_session, platform, handler)
        with pytest.raises(Exception) as info:  # noqa: B017 - 어떤 예외 종류든 문자열에 URL이 없어야 한다
            conn.fetch_returns(START, TODAY, max_pages=1, max_retries=1, request_budget=RequestBudget(max_requests=50))
        assert calls["n"] >= 1 and not isinstance(info.value, TypeError)  # 실제로 전송을 시도한 경로여야 한다
        assert _leaks(wired.lines) == [], wired.lines
        assert _leaks([str(info.value), repr(info.value)]) == [], (type(info.value).__name__,)

    def test_timeout_and_cancellation_lookup_by_order(self, wired, db_session, platform):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            raise httpx.ReadTimeout(f"timeout {request.url}", request=request)

        conn = _connector(db_session, platform, handler)
        with pytest.raises(Exception) as info:  # noqa: B017
            conn.fetch_cancellation_status(
                ORDER_MARK, START, max_retries=0, request_budget=RequestBudget(max_requests=5)
            )
        assert calls["n"] >= 1 and not isinstance(info.value, TypeError)  # 호출 형태 오류로 우연히 통과하지 않는다
        assert _leaks(wired.lines) == [], wired.lines
        assert _leaks([str(info.value), repr(info.value)]) == [], (type(info.value).__name__,)
