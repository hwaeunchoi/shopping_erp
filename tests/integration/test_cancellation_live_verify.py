"""
tests/integration/test_cancellation_live_verify.py
--------------------------------------------------------
POST /api/orders/sync-claims 의 취소 단건 조회 전용 실계정 검증 프리셋(live_verify_cancellations)을 검증한다.
서버가 후보 최대 2건 / 페이지 1 / retry 0 / 요청 최대 2회로 고정하고 반품·교환·정산은 호출하지 않으며, scheduler와 같은
(platform_id, CLAIM_SYNC) 잠금을 쓰고, cursor 는 데이터와 같은 트랜잭션에서만 움직인다. 스텁 커넥터와 MockTransport 만
쓰므로 실제 외부 호출은 없다. 설정은 전부 monkeypatch 로 명시해 실제 .env 에 의존하지 않는다.
"""

import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from config.settings import settings
from integrations.malls.coupang_connector import COUPANG_API_BASE, CoupangConnector
from integrations.malls.errors import MarketplaceExternalAPIError
from models.order import Cancellation, ClaimCollectionCursor, Order
from services.claim_sync_lock import claim_sync_platform_lock
from services.settings_service import ApiCredentialService

VENDOR = "A00077001"
BASE_TIME = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
ORDER_NO_PREFIX = "88"  # 합성 주문번호(로그에 나오면 안 된다)


class _Stub:
    """취소 단건 조회만 지원하는 커넥터. 다른 모든 조회가 불리면 AssertionError - 호출 자체를 증명한다."""

    supports_cancellation_sync = True  # 일부러 True: 기간 대량조회가 호출되면 안 된다는 것을 증명
    supports_return_sync = True
    supports_exchange_sync = True
    supports_cancellation_lookup_by_order = True

    def __init__(self, script=None):
        self.calls: list[str] = []
        self.kwargs: list[dict] = []
        self.script = script or {}

    def estimate_claim_requests(self, feature, start, end, max_pages, max_retries):
        return 1 + max_retries if feature == "cancellation_lookup" else None

    def fetch_cancellations(self, *a, **k):
        raise AssertionError("기간 대량 취소 조회가 호출됐다")

    def fetch_returns(self, *a, **k):
        raise AssertionError("반품 조회가 호출됐다")

    def fetch_exchanges(self, *a, **k):
        raise AssertionError("교환 조회가 호출됐다")

    def fetch_cancellation_status(self, order_no, since, **kw):
        self.calls.append(order_no)
        self.kwargs.append(kw)
        kw["request_budget"].consume("coupang")
        outcome = self.script.get(order_no)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _seed_orders(session_factory, platform_id, n, start=1):
    db = session_factory()
    try:
        for i in range(start, start + n):
            db.add(
                Order(
                    platform_id=platform_id,
                    platform_order_no=f"{ORDER_NO_PREFIX}{i:06d}",
                    status="NEW",
                    order_date=BASE_TIME + timedelta(minutes=i),  # 번호가 클수록 더 최근 주문
                    total_amount=10000,
                    discount_amount=0,
                )
            )
        db.commit()
    finally:
        db.close()


def _no(i):
    return f"{ORDER_NO_PREFIX}{i:06d}"


def _post(client, auth_headers, seed_data, **extra):
    return client.post(
        "/api/orders/sync-claims",
        json={
            "platform_id": seed_data["platform_id"],
            "warehouse_id": seed_data["warehouse_id"],
            "start_date": "2026-10-12",
            "end_date": "2026-10-13",
            "live_verify_cancellations": True,
            **extra,
        },
        headers=auth_headers,
    )


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "claim_sync_enabled", True)
    monkeypatch.setattr(settings, "claim_sync_max_requests_per_run", 105)
    monkeypatch.setattr(settings, "claim_sync_max_retries_per_page", 2)
    monkeypatch.setattr(settings, "claim_sync_max_pages_per_query", 3)
    monkeypatch.setattr(settings, "claim_cancel_detail_max_orders_per_run", 20)


