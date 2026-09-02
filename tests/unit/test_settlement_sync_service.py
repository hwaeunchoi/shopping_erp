"""
tests/unit/test_settlement_sync_service.py
------------------------------------------------
SettlementSyncService: 정산 회차 요약/상세 수집 + 대사(reconciliation)를 검증한다.

- capability(지원/미지원) 인지, 기능별 SAVEPOINT 격리(한 기능 실패가 다른 기능을
  막지 않음).
- 정산 회차 upsert(platform_id, settlement_cycle, settlement_type) 중복방지.
- 정산 상세 자연키 dedup, 주문/정산 매칭 실패는 SettlementDiscrepancy로(추정 금지),
  같은 미해소 불일치는 재수집해도 중복 생성하지 않음.
- 금액 불일치(AMOUNT_MISMATCH) 탐지 - Decimal로만 계산.
실제 쇼핑몰 API는 호출하지 않는다(스텁만 사용).
"""

from datetime import date, datetime, timezone
from decimal import Decimal

from integrations.malls.errors import MarketplaceCredentialMissingError, MarketplaceExternalAPIError
from models.order import Order, OrderItem
from models.product import Product, ProductOption
from models.settlement import Settlement, SettlementDetail, SettlementDiscrepancy
from services.settlement_sync_service import SettlementSyncService


class StubSettlementConnector:
    def __init__(self, *, settlements=None, details=None, supports=("settlements", "details"), errors=None):
        self._data = {"settlements": settlements or [], "details": details or []}
        self._errors = errors or {}
        self.supports_settlement_sync = "settlements" in supports
        self.supports_settlement_detail_sync = "details" in supports
        self.calls: list[str] = []

    def _feature(self, name):
        self.calls.append(name)
        if name in self._errors:
            raise self._errors[name]
        return self._data[name]

    def fetch_settlements(self, s, e):
        return self._feature("settlements")

    def fetch_settlement_details(self, s, e):
        return self._feature("details")


SPAN = (date(2026, 1, 1), date(2026, 1, 31))


def _settlement_raw(cycle="2026-01-31", settlement_type="MONTHLY", expected=100000, settled=95000, status="COMPLETED"):
    return {
        "settlement_cycle": cycle,
        "settlement_type": settlement_type,
        "scheduled_date": date(2026, 1, 31),
        "settled_date": date(2026, 1, 31) if status == "COMPLETED" else None,
        "expected_amount": Decimal(expected),
        "settled_amount": Decimal(settled) if status == "COMPLETED" else Decimal("0"),
        "status": status,
    }


def _make_order(db_session, platform, order_no="SETL-ORD-1"):
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


def _make_order_item(db_session, order, poin="ITEM-1"):
    product = Product(name="p", category="c", base_price=1000, status="ACTIVE")
    db_session.add(product)
    db_session.flush()
    option = ProductOption(product_id=product.id, sku_code=f"SKU-{poin}", is_active=True)
    db_session.add(option)
    db_session.flush()
    item = OrderItem(
        order_id=order.id,
        product_option_id=option.id,
        platform_order_item_no=poin,
        quantity=1,
        unit_price=1000,
        line_amount=1000,
    )
    db_session.add(item)
    db_session.flush()
    return item


def _detail_raw(order_no, poin=None, gross=20000, fee=2200, net=17800, sale_type="SALE", rec_date=date(2026, 1, 31)):
    return {
        "platform_order_no": order_no,
        "platform_order_item_no": poin,
        "sale_type": sale_type,
        "recognition_date": rec_date,
        "settled_date": rec_date,
        "gross_amount": Decimal(gross),
        "fee_amount": Decimal(fee),
        "net_amount": Decimal(net),
    }


class TestCapabilityGating:
    def test_unsupported_settlement_does_not_call_fetch(self, db_session, platform):
        conn = StubSettlementConnector(supports=())
        result = SettlementSyncService(db_session).sync_settlements(conn, platform.id, *SPAN)
        assert conn.calls == []
        assert result == {
            "status": "UNSUPPORTED",
            "count": 0,
            "reason_code": "CAPABILITY_UNSUPPORTED",
            "retryable": False,
        }

    def test_unsupported_details_does_not_call_fetch(self, db_session, platform):
        conn = StubSettlementConnector(supports=())
        result = SettlementSyncService(db_session).sync_settlement_details(conn, platform.id, *SPAN)
        assert conn.calls == []
        assert result["status"] == "UNSUPPORTED"


