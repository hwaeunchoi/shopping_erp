"""
tests/unit/test_shipment_dispatch_service.py
--------------------------------------------------
ShipmentDispatchService: 채널 전송의 idempotency/상태검증/부분성공-실패
격리/실패시 성공위장 금지/비동기 outbox 실행(enqueue/execute_command 분리)/
재시도 상태전이/분할배송(부분출고)/배송묶음 ID 라인단위 전달을 가짜(Fake)
커넥터로 검증한다(네트워크 없음).

실제 Naver/Coupang 커넥터의 submit_shipment() 자체(HTTP 요청 구성)는
tests/unit/test_naver_smartstore_connector.py / test_coupang_connector.py의
MockTransport 테스트에서 별도로 검증한다 - 이 파일은 서비스 계층(outbox/
idempotency/트랜잭션 격리/재시도/분할배송)만 검증한다.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from integrations.malls.base_mall_connector import BaseMallConnector, ShipmentSubmitResult
from integrations.malls.errors import MarketplaceCapabilityUnsupportedError, MarketplaceExternalAPIError
from models.order import Order, OrderItem, Shipment, ShipmentItem
from models.product import Product, ProductOption
from repositories.integration_sync_repository import ExternalCommandRepository, OrderStatusConflictRepository
from services.shipment_dispatch_service import (
    MAX_ATTEMPTS,
    ShipmentAlreadyRunningError,
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
        self.calls.append((platform_order_item_no, carrier_code, tracking_no, platform_shipment_box_id))
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


def _seed_shippable_order(
    db_session, platform, poin="PO-1", carrier="CJ_LOGISTICS", tracking_no="TRACK-1", status="PREPARING", box_id=None
):
    product = Product(name="테스트 상품", category="테스트", base_price=10000, status="ACTIVE")
    db_session.add(product)
    db_session.flush()
    option = ProductOption(product_id=product.id, sku_code=f"SKU-{poin}", is_active=True)
    db_session.add(option)
    db_session.flush()

    order = Order(
        platform_id=platform.id,
        platform_order_no=f"ORDER-{poin}",
        status=status,
        order_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        total_amount=10000,
    )
    db_session.add(order)
    db_session.flush()
    item = OrderItem(
        order_id=order.id,
        product_option_id=option.id,
        platform_order_item_no=poin,
        platform_shipment_box_id=box_id,
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


def _seed_multi_item_order(db_session, platform, order_no="MULTI-1", box_ids=(None, None)):
    """라인 2개짜리 주문 1건 - 분할배송/부분출고 테스트용(라인별 다른 배송묶음 ID 부여 가능)."""
    product = Product(name="테스트 상품", category="테스트", base_price=10000, status="ACTIVE")
    db_session.add(product)
    db_session.flush()

    order = Order(
        platform_id=platform.id,
        platform_order_no=order_no,
        status="PREPARING",
        order_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        total_amount=20000,
    )
    db_session.add(order)
    db_session.flush()

    items = []
    for i, box_id in enumerate(box_ids, start=1):
        option = ProductOption(product_id=product.id, sku_code=f"SKU-{order_no}-{i}", is_active=True)
        db_session.add(option)
        db_session.flush()
        item = OrderItem(
            order_id=order.id,
            product_option_id=option.id,
            platform_order_item_no=f"{order_no}-LINE{i}",
            platform_shipment_box_id=box_id,
            quantity=1,
            unit_price=10000,
            line_amount=10000,
        )
        db_session.add(item)
        db_session.flush()
        items.append(item)
    return order, items


class TestSuccessfulSingleSubmit:
    def test_accepted_marks_command_success(self, db_session, platform):
        _, _, shipment = _seed_shippable_order(db_session, platform)
        factory = _make_factory("accept")
        service = ShipmentDispatchService(db_session, connector_factory=factory)

        outcome = service.submit(shipment.id, dispatch_date=date(2026, 1, 2))

        assert outcome.command.status == "SUCCESS"
        assert outcome.already_processed is False
        assert factory.connector.calls == [("PO-1", "CJGLS", "TRACK-1", None)]

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

    def test_double_enqueue_before_execution_reuses_same_pending_command(self, db_session, platform):
        """버튼 연타 방지 - execute_command()가 아직 실행되기 전에 enqueue()를 두 번
        호출해도 같은 PENDING 명령을 재사용한다(새 행이 중복 생성되지 않는다)."""
        _, _, shipment = _seed_shippable_order(db_session, platform)
        factory = _make_factory("accept")
        service = ShipmentDispatchService(db_session, connector_factory=factory)

        first = service.enqueue(shipment.id)
        second = service.enqueue(shipment.id)

        assert first.command.id == second.command.id
        assert first.command.status == "PENDING"
        assert first.already_processed is False
        assert second.already_processed is False
        assert factory.connector.calls == []  # enqueue()는 채널을 호출하지 않는다.

    def test_different_shipments_with_same_tracking_no_do_not_collide(self, db_session, platform):
        """서로 다른 배송 건이 우연히 같은 송장번호를 쓰더라도(오입력 등) idempotency_key에
        shipment_id가 포함되어 있어 서로 다른 명령으로 분리된다(한쪽이 다른 쪽 결과에
        영향받지 않음)."""
        _, _, shipment_a = _seed_shippable_order(db_session, platform, poin="PO-A", tracking_no="SAME-TRACK")
        _, _, shipment_b = _seed_shippable_order(db_session, platform, poin="PO-B", tracking_no="SAME-TRACK")
        service = ShipmentDispatchService(db_session, connector_factory=_make_factory("accept"))

        outcome_a = service.submit(shipment_a.id)
        outcome_b = service.submit(shipment_b.id)

        assert outcome_a.command.id != outcome_b.command.id
        assert outcome_a.command.status == "SUCCESS"
        assert outcome_b.command.status == "SUCCESS"


class TestAsyncOutboxExecution:
    def test_enqueue_creates_pending_command_without_calling_connector(self, db_session, platform):
        _, _, shipment = _seed_shippable_order(db_session, platform)
        factory = _make_factory("accept")
        service = ShipmentDispatchService(db_session, connector_factory=factory)

        outcome = service.enqueue(shipment.id)

        assert outcome.command.status == "PENDING"
        assert outcome.command.attempt_count == 0
        assert factory.connector.calls == []

    def test_execute_command_on_pending_calls_connector_and_marks_success(self, db_session, platform):
        _, _, shipment = _seed_shippable_order(db_session, platform)
        factory = _make_factory("accept")
        service = ShipmentDispatchService(db_session, connector_factory=factory)
        command_id = service.enqueue(shipment.id).command.id

        outcome = service.execute_command(command_id)

        assert outcome.command.status == "SUCCESS"
        assert len(factory.connector.calls) == 1

    def test_execute_command_on_running_raises(self, db_session, platform):
        _, _, shipment = _seed_shippable_order(db_session, platform)
        service = ShipmentDispatchService(db_session, connector_factory=_make_factory("accept"))
        command_id = service.enqueue(shipment.id).command.id
        command = ExternalCommandRepository(db_session).get_by_id(command_id)
        assert command is not None
        command.status = "RUNNING"
        db_session.flush()

        with pytest.raises(ShipmentAlreadyRunningError):
            service.execute_command(command_id)


class TestRejectionIsNotShownAsSuccess:
    def test_platform_rejection_marks_command_failed_and_raises(self, db_session, platform):
        _, _, shipment = _seed_shippable_order(db_session, platform)
        service = ShipmentDispatchService(db_session, connector_factory=_make_factory("reject"))

        with pytest.raises(ShipmentSubmitRejectedError):
            service.submit(shipment.id)

        command = ExternalCommandRepository(db_session).get_by_idempotency_key(f"SHIPMENT_SUBMIT:{shipment.id}:TRACK-1")
        assert command is not None
        assert command.status == "FAILED"
        assert command.retryable is False


class TestRetryPolicy:
    def test_retryable_external_error_goes_to_retry_wait_not_failed(self, db_session, platform):
        """재시도 가능한 실패는 즉시 FAILED로 확정하지 않고 RETRY_WAIT + next_retry_at을
        채워 다음 주기의 outbox worker가 다시 시도하게 한다."""
        _, _, shipment = _seed_shippable_order(db_session, platform)
        service = ShipmentDispatchService(db_session, connector_factory=_make_factory("external_error"))

        before = datetime.now(timezone.utc).replace(tzinfo=None)  # SQLite DateTime은 naive로 왕복된다.
        with pytest.raises(MarketplaceExternalAPIError):
            service.submit(shipment.id)

        command = ExternalCommandRepository(db_session).get_by_idempotency_key(f"SHIPMENT_SUBMIT:{shipment.id}:TRACK-1")
        assert command is not None
        assert command.status == "RETRY_WAIT"
        assert command.retryable is True
        assert command.attempt_count == 1
        assert command.next_retry_at is not None
        assert command.next_retry_at > before

    def test_retries_exhausted_after_max_attempts_marks_failed(self, db_session, platform):
        """MAX_ATTEMPTS번 연속 재시도 가능한 실패를 거치면 더 이상 재시도하지 않고
        FAILED로 확정한다(무한 재시도 방지)."""
        _, _, shipment = _seed_shippable_order(db_session, platform)
        service = ShipmentDispatchService(db_session, connector_factory=_make_factory("external_error"))
        command_id = service.enqueue(shipment.id).command.id

        for _ in range(MAX_ATTEMPTS):
            with pytest.raises(MarketplaceExternalAPIError):
                service.execute_command(command_id)

        command = ExternalCommandRepository(db_session).get_by_id(command_id)
        assert command is not None
        assert command.attempt_count == MAX_ATTEMPTS
        assert command.status == "FAILED"

    def test_stale_running_is_recovered_to_pending(self, db_session, platform):
        """RUNNING으로 STALE_RUNNING_TIMEOUT_MINUTES 이상 머물러 있으면(worker가 실행
        도중 죽었다고 추정) recover_stale_running()이 PENDING으로 되돌려 다음 실행에서
        다시 시도되게 한다."""
        _, _, shipment = _seed_shippable_order(db_session, platform)
        service = ShipmentDispatchService(db_session, connector_factory=_make_factory("accept"))
        command_id = service.enqueue(shipment.id).command.id
        command = ExternalCommandRepository(db_session).get_by_id(command_id)
        assert command is not None
        command.status = "RUNNING"
        db_session.flush()
        command.updated_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        db_session.flush()

        recovered = service.recover_stale_running("SHIPMENT_SUBMIT")

        assert recovered == 1
        assert command.status == "PENDING"

    def test_fresh_running_is_not_recovered(self, db_session, platform):
        _, _, shipment = _seed_shippable_order(db_session, platform)
        service = ShipmentDispatchService(db_session, connector_factory=_make_factory("accept"))
        command_id = service.enqueue(shipment.id).command.id
        command = ExternalCommandRepository(db_session).get_by_id(command_id)
        assert command is not None
        command.status = "RUNNING"
        db_session.flush()

        recovered = service.recover_stale_running("SHIPMENT_SUBMIT")

        assert recovered == 0
        assert command.status == "RUNNING"


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

        command = ExternalCommandRepository(db_session).get_by_idempotency_key(f"SHIPMENT_SUBMIT:{shipment.id}:TRACK-1")
        assert command is not None
        assert command.status == "FAILED"  # 미지원은 사람의 조치가 필요 - 자동 재시도 대상이 아니다.


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


class TestSplitShipmentAndPartialFulfillment:
    """분할배송(한 주문을 여러 배송으로 나눠 보냄)/부분출고(배송 하나가 주문의 일부
    라인만 담당) 시나리오 - Shipment는 shipment_items를 통해 Order/OrderItem과
    N:M으로 연결되며, 분할배송/부분출고 모두 이 구조로 이미 표현 가능하다(모델
    변경 불필요). 여기서는 ShipmentDispatchService가 각 배송에 대해 실제로
    올바른 라인만 전송하는지 검증한다."""

    def test_one_order_two_shipments_each_sends_only_its_own_line(self, db_session, naver_platform):
        """한 주문/복수 배송(부분출고): 주문 1건의 라인 2개를 배송 2건으로 나눠 보내면,
        각 배송은 자신에게 연결된(order_item_id) 라인만 전송해야 한다 - 다른 배송의
        라인까지 함께 보내면 안 된다."""
        order, (item1, item2) = _seed_multi_item_order(db_session, naver_platform, order_no="SPLIT-1")

        shipment1 = Shipment(carrier="CJ_LOGISTICS", tracking_no="TRACK-SPLIT-1", status="READY")
        shipment2 = Shipment(carrier="CJ_LOGISTICS", tracking_no="TRACK-SPLIT-2", status="READY")
        db_session.add_all([shipment1, shipment2])
        db_session.flush()
        db_session.add(ShipmentItem(shipment_id=shipment1.id, order_id=order.id, order_item_id=item1.id))
        db_session.add(ShipmentItem(shipment_id=shipment2.id, order_id=order.id, order_item_id=item2.id))
        db_session.flush()

        factory = _make_factory("accept")
        service = ShipmentDispatchService(db_session, connector_factory=factory)

        service.submit(shipment1.id)
        sent_after_first = [c[0] for c in factory.connector.calls]
        service.submit(shipment2.id)
        sent_after_second = [c[0] for c in factory.connector.calls]

        assert sent_after_first == ["SPLIT-1-LINE1"]  # 첫 배송은 자기 라인만.
        assert sent_after_second == ["SPLIT-1-LINE1", "SPLIT-1-LINE2"]  # 두 번째 배송이 나머지 라인 전송.

    def test_coupang_multiple_shipment_boxes_send_correct_box_id_per_line(self, db_session, platform):
        """쿠팡 복수 shipmentBoxId: 한 주문의 두 라인이 서로 다른 배송묶음(box)에서 왔다면
        (분할배송으로 수집됨) 각 라인은 자신이 실제로 속한 box id로 전송돼야 한다 -
        주문 전체 대표값 하나를 두 라인 모두에 잘못 적용하면 안 된다."""
        order, (item1, item2) = _seed_multi_item_order(
            db_session, platform, order_no="BOX-1", box_ids=("BOX-AAA", "BOX-BBB")
        )
        shipment1 = Shipment(carrier="CJ_LOGISTICS", tracking_no="TRACK-BOX-1", status="READY")
        shipment2 = Shipment(carrier="CJ_LOGISTICS", tracking_no="TRACK-BOX-2", status="READY")
        db_session.add_all([shipment1, shipment2])
        db_session.flush()
        db_session.add(ShipmentItem(shipment_id=shipment1.id, order_id=order.id, order_item_id=item1.id))
        db_session.add(ShipmentItem(shipment_id=shipment2.id, order_id=order.id, order_item_id=item2.id))
        db_session.flush()

        factory = _make_factory("accept")
        service = ShipmentDispatchService(db_session, connector_factory=factory)

        service.submit(shipment1.id)
        service.submit(shipment2.id)

        box_ids_sent = {call[0]: call[3] for call in factory.connector.calls}
        assert box_ids_sent["BOX-1-LINE1"] == "BOX-AAA"
        assert box_ids_sent["BOX-1-LINE2"] == "BOX-BBB"

    def test_whole_order_shipment_sends_all_lines(self, db_session, naver_platform):
        """order_item_id가 없는(주문 전체) shipment_item은 그 주문의 라인 전체를 대상으로
        한다(기존 합포장/단일배송 동작 회귀 확인)."""
        order, (item1, item2) = _seed_multi_item_order(db_session, naver_platform, order_no="WHOLE-1")
        shipment = Shipment(carrier="CJ_LOGISTICS", tracking_no="TRACK-WHOLE", status="READY")
        db_session.add(shipment)
        db_session.flush()
        db_session.add(ShipmentItem(shipment_id=shipment.id, order_id=order.id, order_item_id=None))
        db_session.flush()

        factory = _make_factory("accept")
        service = ShipmentDispatchService(db_session, connector_factory=factory)
        service.submit(shipment.id)

        sent_ids = {c[0] for c in factory.connector.calls}
        assert sent_ids == {"WHOLE-1-LINE1", "WHOLE-1-LINE2"}


class TestChannelStatusSyncAfterSuccess:
    def test_successful_submit_applies_shipping_status_via_state_machine(self, db_session, platform):
        """송장 전송 성공 후, 허용된 전이(PREPARING -> SHIPPING)면 자동으로 반영된다."""
        order, _, shipment = _seed_shippable_order(db_session, platform, status="PREPARING")
        service = ShipmentDispatchService(db_session, connector_factory=_make_factory("accept"))

        service.submit(shipment.id)

        assert order.status == "SHIPPING"

    def test_successful_submit_with_disallowed_transition_creates_conflict_not_overwrite(self, db_session, platform):
        """허용되지 않는 전이(예: 이미 CANCELED)면 자동으로 덮어쓰지 않고 충돌로 남긴다."""
        order, _, shipment = _seed_shippable_order(db_session, platform, status="CANCELED")
        # CANCELED 상태에서는 정상적으로 shipment READY 검증이 통과하지 않을 수 있으나,
        # 이 테스트는 주문 상태 반영 로직만 확인하는 것이 목적이라 shipment 자체는 READY로 둔다.
        service = ShipmentDispatchService(db_session, connector_factory=_make_factory("accept"))

        service.submit(shipment.id)

        assert order.status == "CANCELED"  # 자동으로 덮어쓰지 않는다.
        conflict = OrderStatusConflictRepository(db_session).get_unresolved_for_status(order.id, "SHIPPING")
        assert conflict is not None
        assert conflict.internal_status == "CANCELED"

    def test_repeated_conflict_does_not_duplicate(self, db_session, platform):
        """같은 배송을 재시도(idempotent라 실제로는 채널을 다시 부르지 않지만, 충돌
        해소 로직 자체의 중복 방지 여부를 sync_channel_status 직접 호출로 확인한다)."""
        order, _, shipment = _seed_shippable_order(db_session, platform, status="CANCELED")
        service = ShipmentDispatchService(db_session, connector_factory=_make_factory("accept"))

        service.submit(shipment.id)
        # 같은 주문 상태 불일치를 다시 감지해도(예: 다음 재조회 주기) 중복 행을 만들지 않는다.
        service.channel_sync_service.sync_channel_status(order, "SHIPPING")

        repo = OrderStatusConflictRepository(db_session)
        all_unresolved = repo.list_unresolved(order_id=order.id)
        assert len(all_unresolved) == 1
