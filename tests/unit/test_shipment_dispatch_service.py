"""
tests/unit/test_shipment_dispatch_service.py
--------------------------------------------------
ShipmentDispatchService: 채널 전송의 idempotency/상태검증/부분성공-실패
격리/실패시 성공위장 금지를 가짜(Fake) 커넥터로 검증한다(네트워크 없음).

실제 Naver/Coupang 커넥터의 submit_shipment() 자체(HTTP 요청 구성)는
tests/unit/test_naver_smartstore_connector.py / test_coupang_connector.py의
MockTransport 테스트에서 별도로 검증한다 - 이 파일은 서비스 계층(outbox/
idempotency/트랜잭션 격리)만 검증한다.
"""

from datetime import date, datetime, timezone

import pytest

from integrations.malls.base_mall_connector import BaseMallConnector, ShipmentSubmitResult
from integrations.malls.errors import MarketplaceCapabilityUnsupportedError, MarketplaceExternalAPIError
from models.order import Order, OrderItem, Shipment, ShipmentItem
from models.product import Product, ProductOption
from repositories.integration_sync_repository import ExternalCommandRepository
from services.shipment_dispatch_service import (
    ShipmentDispatchService,
    ShipmentNotReadyError,
    ShipmentPlatformMismatchError,
    ShipmentSubmitRejectedError,
)


class _FakeConnector(BaseMallConnector):
    platform_code = "naver_smartstore"
    supports_shipment_submit = True

    def __init__(self, outcome="accept", session=None, platform_id=None):
        super().__init__(session=session, platform_id=platform_id)
        self.outcome = outcome
        self.calls: list[tuple] = []

    def fetch_orders(self, start_date, end_date):
        raise NotImplementedError

    def fetch_order_detail(self, platform_order_no):
        raise NotImplementedError

    def update_shipment(self, platform_order_no, carrier, tracking_no):
        raise NotImplementedError

    def fetch_settlements(self, start_date, end_date):
        raise NotImplementedError

    def submit_shipment(
        self,
        platform_order_item_no,
        carrier_code,
        tracking_no,
        dispatch_date,
        platform_order_no=None,
        platform_shipment_box_id=None,
    ):
        self.calls.append((platform_order_item_no, carrier_code, tracking_no))
        if self.outcome == "accept":
            return ShipmentSubmitResult(accepted=True, platform_result_code="OK")
        if self.outcome == "reject":
            return ShipmentSubmitResult(accepted=False, platform_result_code="FAIL_CODE")
        if self.outcome == "external_error":
            raise MarketplaceExternalAPIError("naver_smartstore", "SERVER_ERROR", True, http_status=500)
        raise AssertionError(f"unknown outcome: {self.outcome}")


class _ConnectorFactory:
    """호출 가능(connector_factory 계약)하면서, 테스트에서 만든 커넥터를
    .connector로 그대로 들여다볼 수 있게 하는 작은 헬퍼."""

    def __init__(self, outcome: str = "accept") -> None:
        self.connector = _FakeConnector(outcome=outcome)

    def __call__(self, connector_class: str, session, platform_id):
        return self.connector


def _make_factory(outcome="accept") -> _ConnectorFactory:
    return _ConnectorFactory(outcome=outcome)


def _seed_shippable_order(db_session, platform, poin="PO-1", carrier="CJ_LOGISTICS", tracking_no="TRACK-1"):
    product = Product(name="테스트 상품", category="테스트", base_price=10000, status="ACTIVE")
    db_session.add(product)
    db_session.flush()
    option = ProductOption(product_id=product.id, sku_code=f"SKU-{poin}", is_active=True)
    db_session.add(option)
    db_session.flush()

    order = Order(
        platform_id=platform.id,
        platform_order_no=f"ORDER-{poin}",
        status="PREPARING",
        order_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        total_amount=10000,
    )
    db_session.add(order)
    db_session.flush()
    item = OrderItem(
        order_id=order.id,
        product_option_id=option.id,
        platform_order_item_no=poin,
        quantity=1,
        unit_price=10000,
        line_amount=10000,
    )
    db_session.add(item)
    db_session.flush()

    shipment = Shipment(carrier=carrier, tracking_no=tracking_no, status="READY")
    db_session.add(shipment)
    db_session.flush()
    db_session.add(ShipmentItem(shipment_id=shipment.id, order_id=order.id, order_item_id=item.id))
    db_session.flush()
    return order, item, shipment