class TestSettlementUpsert:
    def test_creates_new_settlement(self, db_session, platform):
        conn = StubSettlementConnector(settlements=[_settlement_raw()], supports=("settlements",))
        result = SettlementSyncService(db_session).sync_settlements(conn, platform.id, *SPAN)

        assert result["status"] == "SUCCESS"
        assert result["count"] == 1
        settlement = db_session.query(Settlement).filter_by(platform_id=platform.id).one()
        assert settlement.settlement_type == "MONTHLY"
        assert settlement.settled_amount == Decimal("95000")

    def test_resync_updates_instead_of_duplicating(self, db_session, platform):
        conn = StubSettlementConnector(settlements=[_settlement_raw(settled=95000)], supports=("settlements",))
        svc = SettlementSyncService(db_session)
        svc.sync_settlements(conn, platform.id, *SPAN)

        conn._data["settlements"][0]["settled_amount"] = Decimal("96000")
        svc.sync_settlements(conn, platform.id, *SPAN)

        assert db_session.query(Settlement).filter_by(platform_id=platform.id).count() == 1
        assert db_session.query(Settlement).filter_by(platform_id=platform.id).one().settled_amount == Decimal("96000")

    def test_same_cycle_different_type_are_separate_settlements(self, db_session, platform):
        """같은 settlement_cycle 문자열이라도 settlement_type이 다르면 별개 정산 건이다."""
        conn = StubSettlementConnector(
            settlements=[
                _settlement_raw(cycle="2026-01-31", settlement_type="MONTHLY"),
                _settlement_raw(cycle="2026-01-31", settlement_type="WEEKLY"),
            ],
            supports=("settlements",),
        )
        result = SettlementSyncService(db_session).sync_settlements(conn, platform.id, *SPAN)

        assert result["count"] == 2
        assert db_session.query(Settlement).filter_by(platform_id=platform.id).count() == 2


