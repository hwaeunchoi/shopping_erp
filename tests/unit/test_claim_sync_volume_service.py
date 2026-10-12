"""
tests/unit/test_claim_sync_volume_service.py
------------------------------------------------
ClaimSyncService의 호출량 안전장치: 사전 예산 검사(네트워크 전 차단), 기능별 실패 격리(부분 저장 없음),
취소 단건 조회의 실행당 상한·공유 예산·cursor 순환·개별 오류 건너뜀(기아 방지)·공통 오류 중단,
로그에 주문번호 등이 남지 않음. 스텁 커넥터만 쓰며 외부 호출은 없다.
"""

import logging
from datetime import date, datetime, timedelta, timezone

from integrations.malls.errors import MarketplaceExternalAPIError, RequestBudget
from models.order import Exchange, Order, Return
from services.claim_sync_service import ClaimSyncLimits, ClaimSyncService, build_claim_sync_limits

TODAY = date(2026, 10, 8)
START = TODAY - timedelta(days=3)


def _limits(max_requests=105, pages=3, retries=2) -> ClaimSyncLimits:
    return ClaimSyncLimits(max_pages=pages, max_retries=retries, budget=RequestBudget(max_requests=max_requests))


def _order(db_session, platform, order_no, status="NEW"):
    order = Order(
        platform_id=platform.id,
        platform_order_no=order_no,
        status=status,
        order_date=datetime.now(timezone.utc),
        total_amount=10000,
        discount_amount=0,
    )
    db_session.add(order)
    db_session.flush()
    return order


def _claim(order_no, claim_id, **kw):
    base = {"platform_order_no": order_no, "platform_claim_id": claim_id, "reason": "사유", "status": "REQUESTED"}
    base.update(kw)
    return base