class TestSuccessfulSingleSubmit:
    def test_accepted_marks_command_success(self, db_session, platform):
        _, _, shipment = _seed_shippable_order(db_session, platform)
        factory = _make_factory("accept")
        service = ShipmentDispatchService(db_session, connector_factory=factory)

        outcome = service.submit(shipment.id, dispatch_date=date(2026, 1, 2))

        assert outcome.command.status == "SUCCESS"
        assert outcome.already_processed is False
        assert factory.connector.calls == [("PO-1", "CJGLS", "TRACK-1")]

    def test_uses_platform_order_item_no_not_order_no(self, db_session, platform):
        """상품주문번호(라인)를 보내야 한다 - 주문번호(Order 단위)를 잘못 보내면 안 된다."""
        order, item, shipment = _seed_shippable_order(db_session, platform, poin="PO-DISTINCT")
        factory = _make_factory("accept")
        service = ShipmentDispatchService(db_session, connector_factory=factory)

        service.submit(shipment.id)

        sent_id = factory.connector.calls[0][0]
        assert sent_id == "PO-DISTINCT"
        assert sent_id != order.platform_order_no


class TestNotReadyIsBlocked:
    def test_non_ready_shipment_status_is_rejected(self, db_session, platform):
        _, _, shipment = _seed_shippable_order(db_session, platform)
        shipment.status = "SHIPPING"
        db_session.flush()
        service = ShipmentDispatchService(db_session, connector_factory=_make_factory("accept"))

        with pytest.raises(ShipmentNotReadyError):
            service.submit(shipment.id)

    def test_missing_tracking_no_is_rejected(self, db_session, platform):
        _, _, shipment = _seed_shippable_order(db_session, platform, tracking_no=None)
        service = ShipmentDispatchService(db_session, connector_factory=_make_factory("accept"))

        with pytest.raises(ShipmentNotReadyError):
            service.submit(shipment.id)


class TestIdempotency:
    def test_resubmitting_same_shipment_and_tracking_no_does_not_call_connector_again(self, db_session, platform):
        _, _, shipment = _seed_shippable_order(db_session, platform)
        factory = _make_factory("accept")
        service = ShipmentDispatchService(db_session, connector_factory=factory)

        first = service.submit(shipment.id)
        second = service.submit(shipment.id)

        assert first.already_processed is False
        assert second.already_processed is True
        assert second.command.id == first.command.id
        assert len(factory.connector.calls) == 1  # 두 번째는 실제 API를 다시 부르지 않는다.


class TestRejectionIsNotShownAsSuccess:
    def test_platform_rejection_marks_command_failed_and_raises(self, db_session, platform):
        _, _, shipment = _seed_shippable_order(db_session, platform)
        service = ShipmentDispatchService(db_session, connector_factory=_make_factory("reject"))

        with pytest.raises(ShipmentSubmitRejectedError):
            service.submit(shipment.id)

        command = ExternalCommandRepository(db_session).get_by_idempotency_key(f"SHIPMENT_SUBMIT:{shipment.id}:TRACK-1")
        assert command is not None
        assert command.status == "FAILED"

    def test_external_api_error_marks_retryable_failed(self, db_session, platform):
        _, _, shipment = _seed_shippable_order(db_session, platform)
        service = ShipmentDispatchService(db_session, connector_factory=_make_factory("external_error"))

        with pytest.raises(MarketplaceExternalAPIError):
            service.submit(shipment.id)

        command = ExternalCommandRepository(db_session).get_by_idempotency_key(f"SHIPMENT_SUBMIT:{shipment.id}:TRACK-1")
        assert command is not None
        assert command.status == "FAILED"
        assert command.retryable is True


