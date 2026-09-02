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
from models.order import Cancellation, ClaimUnmatched, Exchange, Order, OrderItem, Return
from models.product import Product, ProductOption
from repositories.order_repository import ClaimUnmatchedRepository
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


class TestClaimIdBasedDedupAndUpdate:
    """2단계 확장: platform_claim_id가 있으면 같은 유형의 클레임이 여러 건이어도
    각각 별도 행으로 만들고, 재수집 시 새로 만들지 않고 기존 행을 갱신한다."""

    def test_two_claims_same_order_same_type_both_created(self, db_session, platform):
        """하나의 주문에 같은 유형(반품) 클레임 여러 건을 지원한다(부분 클레임)."""
        order = _make_order(db_session, platform)
        conn = StubClaimConnector(
            returns=[
                _claim(order.platform_order_no, platform_claim_id="R-1", reason="사이즈 불만"),
                _claim(order.platform_order_no, platform_claim_id="R-2", reason="색상 불만"),
            ],
            supports=("return",),
        )
        result = ClaimSyncService(db_session).sync_claims(conn, platform.id, *SPAN)

        assert result["returns"]["count"] == 2
        assert db_session.query(Return).filter_by(order_id=order.id).count() == 2

    def test_resync_updates_existing_claim_instead_of_duplicating(self, db_session, platform):
        order = _make_order(db_session, platform)
        conn = StubClaimConnector(
            returns=[_claim(order.platform_order_no, platform_claim_id="R-1", status="REQUESTED")], supports=("return",)
        )
        svc = ClaimSyncService(db_session)
        svc.sync_claims(conn, platform.id, *SPAN)

        conn._data["returns"][0]["status"] = "APPROVED"  # 채널에서 승인 처리됨
        result = svc.sync_claims(conn, platform.id, *SPAN)

        assert db_session.query(Return).filter_by(order_id=order.id).count() == 1  # 새 행이 생기지 않는다.
        updated = db_session.query(Return).filter_by(order_id=order.id).one()
        assert updated.status == "APPROVED"
        assert result["returns"]["count"] == 1  # 갱신도 "처리됨"으로 집계.

    def test_stale_response_does_not_regress_status(self, db_session, platform):
        """오래된 응답(이미 REFUNDED인데 REQUESTED가 다시 옴)이 최신 상태를 되돌리지 않는다."""
        order = _make_order(db_session, platform)
        conn = StubClaimConnector(
            returns=[_claim(order.platform_order_no, platform_claim_id="R-1", status="REFUNDED")], supports=("return",)
        )
        svc = ClaimSyncService(db_session)
        svc.sync_claims(conn, platform.id, *SPAN)

        conn._data["returns"][0]["status"] = "REQUESTED"  # 오래된(지연 도착) 응답 재현
        svc.sync_claims(conn, platform.id, *SPAN)

        assert db_session.query(Return).filter_by(order_id=order.id).one().status == "REFUNDED"

    def test_unknown_raw_status_preserved_as_review_not_completed(self, db_session, platform):
        """알 수 없는 상태는 REVIEW로 보존하고 완료로 추정하지 않는다."""
        order = _make_order(db_session, platform)
        conn = StubClaimConnector(
            returns=[
                _claim(order.platform_order_no, platform_claim_id="R-1", status="REVIEW", raw_status="XYZ_UNKNOWN")
            ],
            supports=("return",),
        )
        result = ClaimSyncService(db_session).sync_claims(conn, platform.id, *SPAN)

        ret = db_session.query(Return).filter_by(order_id=order.id).one()
        assert ret.status == "REVIEW"
        assert ret.raw_status == "XYZ_UNKNOWN"
        assert ret.completed_at is None
        assert result["returns"]["count"] == 1

    def test_review_status_upgrades_to_recognized_status_later(self, db_session, platform):
        order = _make_order(db_session, platform)
        conn = StubClaimConnector(
            returns=[_claim(order.platform_order_no, platform_claim_id="R-1", status="REVIEW", raw_status="XYZ")],
            supports=("return",),
        )
        svc = ClaimSyncService(db_session)
        svc.sync_claims(conn, platform.id, *SPAN)

        conn._data["returns"][0]["status"] = "REQUESTED"
        conn._data["returns"][0]["raw_status"] = "NOW_RECOGNIZED"
        svc.sync_claims(conn, platform.id, *SPAN)

        assert db_session.query(Return).filter_by(order_id=order.id).one().status == "REQUESTED"

    def test_quantity_shipping_fee_fault_type_persisted(self, db_session, platform):
        order = _make_order(db_session, platform)
        conn = StubClaimConnector(
            exchanges=[
                _claim(
                    order.platform_order_no,
                    platform_claim_id="E-1",
                    quantity=2,
                    shipping_fee=3000,
                    fault_type="CUSTOMER",
                )
            ],
            supports=("exchange",),
        )
        ClaimSyncService(db_session).sync_claims(conn, platform.id, *SPAN)

        exch = db_session.query(Exchange).filter_by(order_id=order.id).one()
        assert exch.quantity == 2
        assert exch.shipping_fee == 3000
        assert exch.fault_type == "CUSTOMER"

    def test_order_item_linked_by_platform_order_item_no(self, db_session, platform):
        order = _make_order(db_session, platform)
        product = Product(name="p", category="c", base_price=1000, status="ACTIVE")
        db_session.add(product)
        db_session.flush()
        option = ProductOption(product_id=product.id, sku_code="SKU-1", is_active=True)
        db_session.add(option)
        db_session.flush()
        item = OrderItem(
            order_id=order.id,
            product_option_id=option.id,
            platform_order_item_no="LINE-1",
            quantity=1,
            unit_price=1000,
            line_amount=1000,
        )
        db_session.add(item)
        db_session.flush()

        conn = StubClaimConnector(
            returns=[_claim(order.platform_order_no, platform_claim_id="R-1", platform_order_item_no="LINE-1")],
            supports=("return",),
        )
        ClaimSyncService(db_session).sync_claims(conn, platform.id, *SPAN)

        ret = db_session.query(Return).filter_by(order_id=order.id).one()
        assert ret.order_item_id == item.id