class _ListConnector:
    """반품·교환 목록 조회 스텁 - 호출 시 지정한 만큼 예산을 소비(실제 커넥터의 요청 소비를 흉내)하고 estimate를 제공한다."""

    supports_cancellation_sync = False
    supports_return_sync = True
    supports_exchange_sync = True

    def __init__(self, *, returns=None, exchanges=None, return_error=None, exchange_error=None, spend=(0, 0)):
        self._returns = returns or []
        self._exchanges = exchanges or []
        self._return_error = return_error
        self._exchange_error = exchange_error
        self._spend = spend
        self.calls: list[str] = []
        self.kwargs: dict[str, dict] = {}

    def estimate_claim_requests(self, feature, start, end, max_pages, max_retries):
        days = max((end - start).days, 0)
        attempts = 1 + max_retries
        if feature == "returns":
            return 4 * -(-days // 31) * max_pages * attempts
        if feature == "exchanges":
            return -(-days // 7) * max_pages * attempts
        return None

    def fetch_returns(self, s, e, **kw):
        self.calls.append("returns")
        self.kwargs["returns"] = kw
        for _ in range(self._spend[0]):
            kw["request_budget"].consume("coupang")
        if self._return_error:
            raise self._return_error
        return self._returns

    def fetch_exchanges(self, s, e, **kw):
        self.calls.append("exchanges")
        self.kwargs["exchanges"] = kw
        for _ in range(self._spend[1]):
            kw["request_budget"].consume("coupang")
        if self._exchange_error:
            raise self._exchange_error
        return self._exchanges


class TestPreCheckBlocksBeforeAnyRequest:
    def test_default_limits_come_from_settings(self, monkeypatch):
        from config.settings import settings

        monkeypatch.setattr(settings, "claim_sync_max_pages_per_query", 2)
        monkeypatch.setattr(settings, "claim_sync_max_retries_per_page", 1)
        monkeypatch.setattr(settings, "claim_sync_max_requests_per_run", 77)
        limits = build_claim_sync_limits()
        assert (limits.max_pages, limits.max_retries, limits.budget.max_requests) == (2, 1, 77)

    def test_limits_are_passed_to_both_fetches(self, db_session, platform):
        conn = _ListConnector()
        limits = _limits()
        ClaimSyncService(db_session).sync_claims(conn, platform.id, START, TODAY, limits)
        for feature in ("returns", "exchanges"):
            assert conn.kwargs[feature]["max_pages"] == 3
            assert conn.kwargs[feature]["max_retries"] == 2
            assert conn.kwargs[feature]["request_budget"] is limits.budget

    def test_budget_smaller_than_the_worst_case_fails_that_feature_without_any_fetch(self, db_session, platform):
        conn = _ListConnector()
        result = ClaimSyncService(db_session).sync_claims(conn, platform.id, START, TODAY, _limits(max_requests=10))
        assert result["returns"]["reason_code"] == "BUDGET_TOO_SMALL"  # 36 > 10
        assert "returns" not in conn.calls
        assert result["exchanges"]["status"] == "SUCCESS"  # 9 <= 10 이므로 교환은 진행

    def test_remaining_budget_too_small_after_returns_blocks_exchanges(self, db_session, platform):
        conn = _ListConnector(spend=(36, 0))
        result = ClaimSyncService(db_session).sync_claims(conn, platform.id, START, TODAY, _limits(max_requests=40))
        assert result["returns"]["status"] == "SUCCESS"
        assert result["exchanges"]["reason_code"] == "REQUEST_BUDGET_EXCEEDED"  # 남은 4 < 교환 최악 9
        assert result["exchanges"]["retryable"] is True
        assert "exchanges" not in conn.calls
        assert result["requests_used"] == 36

    def test_default_budget_fits_both_lists_and_leaves_room_for_the_cancellation_lookups(self, db_session, platform):
        conn = _ListConnector(spend=(36, 9))  # 목록의 절대 최악
        limits = _limits()
        result = ClaimSyncService(db_session).sync_claims(conn, platform.id, START, TODAY, limits)
        assert result["overall_status"] == "SUCCESS"
        assert limits.budget.max_requests - limits.budget.used == 60 == 20 * 3  # 취소 단건 20주문 x 3


class TestFeatureFailureIsIsolatedWithoutPartialWrites:
    def test_page_limit_on_returns_saves_no_return_and_still_saves_exchanges(self, db_session, platform):
        _order(db_session, platform, "ORD-1")
        conn = _ListConnector(
            return_error=MarketplaceExternalAPIError("coupang", "PAGE_LIMIT_EXCEEDED", False),
            exchanges=[_claim("ORD-1", "X-1")],
        )
        result = ClaimSyncService(db_session).sync_claims(conn, platform.id, START, TODAY, _limits())
        db_session.flush()
        assert result["returns"]["status"] == "FAILED" and result["returns"]["reason_code"] == "PAGE_LIMIT_EXCEEDED"
        assert result["exchanges"]["status"] == "SUCCESS"
        assert result["overall_status"] == "PARTIAL"
        assert db_session.query(Return).count() == 0
        assert db_session.query(Exchange).count() == 1

    def test_next_run_after_a_page_limit_failure_recovers_the_same_period(self, db_session, platform):
        _order(db_session, platform, "ORD-1")
        service = ClaimSyncService(db_session)
        failing = _ListConnector(return_error=MarketplaceExternalAPIError("coupang", "PAGE_LIMIT_EXCEEDED", False))
        service.sync_claims(failing, platform.id, START, TODAY, _limits())
        assert db_session.query(Return).count() == 0

        ok = _ListConnector(returns=[_claim("ORD-1", "R-1")])
        again = service.sync_claims(ok, platform.id, START, TODAY, _limits())
        db_session.flush()
        assert again["returns"]["status"] == "SUCCESS"
        assert db_session.query(Return).count() == 1

    def test_pagination_anomalies_are_reported_as_non_retryable_failures(self, db_session, platform):
        for reason in ("PAGINATION_LOOP", "PAGINATION_ANOMALY"):
            conn = _ListConnector(exchange_error=MarketplaceExternalAPIError("coupang", reason, False))
            result = ClaimSyncService(db_session).sync_claims(conn, platform.id, START, TODAY, _limits())
            assert result["exchanges"]["reason_code"] == reason and result["exchanges"]["retryable"] is False


class _LookupConnector:
    supports_cancellation_lookup_by_order = True

    def __init__(self, *, errors=None, results=None, spend=1):
        self._errors = errors or {}
        self._results = results or {}
        self._spend = spend
        self.calls: list[str] = []
        self.kwargs: list[dict] = []

    def estimate_claim_requests(self, feature, start, end, max_pages, max_retries):
        return (1 + max_retries) if feature == "cancellation_lookup" else None

    def fetch_cancellation_status(self, platform_order_no, since, **kw):
        self.calls.append(platform_order_no)
        self.kwargs.append(kw)
        for _ in range(self._spend):
            kw["request_budget"].consume("coupang")
        if platform_order_no in self._errors:
            raise self._errors[platform_order_no]
        return self._results.get(platform_order_no)


def _orders(db_session, platform, n, prefix="C"):
    return [_order(db_session, platform, f"{prefix}-{i:03d}") for i in range(1, n + 1)]


class TestCancellationLookupVolume:
    def test_at_most_max_orders_are_looked_up_per_run(self, db_session, platform):
        _orders(db_session, platform, 50)
        conn = _LookupConnector()
        result = ClaimSyncService(db_session).sync_cancellations_by_candidate_orders(conn, platform.id, 20, _limits())
        assert len(conn.calls) == 20 and result["checked"] == 20
        assert all(kw["max_retries"] == 2 for kw in conn.kwargs)

    def test_default_batch_comes_from_settings(self, db_session, platform, monkeypatch):
        from config.settings import settings

        monkeypatch.setattr(settings, "claim_cancel_detail_max_orders_per_run", 7)
        _orders(db_session, platform, 30)
        conn = _LookupConnector()
        ClaimSyncService(db_session).sync_cancellations_by_candidate_orders(conn, platform.id, limits=_limits())
        assert len(conn.calls) == 7

    def test_lookups_share_the_budget_and_stop_before_the_network_when_it_runs_out(self, db_session, platform):
        _orders(db_session, platform, 10)
        conn = _LookupConnector(spend=3)  # 주문마다 최악(최초+retry 2)을 쓴다
        limits = _limits(max_requests=7)  # 3 + 3 = 6 사용 후 남은 1 < 최악 3
        result = ClaimSyncService(db_session).sync_cancellations_by_candidate_orders(conn, platform.id, 10, limits)
        assert len(conn.calls) == 2 and result["checked"] == 2
        assert result["status"] == "SUCCESS"  # 예산 부족은 실패가 아니라 "다음 실행이 이어간다"
        assert limits.budget.used == 6

    def test_cursor_advances_so_every_candidate_is_checked_within_a_rotation(self, db_session, platform):
        _orders(db_session, platform, 45)
        service = ClaimSyncService(db_session)
        seen: list[str] = []
        for _ in range(3):  # 45 / 20 -> 3회 안에 한 바퀴
            conn = _LookupConnector()
            service.sync_cancellations_by_candidate_orders(conn, platform.id, 20, _limits())
            seen.extend(conn.calls)
        assert set(seen) == {f"C-{i:03d}" for i in range(1, 46)}

    def test_a_permanently_failing_order_is_skipped_and_never_stalls_the_rotation(self, db_session, platform):
        _orders(db_session, platform, 6)
        bad = {"C-002": MarketplaceExternalAPIError("coupang", "PAGE_LIMIT_EXCEEDED", False)}
        service = ClaimSyncService(db_session)

        first = _LookupConnector(errors=bad)
        r1 = service.sync_cancellations_by_candidate_orders(first, platform.id, 3, _limits())
        assert first.calls == ["C-001", "C-002", "C-003"]
        assert r1["status"] == "SUCCESS" and r1["skipped_failed"] == 1 and r1["checked"] == 2

        second = _LookupConnector(errors=bad)
        service.sync_cancellations_by_candidate_orders(second, platform.id, 3, _limits())
        assert second.calls == ["C-004", "C-005", "C-006"]  # 같은 자리에서 다시 멈추지 않고 이어간다

    def test_three_consecutive_skips_are_treated_as_a_common_problem(self, db_session, platform):
        _orders(db_session, platform, 8)
        bad = {f"C-00{i}": MarketplaceExternalAPIError("coupang", "BAD_RESPONSE", False) for i in range(1, 9)}
        conn = _LookupConnector(errors=bad)
        result = ClaimSyncService(db_session).sync_cancellations_by_candidate_orders(conn, platform.id, 8, _limits())
        assert len(conn.calls) == 3
        assert result["status"] == "FAILED" and result["reason_code"] == "BAD_RESPONSE"
        # cursor는 건너뛴 주문 뒤로 전진했다 - 다음 실행은 4번부터 이어간다.
        nxt = _LookupConnector()
        ClaimSyncService(db_session).sync_cancellations_by_candidate_orders(nxt, platform.id, 2, _limits())
        assert nxt.calls == ["C-004", "C-005"]

    def test_systemic_errors_stop_the_batch_immediately_and_retry_the_same_order_first(self, db_session, platform):
        _orders(db_session, platform, 5)
        for reason in ("RATE_LIMITED", "AUTH_FAILED", "SERVER_ERROR", "TIMEOUT", "REQUEST_BUDGET_EXCEEDED"):
            conn = _LookupConnector(errors={"C-003": MarketplaceExternalAPIError("coupang", reason, True)})
            service = ClaimSyncService(db_session)
            result = service.sync_cancellations_by_candidate_orders(conn, platform.id, 5, _limits())
            assert conn.calls == ["C-001", "C-002", "C-003"], reason
            assert result["status"] == "PARTIAL" and result["reason_code"] == reason
            retry = _LookupConnector()
            service.sync_cancellations_by_candidate_orders(retry, platform.id, 1, _limits())
            assert retry.calls == ["C-003"], reason  # 실패한 주문부터 다시
            # 다음 반복을 위해 cursor를 처음으로 되돌린다.
            from repositories.order_repository import ClaimCollectionCursorRepository
            from services.claim_sync_service import CANCELLATION_ORDER_LOOKUP_CURSOR_TYPE

            ClaimCollectionCursorRepository(db_session).get_or_create(
                platform.id, CANCELLATION_ORDER_LOOKUP_CURSOR_TYPE
            ).last_order_id = 0
            db_session.flush()

    def test_logs_never_contain_order_numbers_or_credentials(self, db_session, platform, caplog):
        _order(db_session, platform, "SENSITIVE-ORDER-12345")
        conn = _LookupConnector(
            errors={"SENSITIVE-ORDER-12345": MarketplaceExternalAPIError("coupang", "BAD_RESPONSE", False)}
        )
        with caplog.at_level(logging.DEBUG):
            ClaimSyncService(db_session).sync_cancellations_by_candidate_orders(conn, platform.id, 5, _limits())
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "BAD_RESPONSE" in text
        assert "SENSITIVE-ORDER-12345" not in text

    def test_zero_batch_size_does_nothing(self, db_session, platform):
        _orders(db_session, platform, 3)
        conn = _LookupConnector()
        result = ClaimSyncService(db_session).sync_cancellations_by_candidate_orders(conn, platform.id, 0, _limits())
        assert conn.calls == [] and result["status"] == "SUCCESS"


class TestFeatureSelection:
    def test_unrequested_features_are_not_called_and_are_labelled(self, db_session, platform):
        conn = _ListConnector()
        conn.supports_cancellation_sync = True
        conn.fetch_cancellations = lambda s, e: (_ for _ in ()).throw(AssertionError("취소는 요청되지 않았다"))  # type: ignore[attr-defined]
        result = ClaimSyncService(db_session).sync_claims(
            conn, platform.id, START, TODAY, _limits(), frozenset({"returns", "exchanges"})
        )
        assert result["cancellations"] == {
            "status": "UNSUPPORTED",
            "count": 0,
            "reason_code": "NOT_REQUESTED",
            "retryable": False,
        }
        assert conn.calls == ["returns", "exchanges"]

    def test_only_one_feature_can_be_requested(self, db_session, platform):
        conn = _ListConnector()
        result = ClaimSyncService(db_session).sync_claims(
            conn, platform.id, START, TODAY, _limits(), frozenset({"exchanges"})
        )
        assert conn.calls == ["exchanges"]
        assert result["returns"]["reason_code"] == "NOT_REQUESTED"


class TestUnmatchedPromotionIsolation:
    def _hold(self, db_session, platform, claim_ids):
        service = ClaimSyncService(db_session)
        conn = _ListConnector(returns=[_claim(f"ORD-{c}", c) for c in claim_ids])
        service.sync_claims(conn, platform.id, START, TODAY, _limits())
        for c in claim_ids:
            _order(db_session, platform, f"ORD-{c}")
        db_session.flush()
        return service

    def test_one_failing_promotion_does_not_stop_the_others_or_the_sync(self, db_session, platform, monkeypatch):
        from sqlalchemy.exc import IntegrityError

        from models.order import ClaimUnmatched

        service = self._hold(db_session, platform, ["P-1", "G-1"])
        original = ClaimSyncService._promote_unmatched

        def poisoned(self, pending, order_id):
            if pending.platform_claim_id == "P-1":
                raise IntegrityError("INSERT", {}, Exception("synthetic"))
            return original(self, pending, order_id)

        monkeypatch.setattr(ClaimSyncService, "_promote_unmatched", poisoned)
        result = service.sync_claims(_ListConnector(), platform.id, START, TODAY, _limits())

        assert result["resolved_unmatched"] == 1 and result["overall_status"] == "SUCCESS"
        states = {u.platform_claim_id: u.resolved_at is not None for u in db_session.query(ClaimUnmatched).all()}
        assert states == {"P-1": False, "G-1": True}

    def test_a_promotion_that_fails_after_creating_the_claim_leaves_neither_claim_nor_marker(
        self, db_session, platform, monkeypatch
    ):
        from sqlalchemy.exc import IntegrityError

        from models.order import ClaimUnmatched

        service = self._hold(db_session, platform, ["P-1"])
        original = ClaimSyncService._promote_unmatched

        def half_done(self, pending, order_id):
            original(self, pending, order_id)  # 클레임 행이 만들어진 뒤
            raise IntegrityError("UPDATE", {}, Exception("synthetic"))  # 해결 표시 전에 실패

        monkeypatch.setattr(ClaimSyncService, "_promote_unmatched", half_done)
        service.sync_claims(_ListConnector(), platform.id, START, TODAY, _limits())

        assert db_session.query(Return).count() == 0  # 반쪽 승격 없음
        assert all(u.resolved_at is None for u in db_session.query(ClaimUnmatched).all())

    def test_recovery_after_the_cause_disappears_promotes_once_without_duplicates(
        self, db_session, platform, monkeypatch
    ):
        service = self._hold(db_session, platform, ["P-1"])
        original = ClaimSyncService._promote_unmatched
        monkeypatch.setattr(
            ClaimSyncService, "_promote_unmatched", lambda self, p, o: (_ for _ in ()).throw(RuntimeError("x"))
        )
        service.sync_claims(_ListConnector(), platform.id, START, TODAY, _limits())
        monkeypatch.setattr(ClaimSyncService, "_promote_unmatched", original)
        service.sync_claims(_ListConnector(), platform.id, START, TODAY, _limits())
        service.sync_claims(_ListConnector(), platform.id, START, TODAY, _limits())
        assert db_session.query(Return).count() == 1

    def test_promotion_failure_logs_only_the_exception_class(self, db_session, platform, monkeypatch, caplog):
        service = self._hold(db_session, platform, ["SECRET-CLAIM-ID"])
        monkeypatch.setattr(
            ClaimSyncService,
            "_promote_unmatched",
            lambda self, p, o: (_ for _ in ()).throw(RuntimeError("ORD-SECRET-CLAIM-ID")),
        )
        with caplog.at_level(logging.DEBUG):
            service.sync_claims(_ListConnector(), platform.id, START, TODAY, _limits())
        text_ = " ".join(r.getMessage() for r in caplog.records)
        assert "type=RuntimeError" in text_
        assert "SECRET-CLAIM-ID" not in text_
