"""
tests/integration/test_claim_sync_lock_and_verify.py
--------------------------------------------------------
수동 POST /api/orders/sync-claims의 (1) scheduler claim_sync와 공유하는 플랫폼 잠금(겹치면 ALREADY_RUNNING, 외부 호출·DB
쓰기 없음)과 (2) 실계정 첫 검증 프리셋(live_verify)의 서버 측 강제 범위를 검증한다. 스텁 커넥터만 쓴다.
"""

from sqlalchemy import create_engine

from config.settings import settings
from services.claim_sync_lock import claim_sync_platform_lock


class _Recorder:
    """어떤 기능이 어떤 제한으로 호출됐는지 기록한다. 취소 기능도 "지원"으로 표시해 제외가 코드로 강제됨을 확인한다."""

    supports_cancellation_sync = True
    supports_return_sync = True
    supports_exchange_sync = True
    supports_cancellation_lookup_by_order = True

    def __init__(self):
        self.calls: list[str] = []
        self.kwargs: dict[str, dict] = {}

    def estimate_claim_requests(self, feature, start, end, max_pages, max_retries):
        return None

    def fetch_cancellations(self, s, e):
        self.calls.append("cancellations")
        return []

    def fetch_returns(self, s, e, **kw):
        self.calls.append("returns")
        self.kwargs["returns"] = kw
        for _ in range(4):  # 반품 상태 4종 x 1페이지
            kw["request_budget"].consume("coupang")
        return []

    def fetch_exchanges(self, s, e, **kw):
        self.calls.append("exchanges")
        self.kwargs["exchanges"] = kw
        kw["request_budget"].consume("coupang")
        return []

    def fetch_cancellation_status(self, *a, **k):
        raise AssertionError("수동 sync-claims는 취소 단건 조회를 하지 않는다.")


def _post(client, auth_headers, seed_data, start="2026-10-07", end="2026-10-08", **extra):
    return client.post(
        "/api/orders/sync-claims",
        json={
            "platform_id": seed_data["platform_id"],
            "warehouse_id": seed_data["warehouse_id"],
            "start_date": start,
            "end_date": end,
            **extra,
        },
        headers=auth_headers,
    )


def _enable(monkeypatch, connector):
    monkeypatch.setattr(settings, "claim_sync_enabled", True)
    monkeypatch.setattr("api.routers.orders.get_mall_connector", lambda *a, **k: connector)


class TestSharedPlatformLock:
    def test_manual_sync_ends_with_already_running_when_the_job_holds_the_platform_lock(
        self, client, auth_headers, seed_data, monkeypatch
    ):
        monkeypatch.setattr(settings, "claim_sync_enabled", True)

        def _forbidden(*a, **k):
            raise AssertionError("잠금이 잡혀 있는데 커넥터가 만들어졌습니다.")

        monkeypatch.setattr("api.routers.orders.get_mall_connector", _forbidden)
        with claim_sync_platform_lock(seed_data["platform_id"], create_engine("sqlite://")) as held:
            assert held is True  # 이 블록이 scheduler job이 쥔 잠금을 흉내낸다
            resp = _post(client, auth_headers, seed_data)

        assert resp.status_code == 200
        body = resp.json()
        assert body["overall_status"] == "ALREADY_RUNNING"
        assert body["returns"]["reason_code"] == body["exchanges"]["reason_code"] == "ALREADY_RUNNING"

    def test_after_the_lock_is_released_the_manual_sync_runs(self, client, auth_headers, seed_data, monkeypatch):
        conn = _Recorder()
        _enable(monkeypatch, conn)
        engine = create_engine("sqlite://")
        with claim_sync_platform_lock(seed_data["platform_id"], engine):
            pass
        resp = _post(client, auth_headers, seed_data)
        assert resp.status_code == 200 and resp.json()["overall_status"] == "SUCCESS"
        assert "returns" in conn.calls
        # 요청이 끝나면 잠금이 풀려 다음 호출(또는 job)이 바로 잡을 수 있다.
        with claim_sync_platform_lock(seed_data["platform_id"], engine) as again:
            assert again is True

    def test_another_platform_is_not_blocked(self, client, auth_headers, seed_data, monkeypatch):
        conn = _Recorder()
        _enable(monkeypatch, conn)
        with claim_sync_platform_lock(seed_data["platform_id"] + 1000, create_engine("sqlite://")):
            resp = _post(client, auth_headers, seed_data)
        assert resp.json()["overall_status"] == "SUCCESS"


class TestLiveVerifyPresetIsEnforcedByTheServer:
    def test_only_returns_and_exchanges_with_one_page_no_retry_and_five_requests(
        self, client, auth_headers, seed_data, monkeypatch
    ):
        conn = _Recorder()
        _enable(monkeypatch, conn)
        monkeypatch.setattr(settings, "claim_sync_max_requests_per_run", 105)
        monkeypatch.setattr(settings, "claim_sync_max_pages_per_query", 3)
        monkeypatch.setattr(settings, "claim_sync_max_retries_per_page", 2)

        resp = _post(client, auth_headers, seed_data, live_verify=True)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert conn.calls == ["returns", "exchanges"]  # 취소는 지원 표시여도 제외
        assert body["cancellations"]["reason_code"] == "NOT_REQUESTED"
        for feature in ("returns", "exchanges"):
            assert conn.kwargs[feature]["max_pages"] == 1
            assert conn.kwargs[feature]["max_retries"] == 0
            assert conn.kwargs[feature]["request_budget"].max_requests == 5
        assert body["requests_used"] == 5

    def test_the_window_must_be_exactly_one_day(self, client, auth_headers, seed_data, monkeypatch):
        conn = _Recorder()
        _enable(monkeypatch, conn)
        for start, end in (("2026-10-06", "2026-10-08"), ("2026-10-08", "2026-10-08"), ("2026-10-01", "2026-10-31")):
            resp = _post(client, auth_headers, seed_data, start=start, end=end, live_verify=True)
            assert resp.status_code == 422, (start, end)
        assert conn.calls == []  # 거부된 요청은 어떤 외부 호출도 만들지 않았다

    def test_the_preset_never_exceeds_a_smaller_configured_budget(self, client, auth_headers, seed_data, monkeypatch):
        conn = _Recorder()
        _enable(monkeypatch, conn)
        monkeypatch.setattr(settings, "claim_sync_max_requests_per_run", 3)
        _post(client, auth_headers, seed_data, live_verify=True)
        assert conn.kwargs["returns"]["request_budget"].max_requests == 3

    def test_without_the_preset_the_configured_limits_apply_and_cancellations_still_never_look_up(
        self, client, auth_headers, seed_data, monkeypatch
    ):
        conn = _Recorder()
        _enable(monkeypatch, conn)
        monkeypatch.setattr(settings, "claim_sync_max_pages_per_query", 2)
        monkeypatch.setattr(settings, "claim_sync_max_retries_per_page", 1)
        monkeypatch.setattr(settings, "claim_sync_max_requests_per_run", 77)
        resp = _post(client, auth_headers, seed_data, start="2026-10-05", end="2026-10-08")
        assert resp.status_code == 200
        assert conn.kwargs["returns"]["max_pages"] == 2 and conn.kwargs["returns"]["max_retries"] == 1
        assert conn.kwargs["returns"]["request_budget"].max_requests == 77
        assert "cancellation_lookup" not in conn.calls  # fetch_cancellation_status는 호출되면 AssertionError