def _use(monkeypatch, connector):
    monkeypatch.setattr("api.routers.orders.get_mall_connector", lambda *a, **k: connector)


def _cursor(session_factory, platform_id):
    db = session_factory()
    try:
        return db.execute(
            select(ClaimCollectionCursor.last_order_id).where(ClaimCollectionCursor.platform_id == platform_id)
        ).scalar_one_or_none()
    finally:
        db.close()


def _ids(session_factory, platform_id):
    db = session_factory()
    try:
        return list(db.execute(select(Order.id).where(Order.platform_id == platform_id).order_by(Order.id)).scalars())
    finally:
        db.close()


class TestPresetContract:
    def test_both_presets_true_is_422_before_any_work(self, client, auth_headers, seed_data, monkeypatch):
        def _forbidden(*a, **k):
            raise AssertionError("커넥터가 만들어졌다")

        monkeypatch.setattr("api.routers.orders.get_mall_connector", _forbidden)
        resp = _post(client, auth_headers, seed_data, live_verify=True)
        assert resp.status_code == 422

    def test_flag_off_keeps_the_existing_disabled_policy(self, client, auth_headers, seed_data, monkeypatch):
        monkeypatch.setattr(settings, "claim_sync_enabled", False)
        monkeypatch.setattr("api.routers.orders.get_mall_connector", lambda *a, **k: pytest.fail("connector built"))
        resp = _post(client, auth_headers, seed_data)
        assert resp.status_code == 501
        assert resp.json()["cancellations"]["reason_code"] == "FEATURE_DISABLED"

    def test_at_most_two_candidates_one_request_each_no_retry_budget_two(
        self, client, auth_headers, seed_data, api_session_factory, monkeypatch
    ):
        _seed_orders(api_session_factory, seed_data["platform_id"], 10)
        stub = _Stub()
        _use(monkeypatch, stub)

        resp = _post(client, auth_headers, seed_data)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert stub.calls == [_no(10), _no(9)]  # 최근 주문 시각 내림차순 2건
        assert all(kw["max_retries"] == 0 and kw["request_budget"].max_requests == 2 for kw in stub.kwargs)
        assert body["requests_used"] == 2 and body["candidate_limit"] == 2
        assert body["returns"]["reason_code"] == body["exchanges"]["reason_code"] == "NOT_REQUESTED"
        assert body["cancellations"]["checked"] == 2
        meta = body["cancellations"]
        assert (meta["candidate_selection"], meta["candidate_count"], meta["cursor_affected"]) == (
            "RECENT_VALID",
            2,
            False,
        )

    def test_request_body_cannot_loosen_the_preset(
        self, client, auth_headers, seed_data, api_session_factory, monkeypatch
    ):
        _seed_orders(api_session_factory, seed_data["platform_id"], 10)
        stub = _Stub()
        _use(monkeypatch, stub)
        resp = _post(
            client,
            auth_headers,
            seed_data,
            candidate_limit=50,
            max_orders=50,
            max_requests=999,
            max_pages=9,
            max_retries=9,
            start_date="2026-01-01",
            end_date="2026-10-13",
        )
        assert resp.status_code == 200
        assert len(stub.calls) == 2
        assert all(kw["max_retries"] == 0 and kw["request_budget"].max_requests == 2 for kw in stub.kwargs)

    def test_a_smaller_configured_budget_wins(self, client, auth_headers, seed_data, api_session_factory, monkeypatch):
        monkeypatch.setattr(settings, "claim_sync_max_requests_per_run", 1)
        _seed_orders(api_session_factory, seed_data["platform_id"], 5)
        stub = _Stub()
        _use(monkeypatch, stub)
        resp = _post(client, auth_headers, seed_data)
        assert resp.status_code == 200
        assert len(stub.calls) == 1 and resp.json()["requests_used"] == 1

    def test_zero_candidates_means_zero_requests_and_is_not_overclaimed(
        self, client, auth_headers, seed_data, monkeypatch
    ):
        stub = _Stub()
        _use(monkeypatch, stub)
        resp = _post(client, auth_headers, seed_data)
        body = resp.json()
        assert resp.status_code == 200 and body["overall_status"] == "SUCCESS"
        assert stub.calls == [] and body["requests_used"] == 0
        assert body["verification_level"] == "NOT_VALIDATED"


