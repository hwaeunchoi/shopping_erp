"""
tests/unit/test_shipment_dispatch_service.py
--------------------------------------------------
ShipmentDispatchService: 채널 전송의 idempotency/상태검증/부분성공-실패
격리/실패시 성공위장 금지/비동기 outbox 실행(enqueue/execute_command 분리)/
결과 불명(UNKNOWN) 분류/동시실행 방지(claim/lease)/분할배송(부분출고)/배송묶음
ID 라인단위 전달/기능 기본 OFF를 가짜(Fake) 커넥터로 검증한다(네트워크 없음).

실제 Naver/Coupang 커넥터의 submit_shipment() 자체(HTTP 요청 구성)는
tests/unit/test_naver_smartstore_connector.py / test_coupang_connector.py의
MockTransport 테스트에서 별도로 검증한다 - 이 파일은 서비스 계층만 검증한다.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from config.settings import settings
from integrations.malls.base_mall_connector import BaseMallConnector, ShipmentSubmitResult
from integrations.malls.errors import (
    MarketplaceCapabilityUnsupportedError,
    MarketplaceCredentialMissingError,
    MarketplaceExternalAPIError,
)
from models.extra import AuditLog
from models.order import Order, OrderItem, Shipment, ShipmentItem
from models.product import Product, ProductOption
from repositories.integration_sync_repository import ExternalCommandRepository, OrderStatusConflictRepository
from services.shipment_dispatch_service import (
    MAX_ATTEMPTS,
    ShipmentAlreadyRunningError,
    ShipmentBoxQuantityAmbiguousError,
    ShipmentChannelSubmitDisabledError,
    ShipmentDispatchService,
    ShipmentNotReadyError,
    ShipmentPlatformMismatchError,
    ShipmentSubmitRejectedError,
    _classify_write_outcome,
)


@pytest.fixture(autouse=True)
def _enable_channel_submit(monkeypatch):
    """이 파일은 디스패치 로직 자체를 검증하는 것이 목적이므로 기본적으로 기능을 켠다.

    TestFeatureFlagDefaultOff는 자체적으로 다시 False로 덮어써 기본 차단을 검증한다."""
    monkeypatch.setattr(settings, "shipment_channel_submit_enabled", True)


_ERROR_OUTCOMES = {
    # outcome 이름 -> (reason_code, retryable) - MarketplaceExternalAPIError 재현용.
    "rate_limited": ("RATE_LIMITED", True),  # SAFE_RETRY: 채널이 명시적으로 "아직 처리 안 함" 응답.
    "connect_failed": ("CONNECT_FAILED", True),  # SAFE_RETRY: 연결 자체가 성립되지 않음(미전송 확실).
    "auth_failed": ("AUTH_FAILED", False),  # CONFIRMED_FAILED: 인증 단계에서 확실히 거부.
    "timeout": ("TIMEOUT", True),  # UNKNOWN: 전송됐는지 알 수 없음.
    "server_error": ("SERVER_ERROR", True),  # UNKNOWN: 처리 후 응답 실패했을 수 있음.
}


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
        if self.outcome in _ERROR_OUTCOMES:
            reason, retryable = _ERROR_OUTCOMES[self.outcome]
            raise MarketplaceExternalAPIError("naver_smartstore", reason, retryable, http_status=500)
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
    """라인 N개짜리 주문 1건 - 분할배송/부분출고 테스트용(라인별 다른 배송묶음 ID 부여 가능)."""
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


class TestClassifyWriteOutcome:
    """예외 -> {SAFE_RETRY, CONFIRMED_FAILED, UNKNOWN} 분류 자체를 직접 검증한다 -
    이 매트릭스가 "결과 불명 요청을 자동 재전송하지 않는다"는 원칙의 핵심이다."""

    def test_connect_failed_is_safe_retry(self):
        assert _classify_write_outcome(MarketplaceExternalAPIError("naver", "CONNECT_FAILED", True)) == "SAFE_RETRY"

    def test_rate_limited_is_safe_retry(self):
        assert _classify_write_outcome(MarketplaceExternalAPIError("naver", "RATE_LIMITED", True)) == "SAFE_RETRY"

    def test_auth_failed_is_confirmed_failed(self):
        result = _classify_write_outcome(MarketplaceExternalAPIError("naver", "AUTH_FAILED", False))
        assert result == "CONFIRMED_FAILED"

    def test_timeout_is_unknown_not_retryable(self):
        """타임아웃은 채널이 실제로 처리했을 가능성을 배제할 수 없다 - RETRY_WAIT이 아니다."""
        assert _classify_write_outcome(MarketplaceExternalAPIError("naver", "TIMEOUT", True)) == "UNKNOWN"

    def test_server_error_is_unknown_not_retryable(self):
        assert _classify_write_outcome(MarketplaceExternalAPIError("naver", "SERVER_ERROR", True)) == "UNKNOWN"

    def test_transport_error_is_unknown(self):
        assert _classify_write_outcome(MarketplaceExternalAPIError("naver", "TRANSPORT_ERROR", True)) == "UNKNOWN"

    def test_parse_failed_is_unknown_not_failed(self):
        """응답 파싱 실패는 200 응답을 받은 뒤(외부 성공 후 로컬 확인 실패) 발생한다 -
        FAILED로 확정하면 안 된다(실제로는 채널이 이미 처리했을 수 있다)."""
        assert _classify_write_outcome(MarketplaceExternalAPIError("naver", "PARSE_FAILED", False)) == "UNKNOWN"

    def test_bad_response_is_unknown(self):
        assert _classify_write_outcome(MarketplaceExternalAPIError("naver", "BAD_RESPONSE", False)) == "UNKNOWN"

    def test_explicit_rejection_is_confirmed_failed(self):
        assert _classify_write_outcome(ShipmentSubmitRejectedError("naver", "DUPLICATE")) == "CONFIRMED_FAILED"

    def test_credential_missing_is_confirmed_failed(self):
        assert _classify_write_outcome(MarketplaceCredentialMissingError("naver")) == "CONFIRMED_FAILED"

    def test_capability_unsupported_is_confirmed_failed(self):
        assert _classify_write_outcome(MarketplaceCapabilityUnsupportedError("naver", "shipment_submit")) == (
            "CONFIRMED_FAILED"
        )

    def test_unexpected_exception_defaults_to_unknown(self):
        """분류표에 없는 예상 밖 예외는 안전한 기본값(UNKNOWN)으로 떨어진다."""
        assert _classify_write_outcome(RuntimeError("boom")) == "UNKNOWN"


class TestRetryPolicy:
    def test_safe_retry_error_goes_to_retry_wait_not_failed(self, db_session, platform):
        """채널이 요청 자체를 받지 못했음이 확실한 실패(SAFE_RETRY)만 자동 재시도(RETRY_WAIT)
        대상이 된다."""
        _, _, shipment = _seed_shippable_order(db_session, platform)
        service = ShipmentDispatchService(db_session, connector_factory=_make_factory("connect_failed"))

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

    def test_ambiguous_error_goes_to_unknown_not_retry_wait_or_failed(self, db_session, platform):
        """timeout처럼 채널이 처리했는지 알 수 없는 실패는 RETRY_WAIT(자동 재전송)도
        FAILED(확정 실패로 오인)도 아닌 UNKNOWN이어야 한다 - 중복 전송 위험 방지."""
        _, _, shipment = _seed_shippable_order(db_session, platform)
        service = ShipmentDispatchService(db_session, connector_factory=_make_factory("timeout"))

        with pytest.raises(MarketplaceExternalAPIError):
            service.submit(shipment.id)

        command = ExternalCommandRepository(db_session).get_by_idempotency_key(f"SHIPMENT_SUBMIT:{shipment.id}:TRACK-1")
        assert command is not None
        assert command.status == "UNKNOWN"
        assert command.retryable is False
        assert command.next_retry_at is None  # 자동 재시도 스케줄이 없다.

    def test_unknown_command_is_excluded_from_due_for_execution(self, db_session, platform):
        """UNKNOWN 명령은 outbox worker의 실행 대상 목록에서 자동으로 빠진다(자동
        재전송 금지가 실제로 지켜지는지 outbox 조회 레벨에서도 확인)."""
        _, _, shipment = _seed_shippable_order(db_session, platform)
        service = ShipmentDispatchService(db_session, connector_factory=_make_factory("timeout"))
        with pytest.raises(MarketplaceExternalAPIError):
            service.submit(shipment.id)

        due = ExternalCommandRepository(db_session).list_due_for_execution("SHIPMENT_SUBMIT")

        assert due == []

    def test_retries_exhausted_after_max_attempts_marks_failed(self, db_session, platform):
        """SAFE_RETRY 실패를 MAX_ATTEMPTS번 연속 거치면 더 이상 재시도하지 않고
        FAILED로 확정한다(무한 재시도 방지)."""
        _, _, shipment = _seed_shippable_order(db_session, platform)
        service = ShipmentDispatchService(db_session, connector_factory=_make_factory("rate_limited"))
        command_id = service.enqueue(shipment.id).command.id

        for _ in range(MAX_ATTEMPTS):
            with pytest.raises(MarketplaceExternalAPIError):
                service.execute_command(command_id)

        command = ExternalCommandRepository(db_session).get_by_id(command_id)
        assert command is not None
        assert command.attempt_count == MAX_ATTEMPTS
        assert command.status == "FAILED"

    def test_stale_running_is_recovered_to_unknown_not_pending(self, db_session, platform):
        """RUNNING으로 STALE_RUNNING_TIMEOUT_MINUTES 이상 머물러 있으면(worker 프로세스가
        실행 도중 죽었다고 추정) recover_stale_running()이 PENDING이 아니라 UNKNOWN으로
        회수한다 - 채널에 실제로 도달했는지 알 수 없는 채로 자동 재전송하면 안 된다."""
        _, _, shipment = _seed_shippable_order(db_session, platform)
        service = ShipmentDispatchService(db_session, connector_factory=_make_factory("accept"))
        command_id = service.enqueue(shipment.id).command.id
        repo = ExternalCommandRepository(db_session)
        assert repo.claim(command_id, "dead-worker-lease") is True
        command = repo.get_by_id(command_id)
        assert command is not None
        command.updated_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        db_session.flush()

        recovered = service.recover_stale_running("SHIPMENT_SUBMIT")

        assert recovered == 1
        assert command.status == "UNKNOWN"
        assert command.lease_token is None

    def test_fresh_running_is_not_recovered(self, db_session, platform):
        _, _, shipment = _seed_shippable_order(db_session, platform)
        service = ShipmentDispatchService(db_session, connector_factory=_make_factory("accept"))
        command_id = service.enqueue(shipment.id).command.id
        repo = ExternalCommandRepository(db_session)
        assert repo.claim(command_id, "fresh-lease") is True
        command = repo.get_by_id(command_id)
        assert command is not None

        recovered = service.recover_stale_running("SHIPMENT_SUBMIT")

        assert recovered == 0
        assert command.status == "RUNNING"

    def test_dead_worker_late_finalize_does_not_clobber_recovered_state(self, db_session, platform):
        """외부 전송 중 프로세스가 종료되는 경우: RUNNING을 UNKNOWN으로 회수한 뒤,
        원래 "죽었다"고 판단됐던 worker가 사실 아직 살아있어 뒤늦게 자신의 예전
        lease_token으로 결과를 쓰려 해도 무시돼야 한다(소유권 검증)."""
        _, _, shipment = _seed_shippable_order(db_session, platform)
        service = ShipmentDispatchService(db_session, connector_factory=_make_factory("accept"))
        command_id = service.enqueue(shipment.id).command.id
        repo = ExternalCommandRepository(db_session)
        repo.claim(command_id, "dead-worker-lease")
        command = repo.get_by_id(command_id)
        assert command is not None
        command.updated_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        db_session.flush()
        service.recover_stale_running("SHIPMENT_SUBMIT")
        assert command.status == "UNKNOWN"

        ok = repo.try_transition(command_id, "dead-worker-lease", status="SUCCESS")

        assert ok is False
        assert command.status == "UNKNOWN"  # 덮어써지지 않았다.


class TestConcurrentWorkerClaim:
    """worker 두 개가 같은 명령을 동시에 실행하지 못하도록 하는 원자적 claim을 검증한다."""

    def test_only_one_of_two_claims_succeeds(self, db_session, platform):
        _, _, shipment = _seed_shippable_order(db_session, platform)
        service = ShipmentDispatchService(db_session, connector_factory=_make_factory("accept"))
        command_id = service.enqueue(shipment.id).command.id
        repo = ExternalCommandRepository(db_session)

        claimed_by_worker_a = repo.claim(command_id, "lease-A")
        claimed_by_worker_b = repo.claim(command_id, "lease-B")

        assert claimed_by_worker_a is True
        assert claimed_by_worker_b is False
        command = repo.get_by_id(command_id)
        assert command is not None
        assert command.status == "RUNNING"
        assert command.lease_token == "lease-A"  # 두 번째 worker가 덮어쓰지 못했다.

    def test_execute_command_raises_when_another_worker_already_claimed(self, db_session, platform):
        """이미 다른 worker가 claim한(RUNNING) 명령에 대해 execute_command()를 호출하면
        겹쳐 실행하지 않고 즉시 거부해야 한다 - 커넥터는 호출되지 않는다."""
        _, _, shipment = _seed_shippable_order(db_session, platform)
        factory = _make_factory("accept")
        service = ShipmentDispatchService(db_session, connector_factory=factory)
        command_id = service.enqueue(shipment.id).command.id
        ExternalCommandRepository(db_session).claim(command_id, "other-worker-lease")

        with pytest.raises(ShipmentAlreadyRunningError):
            service.execute_command(command_id)

        assert factory.connector.calls == []


class TestResolveUnknownCommand:
    def _make_unknown_command(self, db_session, platform):
        _, _, shipment = _seed_shippable_order(db_session, platform)
        service = ShipmentDispatchService(db_session, connector_factory=_make_factory("timeout"))
        command_id = service.enqueue(shipment.id).command.id
        with pytest.raises(MarketplaceExternalAPIError):
            service.execute_command(command_id)
        command = ExternalCommandRepository(db_session).get_by_id(command_id)
        assert command is not None
        assert command.status == "UNKNOWN"
        return service, command_id

    def test_confirmed_not_sent_requeues_as_pending(self, db_session, platform):
        service, command_id = self._make_unknown_command(db_session, platform)

        resolved = service.resolve_unknown_command(command_id, "CONFIRMED_NOT_SENT")

        assert resolved.status == "PENDING"
        assert resolved.next_retry_at is None

    def test_confirmed_success_marks_success_without_resending(self, db_session, platform):
        service, command_id = self._make_unknown_command(db_session, platform)
        factory = service.connector_factory
        calls_before = len(factory.connector.calls)

        resolved = service.resolve_unknown_command(command_id, "CONFIRMED_SUCCESS")

        assert resolved.status == "SUCCESS"
        assert resolved.completed_at is not None
        assert len(factory.connector.calls) == calls_before  # 채널을 다시 호출하지 않았다.

    def test_confirmed_failed_marks_failed(self, db_session, platform):
        service, command_id = self._make_unknown_command(db_session, platform)

        resolved = service.resolve_unknown_command(command_id, "CONFIRMED_FAILED")

        assert resolved.status == "FAILED"
        assert resolved.retryable is False

    def test_cannot_resolve_non_unknown_command(self, db_session, platform):
        _, _, shipment = _seed_shippable_order(db_session, platform)
        service = ShipmentDispatchService(db_session, connector_factory=_make_factory("accept"))
        command_id = service.enqueue(shipment.id).command.id  # PENDING, UNKNOWN 아님.

        with pytest.raises(ValueError):
            service.resolve_unknown_command(command_id, "CONFIRMED_SUCCESS")

    def test_unknown_resolution_value_rejected(self, db_session, platform):
        service, command_id = self._make_unknown_command(db_session, platform)

        with pytest.raises(ValueError):
            service.resolve_unknown_command(command_id, "SOMETHING_ELSE")

    def test_records_audit_log(self, db_session, platform):
        service, command_id = self._make_unknown_command(db_session, platform)

        service.resolve_unknown_command(command_id, "CONFIRMED_FAILED", resolved_by=None)

        logs = db_session.query(AuditLog).filter_by(entity_type="EXTERNAL_COMMAND", entity_id=command_id).all()
        assert len(logs) == 1
        assert logs[0].command == "shipment_command.resolve_unknown"


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

    def test_partial_success_skips_already_succeeded_line_on_retry(self, db_session, naver_platform):
        """여러 라인을 전송하다 일부만 성공하면(라인1 성공, 라인2 거부) 성공한 라인은
        기록되고, 이후 재시도(운영자가 명령을 PENDING으로 되돌려 재실행)에서 라인1은
        다시 전송하지 않는다(중복 전송 방지)."""
        order, (item1, item2) = _seed_multi_item_order(db_session, naver_platform, order_no="PARTIAL-1")
        shipment = Shipment(carrier="CJ_LOGISTICS", tracking_no="TRACK-PARTIAL-1", status="READY")
        db_session.add(shipment)
        db_session.flush()
        db_session.add(ShipmentItem(shipment_id=shipment.id, order_id=order.id, order_item_id=None))
        db_session.flush()

        class _FirstLineOkSecondRejectedThenAccepted(_FakeConnector):
            def __init__(self):
                super().__init__()
                self.line2_attempts = 0

            def submit_shipment(self, platform_order_item_no, *a, **k):
                self.calls.append((platform_order_item_no, None, None, None))
                if platform_order_item_no == "PARTIAL-1-LINE2":
                    self.line2_attempts += 1
                    if self.line2_attempts == 1:
                        return ShipmentSubmitResult(accepted=False, platform_result_code="FAIL_CODE")
                return ShipmentSubmitResult(accepted=True, platform_result_code="OK")

        conn = _FirstLineOkSecondRejectedThenAccepted()

        def factory(connector_class, session, platform_id):
            return conn

        service = ShipmentDispatchService(db_session, connector_factory=factory)
        command_id = service.enqueue(shipment.id).command.id

        with pytest.raises(ShipmentSubmitRejectedError):
            service.execute_command(command_id)
        assert [c[0] for c in conn.calls] == ["PARTIAL-1-LINE1", "PARTIAL-1-LINE2"]

        # 운영자가 원인을 확인하고 다시 실행한다(같은 command_id) - 이미 성공한 라인1은
        # 다시 전송되지 않고, 라인2만 재시도된다(이번에는 채널이 수락).
        command = ExternalCommandRepository(db_session).get_by_id(command_id)
        assert command is not None
        command.status = "PENDING"
        db_session.flush()

        outcome = service.execute_command(command_id)

        assert outcome.command.status == "SUCCESS"
        assert [c[0] for c in conn.calls] == ["PARTIAL-1-LINE1", "PARTIAL-1-LINE2", "PARTIAL-1-LINE2"]


class TestBoxQuantityAmbiguousIsBlocked:
    def test_partial_quantity_on_box_tracked_item_is_blocked(self, db_session, platform):
        """쿠팡처럼 배송묶음(box) ID로만 추적되는 라인은 부분 수량 발송을 표현할 수
        없다 - 추측 대신 명시적으로 차단해야 한다(전송 자체가 일어나지 않아야 함)."""
        order, (item,) = _seed_multi_item_order(db_session, platform, order_no="PARTIAL-BOX", box_ids=("BOX-X",))
        item.quantity = 5
        db_session.flush()
        shipment = Shipment(carrier="CJ_LOGISTICS", tracking_no="TRACK-PARTIAL-BOX", status="READY")
        db_session.add(shipment)
        db_session.flush()
        db_session.add(ShipmentItem(shipment_id=shipment.id, order_id=order.id, order_item_id=item.id, quantity=2))
        db_session.flush()
        factory = _make_factory("accept")
        service = ShipmentDispatchService(db_session, connector_factory=factory)

        with pytest.raises(ShipmentBoxQuantityAmbiguousError):
            service.submit(shipment.id)

        assert factory.connector.calls == []  # 채널에 전송 자체가 나가지 않았다.
        command = ExternalCommandRepository(db_session).get_by_idempotency_key(
            f"SHIPMENT_SUBMIT:{shipment.id}:TRACK-PARTIAL-BOX"
        )
        assert command is not None
        assert command.status == "FAILED"  # 데이터 모델 한계로 명시적 차단 - 확실히 미전송.

    def test_partial_quantity_without_box_id_is_allowed(self, db_session, naver_platform):
        """배송묶음 개념이 없는 채널(네이버)은 부분 수량이어도 문제없이 전송된다."""
        order, (item,) = _seed_multi_item_order(db_session, naver_platform, order_no="PARTIAL-NOBOX", box_ids=(None,))
        item.quantity = 5
        db_session.flush()
        shipment = Shipment(carrier="CJ_LOGISTICS", tracking_no="TRACK-PARTIAL-NOBOX", status="READY")
        db_session.add(shipment)
        db_session.flush()
        db_session.add(ShipmentItem(shipment_id=shipment.id, order_id=order.id, order_item_id=item.id, quantity=2))
        db_session.flush()
        factory = _make_factory("accept")
        service = ShipmentDispatchService(db_session, connector_factory=factory)

        outcome = service.submit(shipment.id)

        assert outcome.command.status == "SUCCESS"
        assert len(factory.connector.calls) == 1


class TestOrderFullyDispatchedAggregate:
    def test_order_stays_not_shipping_until_all_split_quantity_confirmed(self, db_session, naver_platform):
        """같은 OrderItem 수량이 여러 Shipment로 나뉘는 경우 - 일부 수량만 발송됐으면
        주문 전체를 SHIPPING으로 전환하지 않고, 전체 수량이 이행돼야 전환한다."""
        order, (item,) = _seed_multi_item_order(db_session, naver_platform, order_no="QTY-SPLIT", box_ids=(None,))
        item.quantity = 5
        db_session.flush()
        shipment1 = Shipment(carrier="CJ_LOGISTICS", tracking_no="TRACK-QTY-1", status="READY")
        shipment2 = Shipment(carrier="CJ_LOGISTICS", tracking_no="TRACK-QTY-2", status="READY")
        db_session.add_all([shipment1, shipment2])
        db_session.flush()
        db_session.add(ShipmentItem(shipment_id=shipment1.id, order_id=order.id, order_item_id=item.id, quantity=2))
        db_session.add(ShipmentItem(shipment_id=shipment2.id, order_id=order.id, order_item_id=item.id, quantity=3))
        db_session.flush()

        factory = _make_factory("accept")
        service = ShipmentDispatchService(db_session, connector_factory=factory)

        service.submit(shipment1.id)
        assert order.status != "SHIPPING"  # 2/5만 발송됨 - 아직 전체 이행 아님(부분출고 상태 유지).

        service.submit(shipment2.id)
        assert order.status == "SHIPPING"  # 2+3=5, 전체 이행 완료.


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


class TestFeatureFlagDefaultOff:
    """실전송 기본 차단: settings.shipment_channel_submit_enabled가 False이면 enqueue()
    자체가 즉시 차단되고, PENDING 명령도 만들어지지 않으며, 커넥터는 전혀 호출되지 않는다."""

    def test_enqueue_blocked_when_disabled(self, db_session, platform, monkeypatch):
        monkeypatch.setattr(settings, "shipment_channel_submit_enabled", False)
        _, _, shipment = _seed_shippable_order(db_session, platform)
        factory = _make_factory("accept")
        service = ShipmentDispatchService(db_session, connector_factory=factory)

        with pytest.raises(ShipmentChannelSubmitDisabledError):
            service.enqueue(shipment.id)

        assert factory.connector.calls == []
        command = ExternalCommandRepository(db_session).get_by_idempotency_key(f"SHIPMENT_SUBMIT:{shipment.id}:TRACK-1")
        assert command is None  # PENDING 명령 자체가 생성되지 않는다.

    def test_submit_convenience_also_blocked(self, db_session, platform, monkeypatch):
        monkeypatch.setattr(settings, "shipment_channel_submit_enabled", False)
        _, _, shipment = _seed_shippable_order(db_session, platform)
        factory = _make_factory("accept")
        service = ShipmentDispatchService(db_session, connector_factory=factory)

        with pytest.raises(ShipmentChannelSubmitDisabledError):
            service.submit(shipment.id)

        assert factory.connector.calls == []