class TestSettlementDetailReconciliation:
    def test_detail_links_to_order_and_settlement(self, db_session, platform):
        order = _make_order(db_session, platform)
        item = _make_order_item(db_session, order, poin="ITEM-1")
        conn = StubSettlementConnector(
            settlements=[_settlement_raw()], details=[_detail_raw(order.platform_order_no, poin="ITEM-1")]
        )
        svc = SettlementSyncService(db_session)
        svc.sync_settlements(conn, platform.id, *SPAN)
        result = svc.sync_settlement_details(conn, platform.id, *SPAN)

        assert result["status"] == "SUCCESS"
        detail = db_session.query(SettlementDetail).filter_by(order_id=order.id).one()
        assert detail.order_item_id == item.id
        assert detail.net_amount == Decimal("17800")
        assert detail.sale_type == "SALE"

    def test_resync_updates_detail_instead_of_duplicating(self, db_session, platform):
        order = _make_order(db_session, platform)
        conn = StubSettlementConnector(
            settlements=[_settlement_raw()], details=[_detail_raw(order.platform_order_no, net=17800)]
        )
        svc = SettlementSyncService(db_session)
        svc.sync_settlements(conn, platform.id, *SPAN)
        svc.sync_settlement_details(conn, platform.id, *SPAN)

        conn._data["details"][0]["net_amount"] = Decimal("18000")
        svc.sync_settlement_details(conn, platform.id, *SPAN)

        assert db_session.query(SettlementDetail).filter_by(order_id=order.id).count() == 1
        assert db_session.query(SettlementDetail).filter_by(order_id=order.id).one().net_amount == Decimal("18000")

    def test_no_matching_order_creates_discrepancy_and_does_not_create_detail(self, db_session, platform):
        conn = StubSettlementConnector(settlements=[_settlement_raw()], details=[_detail_raw("NO-SUCH-ORDER")])
        svc = SettlementSyncService(db_session)
        svc.sync_settlements(conn, platform.id, *SPAN)
        svc.sync_settlement_details(conn, platform.id, *SPAN)

        assert db_session.query(SettlementDetail).count() == 0
        disc = db_session.query(SettlementDiscrepancy).filter_by(reason="NO_MATCHING_ORDER").one()
        assert disc.resolved_at is None

    def test_no_matching_order_discrepancy_not_duplicated_on_resync(self, db_session, platform):
        conn = StubSettlementConnector(settlements=[_settlement_raw()], details=[_detail_raw("NO-SUCH-ORDER")])
        svc = SettlementSyncService(db_session)
        svc.sync_settlements(conn, platform.id, *SPAN)
        svc.sync_settlement_details(conn, platform.id, *SPAN)
        svc.sync_settlement_details(conn, platform.id, *SPAN)

        assert db_session.query(SettlementDiscrepancy).filter_by(reason="NO_MATCHING_ORDER").count() == 1

    def test_no_matching_settlement_creates_discrepancy(self, db_session, platform):
        """정산 회차 자체가 아직 수집되지 않았으면(날짜가 안 맞으면) 매칭 실패로 남긴다 -
        주문 매출과 정산 입금을 섞어 집계하지 않는다."""
        order = _make_order(db_session, platform)
        conn = StubSettlementConnector(
            settlements=[], details=[_detail_raw(order.platform_order_no, rec_date=date(2026, 1, 31))]
        )
        svc = SettlementSyncService(db_session)
        svc.sync_settlement_details(conn, platform.id, *SPAN)  # settlements 없이 details만 수집.

        assert db_session.query(SettlementDetail).count() == 0
        disc = db_session.query(SettlementDiscrepancy).filter_by(reason="NO_MATCHING_SETTLEMENT").one()
        assert disc.order_id == order.id

    def test_amount_mismatch_detected_after_details_synced(self, db_session, platform):
        order = _make_order(db_session, platform)
        conn = StubSettlementConnector(
            settlements=[_settlement_raw(settled=95000)],
            details=[_detail_raw(order.platform_order_no, net=17800)],  # 회차 합계(95000)와 다름.
        )
        svc = SettlementSyncService(db_session)
        svc.sync_settlements(conn, platform.id, *SPAN)
        svc.sync_settlement_details(conn, platform.id, *SPAN)

        disc = db_session.query(SettlementDiscrepancy).filter_by(reason="AMOUNT_MISMATCH").one()
        assert disc.expected_amount == Decimal("95000")
        assert disc.actual_amount == Decimal("17800")
        assert disc.diff_amount == Decimal("17800") - Decimal("95000")

    def test_refund_negative_amount_not_flipped(self, db_session, platform):
        order = _make_order(db_session, platform)
        conn = StubSettlementConnector(
            settlements=[_settlement_raw(settled=-2000)],
            details=[_detail_raw(order.platform_order_no, gross=-2000, fee=0, net=-2000, sale_type="REFUND")],
        )
        svc = SettlementSyncService(db_session)
        svc.sync_settlements(conn, platform.id, *SPAN)
        svc.sync_settlement_details(conn, platform.id, *SPAN)

        detail = db_session.query(SettlementDetail).filter_by(order_id=order.id).one()
        assert detail.sale_type == "REFUND"
        assert detail.net_amount == Decimal("-2000")  # 부호가 임의로 뒤집히지 않았다.
        assert db_session.query(SettlementDiscrepancy).filter_by(reason="AMOUNT_MISMATCH").count() == 0


class TestFeatureIsolation:
    def test_settlement_failure_does_not_block_details(self, db_session, platform):
        order = _make_order(db_session, platform)
        conn = StubSettlementConnector(
            settlements=[_settlement_raw()],
            details=[_detail_raw(order.platform_order_no)],
            errors={"settlements": MarketplaceCredentialMissingError("coupang")},
        )
        svc = SettlementSyncService(db_session)
        settlements_result = svc.sync_settlements(conn, platform.id, *SPAN)
        details_result = svc.sync_settlement_details(conn, platform.id, *SPAN)

        assert settlements_result["status"] == "FAILED"
        assert settlements_result["reason_code"] == "CREDENTIAL_MISSING"
        # settlements가 실패했어도(회차가 없어도) details 수집 자체는 별도 기능으로 계속된다
        # (다만 매칭할 회차가 없으므로 NO_MATCHING_SETTLEMENT로 남는다).
        assert details_result["status"] == "SUCCESS"
        assert db_session.query(SettlementDiscrepancy).filter_by(reason="NO_MATCHING_SETTLEMENT").count() == 1

    def test_external_api_error_reports_retryable(self, db_session, platform):
        conn = StubSettlementConnector(
            errors={"settlements": MarketplaceExternalAPIError("coupang", "SERVER_ERROR", True)},
            supports=("settlements",),
        )
        result = SettlementSyncService(db_session).sync_settlements(conn, platform.id, *SPAN)
        assert result == {"status": "FAILED", "count": 0, "reason_code": "SERVER_ERROR", "retryable": True}