class TestSharedLock:
    def test_a_running_job_makes_the_preset_end_with_already_running(
        self, client, auth_headers, seed_data, monkeypatch
    ):
        monkeypatch.setattr("api.routers.orders.get_mall_connector", lambda *a, **k: pytest.fail("connector built"))
        with claim_sync_platform_lock(seed_data["platform_id"], create_engine("sqlite://")) as held:
            assert held is True
            resp = _post(client, auth_headers, seed_data)
        assert resp.status_code == 200
        assert resp.json()["overall_status"] == "ALREADY_RUNNING"

    def test_another_platform_is_not_blocked(self, client, auth_headers, seed_data, monkeypatch):
        stub = _Stub()
        _use(monkeypatch, stub)
        with claim_sync_platform_lock(seed_data["platform_id"] + 1000, create_engine("sqlite://")):
            resp = _post(client, auth_headers, seed_data)
        assert resp.json()["overall_status"] == "SUCCESS"


class TestVerificationNeverTouchesTheProductionCursor:
    def test_no_cursor_is_created_and_repeated_runs_pick_the_same_recent_orders(
        self, client, auth_headers, seed_data, api_session_factory, monkeypatch
    ):
        _seed_orders(api_session_factory, seed_data["platform_id"], 6)
        stub = _Stub()
        _use(monkeypatch, stub)
        _post(client, auth_headers, seed_data)
        _post(client, auth_headers, seed_data)
        assert stub.calls == [_no(6), _no(5), _no(6), _no(5)]  # 반복해도 같은 최근 후보 - 순회 위치가 오염되지 않는다
        assert _cursor(api_session_factory, seed_data["platform_id"]) is None  # cursor 행도 만들지 않는다

    def test_an_existing_production_cursor_is_left_exactly_as_it_was(
        self, client, auth_headers, seed_data, api_session_factory, monkeypatch
    ):
        _seed_orders(api_session_factory, seed_data["platform_id"], 6)
        ids = _ids(api_session_factory, seed_data["platform_id"])
        db = api_session_factory()
        stamp = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        db.add(
            ClaimCollectionCursor(
                platform_id=seed_data["platform_id"],
                claim_type="CANCELLATION_ORDER_LOOKUP",
                last_order_id=ids[3],
                updated_at=stamp,
            )
        )
        db.commit()
        db.close()
        _use(monkeypatch, _Stub())
        _post(client, auth_headers, seed_data)
        db = api_session_factory()
        try:
            row = db.execute(select(ClaimCollectionCursor)).scalar_one()
            assert row.last_order_id == ids[3]
            assert row.updated_at.replace(tzinfo=None) == stamp.replace(tzinfo=None)
        finally:
            db.close()

    def test_commit_failure_returns_500_and_rolls_the_request_transaction_back(
        self, client, auth_headers, seed_data, api_session_factory, monkeypatch
    ):
        """API 계층의 동작만 확인한다(500 DB_WRITE_FAILED + rollback 호출). 데이터가 실제로 롤백되는지는 SQLite(pysqlite 의 SAVEPOINT 자동 commit
        동작) 가 아니라 격리 PostgreSQL 에서 증명한다 - test_claim_sync_pg.py."""
        _seed_orders(api_session_factory, seed_data["platform_id"], 4)
        _use(monkeypatch, _Stub())
        real_commit, real_rollback = Session.commit, Session.rollback
        state = {"fail": True, "rollbacks": 0}

        def flaky(self):
            if state["fail"]:
                state["fail"] = False
                raise SQLAlchemyError("synthetic commit failure")
            return real_commit(self)

        def counting_rollback(self):
            state["rollbacks"] += 1
            return real_rollback(self)

        monkeypatch.setattr(Session, "commit", flaky)
        monkeypatch.setattr(Session, "rollback", counting_rollback)
        first = _post(client, auth_headers, seed_data)
        assert first.status_code == 500 and first.json()["cancellations"]["reason_code"] == "DB_WRITE_FAILED"
        assert first.json()["verification_level"] == "NOT_VALIDATED"
        assert state["rollbacks"] >= 1

    def test_a_transient_error_stops_the_batch_and_is_retried_on_the_next_verification(
        self, client, auth_headers, seed_data, api_session_factory, monkeypatch
    ):
        _seed_orders(api_session_factory, seed_data["platform_id"], 4)
        err = MarketplaceExternalAPIError("coupang", "RATE_LIMITED", True, 429)
        stub = _Stub(script={_no(4): err})
        _use(monkeypatch, stub)
        resp = _post(client, auth_headers, seed_data)
        assert stub.calls == [_no(4)]  # 공통 원인(429)이라 배치 중단
        assert resp.json()["cancellations"]["reason_code"] == "RATE_LIMITED"
        assert resp.json()["verification_level"] == "NOT_VALIDATED"
        stub.script.clear()
        _post(client, auth_headers, seed_data)
        assert stub.calls[1:] == [_no(4), _no(3)]

    def test_a_400_stops_the_batch_without_calling_the_next_candidate(
        self, client, auth_headers, seed_data, api_session_factory, monkeypatch
    ):
        _seed_orders(api_session_factory, seed_data["platform_id"], 4)
        stub = _Stub(script={_no(4): MarketplaceExternalAPIError("coupang", "BAD_RESPONSE", False, 400)})
        _use(monkeypatch, stub)
        resp = _post(client, auth_headers, seed_data)
        body = resp.json()
        assert stub.calls == [_no(4)]
        assert body["overall_status"] == "FAILED" and body["cancellations"]["last_http_status"] == 400
        assert body["verification_level"] == "NOT_VALIDATED"

    def test_a_per_order_permanent_error_is_partial_never_validated(
        self, client, auth_headers, seed_data, api_session_factory, monkeypatch
    ):
        _seed_orders(api_session_factory, seed_data["platform_id"], 4)
        stub = _Stub(script={_no(4): MarketplaceExternalAPIError("coupang", "PAGE_LIMIT_EXCEEDED", False)})
        _use(monkeypatch, stub)
        body = _post(client, auth_headers, seed_data).json()
        assert stub.calls == [_no(4), _no(3)]
        assert body["overall_status"] == "PARTIAL" and body["verification_level"] == "NOT_VALIDATED"
        assert body["cancellations"]["checked"] == 1 and body["cancellations"]["skipped_failed"] == 1


