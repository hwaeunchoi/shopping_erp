"""
tests/unit/test_claim_sync_service.py
-------------------------------------------
ClaimSyncService: capability(취소/반품/교환) 인지 + 기능별 격리 미러링을 검증한다.

- capability False 기능은 fetch를 호출하지 않고 결과 UNSUPPORTED(0건과 구분).
- capability True 기능만 SAVEPOINT 안에서 처리하고, 한 기능의 실패(인증/외부/DB/예상밖)는
  그 기능만 FAILED로 격리하고 다음 기능은 계속한다(앞선 성공 데이터 유지).
- 중복방지(주문+유형), 매칭 주문 없으면 skipped_no_order.
실제 쇼핑몰 API는 호출하지 않는다(스텁만 사용).
"""

from datetime import date, datetime, timezone

from sqlalchemy.exc import SQLAlchemyError

from integrations.malls.errors import MarketplaceCredentialMissingError, MarketplaceExternalAPIError
from models.order import Cancellation, Exchange, Order, Return
from services.claim_sync_service import ClaimSyncService

SPAN = (date(2026, 1, 1), date(2026, 1, 31))
_ALL = ("cancellation", "return", "exchange")


class StubClaimConnector:
    """capability 플래그·기능별 데이터·기능별 예외를 제어할 수 있는 테스트 스텁."""

    def __init__(self, *, cancellations=None, returns=None, exchanges=None, supports=_ALL, errors=None):
        self._data = {"cancellations": cancellations or [], "returns": returns or [], "exchanges": exchanges or []}
        self._errors = errors or {}
        self.supports_cancellation_sync = "cancellation" in supports
        self.supports_return_sync = "return" in supports
        self.supports_exchange_sync = "exchange" in supports
        self.calls: list[str] = []

    def _feature(self, name):
        self.calls.append(name)
        if name in self._errors:
            raise self._errors[name]
        return self._data[name]

    def fetch_cancellations(self, s, e):
        return self._feature("cancellations")

    def fetch_returns(self, s, e):
        return self._feature("returns")

    def fetch_exchanges(self, s, e):
        return self._feature("exchanges")


def _make_order(db_session, platform, order_no="CLAIM-ORD-1"):
    order = Order(
        platform_id=platform.id,
        platform_order_no=order_no,
        status="DELIVERED",
        order_date=datetime.now(timezone.utc),
        total_amount=10000,
        discount_amount=0,
    )
    db_session.add(order)
    db_session.flush()
    return order


def _claim(order_no, **kw):
    base = {"platform_order_no": order_no, "reason": "사유", "status": "REQUESTED"}
    base.update(kw)
    return base


class TestCapabilityGating:
    def test_all_unsupported_does_not_call_fetch(self, db_session, platform):
        conn = StubClaimConnector(supports=())  # 세 기능 모두 미지원
        result = ClaimSyncService(db_session).sync_claims(conn, platform.id, *SPAN)

        assert conn.calls == []  # fetch 미호출
        assert result["overall_status"] == "UNSUPPORTED"
        for key in ("cancellations", "returns", "exchanges"):
            assert result[key]["status"] == "UNSUPPORTED"
            assert result[key]["count"] == 0
            assert result[key]["reason_code"] == "CAPABILITY_UNSUPPORTED"

    def test_only_cancellation_supported(self, db_session, platform):
        order = _make_order(db_session, platform)
        conn = StubClaimConnector(cancellations=[_claim(order.platform_order_no)], supports=("cancellation",))
        result = ClaimSyncService(db_session).sync_claims(conn, platform.id, *SPAN)

        assert conn.calls == ["cancellations"]  # 반품/교환 fetch 미호출
        assert result["cancellations"]["status"] == "SUCCESS"
        assert result["returns"]["status"] == "UNSUPPORTED"
        assert result["exchanges"]["status"] == "UNSUPPORTED"
        assert result["overall_status"] == "SUCCESS"  # SUCCESS + UNSUPPORTED = SUCCESS

    def test_only_return_supported(self, db_session, platform):
        order = _make_order(db_session, platform)
        conn = StubClaimConnector(returns=[_claim(order.platform_order_no)], supports=("return",))
        result = ClaimSyncService(db_session).sync_claims(conn, platform.id, *SPAN)
        assert conn.calls == ["returns"]
        assert result["returns"]["status"] == "SUCCESS"
        assert result["overall_status"] == "SUCCESS"

    def test_only_exchange_supported(self, db_session, platform):
        order = _make_order(db_session, platform)
        conn = StubClaimConnector(exchanges=[_claim(order.platform_order_no)], supports=("exchange",))
        result = ClaimSyncService(db_session).sync_claims(conn, platform.id, *SPAN)
        assert conn.calls == ["exchanges"]
        assert result["exchanges"]["status"] == "SUCCESS"
        assert result["overall_status"] == "SUCCESS"

    def test_supported_but_zero_results_is_success_not_unsupported(self, db_session, platform):
        conn = StubClaimConnector(supports=("cancellation",))  # 지원하지만 데이터 0건
        result = ClaimSyncService(db_session).sync_claims(conn, platform.id, *SPAN)
        assert conn.calls == ["cancellations"]  # 지원하므로 호출은 함
        assert result["cancellations"] == {"status": "SUCCESS", "count": 0, "reason_code": None, "retryable": None}
        assert result["overall_status"] == "SUCCESS"