class TestUnmatchedClaimPreservation:
    """주문이 아직 없는 클레임은 조용히 버리지 않고 보존하며, 이후 주문이 수집되면
    다음 재수집 시 실제 클레임 행으로 승격한다."""

    def test_unmatched_claim_is_preserved_not_dropped(self, db_session, platform):
        conn = StubClaimConnector(
            returns=[_claim("NO-SUCH-ORDER", platform_claim_id="R-1", reason="반품사유")], supports=("return",)
        )
        ClaimSyncService(db_session).sync_claims(conn, platform.id, *SPAN)

        held = db_session.query(ClaimUnmatched).all()
        assert len(held) == 1
        assert held[0].platform_order_no == "NO-SUCH-ORDER"
        assert held[0].claim_type == "RETURN"
        assert held[0].resolved_at is None

    def test_resync_of_unmatched_does_not_duplicate_holding_row(self, db_session, platform):
        conn = StubClaimConnector(returns=[_claim("NO-SUCH-ORDER", platform_claim_id="R-1")], supports=("return",))
        svc = ClaimSyncService(db_session)
        svc.sync_claims(conn, platform.id, *SPAN)
        svc.sync_claims(conn, platform.id, *SPAN)

        assert db_session.query(ClaimUnmatched).count() == 1

    def test_order_collected_later_resolves_unmatched_on_next_sync(self, db_session, platform):
        conn = StubClaimConnector(
            returns=[_claim("LATE-ORDER", platform_claim_id="R-1", reason="반품사유")], supports=("return",)
        )
        svc = ClaimSyncService(db_session)
        svc.sync_claims(conn, platform.id, *SPAN)
        assert db_session.query(Return).count() == 0

        # order_collect_job이 이후 이 주문을 수집했다고 가정.
        order = _make_order(db_session, platform, order_no="LATE-ORDER")

        result = svc.sync_claims(conn, platform.id, *SPAN)

        assert result["resolved_unmatched"] == 1
        promoted = db_session.query(Return).filter_by(order_id=order.id).one()
        assert promoted.platform_claim_id == "R-1"
        # 같은 sync_claims() 호출 안에서 승격(REVIEW) 직후 정규 기능별 수집이 곧바로
        # 같은 클레임을 다시 조회해 실제 상태(REQUESTED)까지 채운다 - REVIEW는 항상
        # 인식된 상태로 올라갈 수 있으므로(should_apply_claim_status) 한 번에 갱신된다.
        assert promoted.status == "REQUESTED"
        unmatched = db_session.query(ClaimUnmatched).filter_by(platform_claim_id="R-1").one()
        assert unmatched.resolved_at is not None
        assert unmatched.resolved_entity_id == promoted.id

    def test_no_platform_claim_id_still_holds_unmatched(self, db_session, platform):
        """공식 claim ID가 없는 경우에도(추측 없이) 미매칭 보존은 동작해야 한다."""
        conn = StubClaimConnector(cancellations=[_claim("NO-SUCH-ORDER-2")], supports=("cancellation",))
        ClaimSyncService(db_session).sync_claims(conn, platform.id, *SPAN)

        held = ClaimUnmatchedRepository(db_session).list_unresolved(platform.id)
        assert len(held) == 1
        assert held[0].platform_claim_id is None