def _connector(session_factory, platform_id, handler):
    db = session_factory()
    svc = ApiCredentialService(db)
    svc.upsert_credential("PLATFORM", platform_id, "access_key", "synthetic-access-key")
    svc.upsert_credential("PLATFORM", platform_id, "secret_key", "synthetic-secret-key")
    svc.upsert_credential("PLATFORM", platform_id, "vendor_id", VENDOR)
    db.commit()
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
    return CoupangConnector(session=db, platform_id=platform_id, http_client=client), db


def _cancel_item(order_no, receipt_id=700001):
    return {
        "receiptId": receipt_id,
        "orderId": int(order_no),
        "receiptType": "CANCEL",
        "receiptStatus": "RETURNS_UNCHECKED",
        "createdAt": "2026-10-12T10:00:00+09:00",
        "returnItems": [{"vendorItemId": 1, "cancelCount": 1}],
    }


class TestRealConnectorOverMockTransport:
    def test_no_cancellation_is_a_normal_envelope_and_is_not_called_persistence_validated(
        self, client, auth_headers, seed_data, api_session_factory, monkeypatch
    ):
        _seed_orders(api_session_factory, seed_data["platform_id"], 5)
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"code": 200, "message": "OK", "data": []})

        conn, cdb = _connector(api_session_factory, seed_data["platform_id"], handler)
        _use(monkeypatch, conn)
        body = _post(client, auth_headers, seed_data).json()
        cdb.close()

        assert len(seen) == 2  # 후보 2건 x 1회
        assert all(r.url.params["cancelType"] == "CANCEL" and "status" not in r.url.params for r in seen)
        assert all("nextToken" not in r.url.params for r in seen)  # 다음 페이지 요청 없음
        assert body["overall_status"] == "SUCCESS" and body["cancellations"]["count"] == 0
        assert body["verification_level"] == "LIVE_CANCEL_AUTH_AND_ENVELOPE_VALIDATED"

    def test_a_next_page_is_never_requested(self, client, auth_headers, seed_data, api_session_factory, monkeypatch):
        _seed_orders(api_session_factory, seed_data["platform_id"], 5)
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"code": 200, "message": "OK", "data": [], "nextToken": "more"})

        conn, cdb = _connector(api_session_factory, seed_data["platform_id"], handler)
        _use(monkeypatch, conn)
        body = _post(client, auth_headers, seed_data).json()
        cdb.close()
        assert len(seen) == 2 and all("nextToken" not in r.url.params for r in seen)
        assert body["cancellations"]["skipped_failed"] == 2
        assert body["verification_level"] == "NOT_VALIDATED"

    def test_429_is_not_retried(self, client, auth_headers, seed_data, api_session_factory, monkeypatch):
        _seed_orders(api_session_factory, seed_data["platform_id"], 5)
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(429, json={})

        conn, cdb = _connector(api_session_factory, seed_data["platform_id"], handler)
        _use(monkeypatch, conn)
        resp = _post(client, auth_headers, seed_data)
        cdb.close()
        assert len(seen) == 1  # retry 0 + 공통 원인이라 배치 중단
        assert resp.status_code == 503 and resp.json()["cancellations"]["reason_code"] == "RATE_LIMITED"

    def test_a_cancellation_is_persisted_deduplicated_and_marks_persistence_validated(
        self, client, auth_headers, seed_data, api_session_factory, monkeypatch
    ):
        _seed_orders(api_session_factory, seed_data["platform_id"], 1)

        def handler(request):
            order_no = request.url.params["orderId"]
            return httpx.Response(200, json={"code": 200, "message": "OK", "data": [_cancel_item(order_no)]})

        conn, cdb = _connector(api_session_factory, seed_data["platform_id"], handler)
        _use(monkeypatch, conn)
        first = _post(client, auth_headers, seed_data).json()
        second = _post(
            client, auth_headers, seed_data
        ).json()  # 후보 1건이라 되감겨 같은 주문을 다시 확인 - 테스트에서만 반복
        cdb.close()

        assert (
            first["verification_level"] == "LIVE_CANCEL_PERSISTENCE_VALIDATED" and first["cancellations"]["count"] == 1
        )
        db = api_session_factory()
        try:
            assert db.execute(select(func.count()).select_from(Cancellation)).scalar_one() == 1  # 중복 없음
        finally:
            db.close()
        assert second["overall_status"] == "SUCCESS"

    def test_logs_carry_no_order_number_vendor_id_credential_or_response_body(
        self, client, auth_headers, seed_data, api_session_factory, monkeypatch, caplog
    ):
        _seed_orders(api_session_factory, seed_data["platform_id"], 3)

        def handler(request):
            return httpx.Response(200, json={"code": 200, "message": f"{VENDOR} secret-body-marker", "data": []})

        conn, cdb = _connector(api_session_factory, seed_data["platform_id"], handler)
        _use(monkeypatch, conn)
        with caplog.at_level(logging.DEBUG):
            _post(client, auth_headers, seed_data)
        cdb.close()
        text = "\n".join(r.getMessage() for r in caplog.records)
        for forbidden in (
            VENDOR,
            "synthetic-access-key",
            "synthetic-secret-key",
            "secret-body-marker",
            ORDER_NO_PREFIX + "0000",
            "/vendors/",
            "orderId=",
        ):
            assert forbidden not in text, forbidden