class TestMirroringAndDedup:
    def test_creates_cancellation_return_exchange(self, db_session, platform):
        order = _make_order(db_session, platform)
        conn = StubClaimConnector(
            cancellations=[
                _claim(
                    order.platform_order_no,
                    reason="변심",
                    status="COMPLETED",
                    refund_amount=10000,
                    requested_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
                )
            ],
            returns=[_claim(order.platform_order_no, reason="불량", refund_amount=5000)],
            exchanges=[_claim(order.platform_order_no, reason="사이즈")],
        )
        result = ClaimSyncService(db_session).sync_claims(conn, platform.id, *SPAN)

        assert result["cancellations"]["count"] == 1
        assert result["returns"]["count"] == 1
        assert result["exchanges"]["count"] == 1
        assert result["overall_status"] == "SUCCESS"
        cancel = db_session.query(Cancellation).filter_by(order_id=order.id).one()
        assert cancel.status == "COMPLETED" and cancel.completed_at is not None and cancel.reason == "변심"
        assert db_session.query(Return).filter_by(order_id=order.id).one().completed_at is None
        assert db_session.query(Exchange).filter_by(order_id=order.id).count() == 1

    def test_deduplicates_on_resync_no_change(self, db_session, platform):
        order = _make_order(db_session, platform)
        conn = StubClaimConnector(returns=[_claim(order.platform_order_no, status="REQUESTED")], supports=("return",))
        svc = ClaimSyncService(db_session)
        first = svc.sync_claims(conn, platform.id, *SPAN)
        second = svc.sync_claims(conn, platform.id, *SPAN)
        assert first["returns"]["count"] == 1
        assert second["returns"]["count"] == 0  # 재수집 시 중복 생성 없음(기존 데이터 변경 없음)
        assert db_session.query(Return).filter_by(order_id=order.id).count() == 1

    def test_skips_when_order_not_found(self, db_session, platform):
        conn = StubClaimConnector(cancellations=[_claim("NO-SUCH-ORDER")], supports=("cancellation",))
        result = ClaimSyncService(db_session).sync_claims(conn, platform.id, *SPAN)
        assert result["cancellations"]["status"] == "SUCCESS" and result["cancellations"]["count"] == 0
        assert result["skipped_no_order"] == 1
        assert db_session.query(Cancellation).count() == 0