class TestUnsupportedCapabilityNeverReturnsSuccess:
    def test_capability_unsupported_bubbles_up_as_failure(self, db_session, platform):
        class _UnsupportedConnector(BaseMallConnector):
            platform_code = "esm"

            def fetch_orders(self, *a, **k):
                raise NotImplementedError

            def fetch_order_detail(self, *a, **k):
                raise NotImplementedError

            def update_shipment(self, *a, **k):
                raise NotImplementedError

            def fetch_settlements(self, *a, **k):
                raise NotImplementedError

        conn = _UnsupportedConnector()

        def factory(connector_class, session, platform_id):
            return conn

        _, _, shipment = _seed_shippable_order(db_session, platform)
        service = ShipmentDispatchService(db_session, connector_factory=factory)

        with pytest.raises(MarketplaceCapabilityUnsupportedError):
            service.submit(shipment.id)


class TestBulkSubmitPartialSuccess:
    def test_one_failure_does_not_block_other_shipments(self, db_session, platform):
        _, _, ok_shipment = _seed_shippable_order(db_session, platform, poin="PO-OK", tracking_no="TRACK-OK")
        _, _, fail_shipment = _seed_shippable_order(db_session, platform, poin="PO-FAIL", tracking_no="TRACK-FAIL")

        calls = {"n": 0}

        class _MixedConnector(_FakeConnector):
            def submit_shipment(self, platform_order_item_no, *a, **k):
                calls["n"] += 1
                if platform_order_item_no == "PO-FAIL":
                    return ShipmentSubmitResult(accepted=False, platform_result_code="FAIL_CODE")
                return ShipmentSubmitResult(accepted=True, platform_result_code="OK")

        conn = _MixedConnector()

        def factory(connector_class, session, platform_id):
            return conn

        service = ShipmentDispatchService(db_session, connector_factory=factory)

        results = service.submit_many([ok_shipment.id, fail_shipment.id])

        by_id = {r["shipment_id"]: r for r in results}
        assert by_id[ok_shipment.id]["success"] is True
        assert by_id[fail_shipment.id]["success"] is False
        assert calls["n"] == 2  # 실패한 건도 시도는 했다(건너뛰지 않음).

        # DB 저장 격리: 성공 건의 outbox는 SUCCESS로 남아있어야 한다(실패 건 롤백에 영향받지 않음).
        ok_command = ExternalCommandRepository(db_session).get_by_idempotency_key(
            f"SHIPMENT_SUBMIT:{ok_shipment.id}:TRACK-OK"
        )
        assert ok_command is not None
        assert ok_command.status == "SUCCESS"


class TestPlatformMismatch:
    def test_shipment_spanning_multiple_platforms_is_rejected(self, db_session, platform, naver_platform):
        product = Product(name="p", category="c", base_price=1000, status="ACTIVE")
        db_session.add(product)
        db_session.flush()
        option = ProductOption(product_id=product.id, sku_code="SKU-MIX", is_active=True)
        db_session.add(option)
        db_session.flush()

        order_a = Order(
            platform_id=platform.id,
            platform_order_no="A-1",
            status="PREPARING",
            order_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
            total_amount=1000,
        )
        order_b = Order(
            platform_id=naver_platform.id,
            platform_order_no="B-1",
            status="PREPARING",
            order_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
            total_amount=1000,
        )
        db_session.add_all([order_a, order_b])
        db_session.flush()

        shipment = Shipment(carrier="CJ_LOGISTICS", tracking_no="TRACK-MIX", status="READY")
        db_session.add(shipment)
        db_session.flush()
        db_session.add(ShipmentItem(shipment_id=shipment.id, order_id=order_a.id))
        db_session.add(ShipmentItem(shipment_id=shipment.id, order_id=order_b.id))
        db_session.flush()

        service = ShipmentDispatchService(db_session, connector_factory=_make_factory("accept"))

        with pytest.raises(ShipmentPlatformMismatchError):
            service.submit(shipment.id)