class TestFeatureIsolation:
    def test_success_plus_credential_failure_isolated(self, db_session, platform):
        order = _make_order(db_session, platform)
        conn = StubClaimConnector(
            cancellations=[_claim(order.platform_order_no)],
            errors={"returns": MarketplaceCredentialMissingError("naver")},
            exchanges=[_claim(order.platform_order_no)],
        )
        result = ClaimSyncService(db_session).sync_claims(conn, platform.id, *SPAN)

        assert result["cancellations"]["status"] == "SUCCESS"
        assert result["returns"] == {
            "status": "FAILED",
            "count": 0,
            "reason_code": "CREDENTIAL_MISSING",
            "retryable": False,
        }
        assert result["exchanges"]["status"] == "SUCCESS"  # 실패 후 다음 기능 계속
        assert result["overall_status"] == "PARTIAL"
        # 앞/뒤 성공 기능 데이터 유지
        assert db_session.query(Cancellation).filter_by(order_id=order.id).count() == 1
        assert db_session.query(Exchange).filter_by(order_id=order.id).count() == 1
        assert db_session.query(Return).filter_by(order_id=order.id).count() == 0

    def test_external_api_failure_propagates_retryable(self, db_session, platform):
        conn = StubClaimConnector(
            errors={"cancellations": MarketplaceExternalAPIError("naver", "SERVER_ERROR", True)},
            supports=("cancellation",),
        )
        result = ClaimSyncService(db_session).sync_claims(conn, platform.id, *SPAN)
        assert result["cancellations"] == {
            "status": "FAILED",
            "count": 0,
            "reason_code": "SERVER_ERROR",
            "retryable": True,
        }
        assert result["overall_status"] == "FAILED"

    def test_unexpected_exception_isolated_without_leak(self, db_session, platform):
        order = _make_order(db_session, platform)
        secret = "SUPER-SECRET-보안값"
        conn = StubClaimConnector(
            cancellations=[_claim(order.platform_order_no)], errors={"returns": RuntimeError(secret)}
        )
        result = ClaimSyncService(db_session).sync_claims(conn, platform.id, *SPAN)
        assert result["returns"] == {
            "status": "FAILED",
            "count": 0,
            "reason_code": "INTERNAL_ERROR",
            "retryable": False,
        }
        assert result["cancellations"]["status"] == "SUCCESS"
        # 내부 예외 전문(secret)이 결과 어디에도 노출되지 않는다.
        assert secret not in str(result)

    def test_db_write_failure_isolated_via_savepoint(self, db_session, platform):
        order = _make_order(db_session, platform)
        conn = StubClaimConnector(
            cancellations=[_claim(order.platform_order_no)],
            returns=[_claim(order.platform_order_no)],
            exchanges=[_claim(order.platform_order_no)],
        )
        svc = ClaimSyncService(db_session)

        # 반품 저장만 DB 오류를 내도록 강제(SAVEPOINT 격리 검증).
        def _boom(*a, **k):
            raise SQLAlchemyError("db boom")

        svc.return_repo.add = _boom  # type: ignore[method-assign]

        result = svc.sync_claims(conn, platform.id, *SPAN)

        assert result["returns"] == {
            "status": "FAILED",
            "count": 0,
            "reason_code": "DB_WRITE_FAILED",
            "retryable": False,
        }
        assert result["cancellations"]["status"] == "SUCCESS"
        assert result["exchanges"]["status"] == "SUCCESS"
        assert result["overall_status"] == "PARTIAL"
        # 반품만 롤백, 취소/교환은 유지
        assert db_session.query(Cancellation).filter_by(order_id=order.id).count() == 1
        assert db_session.query(Exchange).filter_by(order_id=order.id).count() == 1
        assert db_session.query(Return).filter_by(order_id=order.id).count() == 0


class TestOverallStatus:
    def _run(self, db_session, platform, statuses):
        """statuses: dict feature->('success'|'unsupported'|'fail')로 결과를 유도."""
        order = _make_order(db_session, platform)
        supports = tuple(f for f, s in statuses.items() if s != "unsupported")
        data = {f + "s": ([_claim(order.platform_order_no)] if s == "success" else []) for f, s in statuses.items()}
        errors = {
            f + "s": MarketplaceExternalAPIError("x", "SERVER_ERROR", False) for f, s in statuses.items() if s == "fail"
        }
        conn = StubClaimConnector(
            cancellations=data.get("cancellations"),
            returns=data.get("returns"),
            exchanges=data.get("exchanges"),
            supports=supports,
            errors=errors,
        )
        return ClaimSyncService(db_session).sync_claims(conn, platform.id, *SPAN)["overall_status"]

    def test_all_success(self, db_session, platform):
        assert (
            self._run(db_session, platform, {"cancellation": "success", "return": "success", "exchange": "success"})
            == "SUCCESS"
        )

    def test_success_and_fail_is_partial(self, db_session, platform):
        assert (
            self._run(db_session, platform, {"cancellation": "success", "return": "fail", "exchange": "unsupported"})
            == "PARTIAL"
        )

    def test_all_supported_failed(self, db_session, platform):
        assert (
            self._run(db_session, platform, {"cancellation": "fail", "return": "fail", "exchange": "unsupported"})
            == "FAILED"
        )

    def test_unsupported_and_failed_no_success(self, db_session, platform):
        assert (
            self._run(
                db_session, platform, {"cancellation": "unsupported", "return": "fail", "exchange": "unsupported"}
            )
            == "FAILED"
        )
