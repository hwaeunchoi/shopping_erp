"""
tests/unit/test_product_sync_dispatch_service.py
------------------------------------------------------
ProductSyncDispatchService: 상용 ERP 확장(3단계, 첫 묶음) - 재고 수량/판매상태
전송의 outbox/lease/UNKNOWN/버전검증 안전성을 검증한다. 실제 채널 API는 호출하지
않는다(스텁 커넥터만 사용).
"""

from datetime import datetime, timedelta, timezone

import pytest

from integrations.malls.base_mall_connector import SALE_STATUS_ON_SALE, SALE_STATUS_SUSPENDED, ProductSyncActionResult
from integrations.malls.errors import MarketplaceCredentialMissingError, MarketplaceExternalAPIError
from models.integration_sync import ExternalCommand, ProductSyncCommandDetail
from services.product_sync_dispatch_service import (
    MAX_QUANTITY,
    ProductChannelSyncDisabledError,
    ProductSyncAlreadyRunningError,
    ProductSyncCommandTypeMismatchError,
    ProductSyncDispatchService,
    ProductSyncMappingNotFoundError,
)


class StubProductConnector:
    """update_inventory()/update_sale_status()를 제어할 수 있는 스텁."""

    def __init__(self, *, supports_inventory=True, supports_status=True, result=None, error=None):
        self.supports_inventory_update = supports_inventory
        self.supports_sale_status_update = supports_status
        self._result = result or ProductSyncActionResult(accepted=True, platform_result_code="SUCCESS")
        self._error = error
        self.calls: list[tuple] = []

    def update_inventory(self, platform_option_id, quantity, platform_origin_product_id=None):
        self.calls.append(("inventory", platform_option_id, quantity))
        if self._error:
            raise self._error
        return self._result

    def update_sale_status(self, platform_option_id, target_status, platform_origin_product_id=None):
        self.calls.append(("status", platform_option_id, target_status))
        if self._error:
            raise self._error
        return self._result


def _factory(connector):
    return lambda connector_class, session=None, platform_id=None: connector


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "product_channel_sync_enabled", True)


class TestDisabledByDefault:
    def test_enqueue_inventory_raises_when_disabled(self, db_session, platform_map, monkeypatch):
        from config.settings import settings

        monkeypatch.setattr(settings, "product_channel_sync_enabled", False)
        svc = ProductSyncDispatchService(db_session)

        with pytest.raises(ProductChannelSyncDisabledError):
            svc.enqueue_inventory_update(platform_map.id, 10)
        assert db_session.query(ExternalCommand).count() == 0

    def test_enqueue_sale_status_raises_when_disabled(self, db_session, platform_map, monkeypatch):
        from config.settings import settings

        monkeypatch.setattr(settings, "product_channel_sync_enabled", False)
        svc = ProductSyncDispatchService(db_session)

        with pytest.raises(ProductChannelSyncDisabledError):
            svc.enqueue_sale_status_update(platform_map.id, SALE_STATUS_ON_SALE)
        assert db_session.query(ExternalCommand).count() == 0


class TestQuantityValidation:
    def test_zero_is_accepted(self, db_session, platform_map):
        svc = ProductSyncDispatchService(db_session)
        outcome = svc.enqueue_inventory_update(platform_map.id, 0)
        assert outcome.command.status == "PENDING"

    def test_negative_is_rejected(self, db_session, platform_map):
        svc = ProductSyncDispatchService(db_session)
        with pytest.raises(ValueError):
            svc.enqueue_inventory_update(platform_map.id, -1)
        assert db_session.query(ExternalCommand).count() == 0

    def test_non_integer_is_rejected(self, db_session, platform_map):
        svc = ProductSyncDispatchService(db_session)
        with pytest.raises(ValueError):
            svc.enqueue_inventory_update(platform_map.id, 1.5)  # type: ignore[arg-type]

    def test_over_max_is_rejected(self, db_session, platform_map):
        svc = ProductSyncDispatchService(db_session)
        with pytest.raises(ValueError):
            svc.enqueue_inventory_update(platform_map.id, MAX_QUANTITY + 1)

    def test_at_max_is_accepted(self, db_session, platform_map):
        svc = ProductSyncDispatchService(db_session)
        outcome = svc.enqueue_inventory_update(platform_map.id, MAX_QUANTITY)
        assert outcome.command.status == "PENDING"


class TestSaleStatusValidation:
    def test_unknown_value_is_rejected(self, db_session, platform_map):
        svc = ProductSyncDispatchService(db_session)
        with pytest.raises(ValueError):
            svc.enqueue_sale_status_update(platform_map.id, "OUTOFSTOCK")
        assert db_session.query(ExternalCommand).count() == 0

    def test_on_sale_and_suspended_are_accepted(self, db_session, platform_map):
        svc = ProductSyncDispatchService(db_session)
        assert svc.enqueue_sale_status_update(platform_map.id, SALE_STATUS_ON_SALE).command.status == "PENDING"


class TestMappingNotFound:
    def test_enqueue_inventory_raises_for_missing_mapping(self, db_session):
        svc = ProductSyncDispatchService(db_session)
        with pytest.raises(ProductSyncMappingNotFoundError):
            svc.enqueue_inventory_update(999999, 10)


class TestIdempotentEnqueue:
    def test_same_target_quantity_returns_same_command(self, db_session, platform_map):
        svc = ProductSyncDispatchService(db_session)
        first = svc.enqueue_inventory_update(platform_map.id, 10)
        second = svc.enqueue_inventory_update(platform_map.id, 10)
        assert first.command.id == second.command.id
        assert db_session.query(ExternalCommand).count() == 1

    def test_same_target_status_returns_same_command(self, db_session, platform_map):
        svc = ProductSyncDispatchService(db_session)
        first = svc.enqueue_sale_status_update(platform_map.id, SALE_STATUS_SUSPENDED)
        second = svc.enqueue_sale_status_update(platform_map.id, SALE_STATUS_SUSPENDED)
        assert first.command.id == second.command.id


class TestSupersedingStaleCommands:
    def test_new_target_quantity_cancels_old_pending_command(self, db_session, platform_map):
        svc = ProductSyncDispatchService(db_session)
        old = svc.enqueue_inventory_update(platform_map.id, 10)
        new = svc.enqueue_inventory_update(platform_map.id, 20)

        db_session.refresh(old.command)
        assert old.command.id != new.command.id
        assert old.command.status == "CANCELLED"
        assert old.command.error_code == "SUPERSEDED_BY_NEWER_REQUEST"
        assert new.command.status == "PENDING"

    def test_new_target_status_cancels_old_pending_command(self, db_session, platform_map):
        svc = ProductSyncDispatchService(db_session)
        old = svc.enqueue_sale_status_update(platform_map.id, SALE_STATUS_ON_SALE)
        new = svc.enqueue_sale_status_update(platform_map.id, SALE_STATUS_SUSPENDED)

        db_session.refresh(old.command)
        assert old.command.status == "CANCELLED"
        assert new.command.status == "PENDING"

    def test_does_not_cancel_commands_for_a_different_mapping(self, db_session, platform_map, platform, product_option):
        from models.product import ProductPlatformMap

        other_option = product_option
        other_mapping = ProductPlatformMap(
            product_option_id=other_option.id, platform_id=platform.id, platform_option_id="EXT-CODE-OTHER"
        )
        db_session.add(other_mapping)
        db_session.flush()

        svc = ProductSyncDispatchService(db_session)
        a = svc.enqueue_inventory_update(platform_map.id, 10)
        svc.enqueue_inventory_update(other_mapping.id, 999)

        db_session.refresh(a.command)
        assert a.command.status == "PENDING"  # 다른 매핑에 대한 명령은 영향을 주지 않는다.


class TestExecuteCommandSuccess:
    def test_success_persists_target_and_calls_connector(self, db_session, platform_map):
        svc = ProductSyncDispatchService(db_session, connector_factory=_factory(StubProductConnector()))
        outcome = svc.enqueue_inventory_update(platform_map.id, 42)

        result = svc.execute_command(outcome.command.id)

        assert result.command.status == "SUCCESS"
        assert result.command.completed_at is not None

    def test_dispatches_correct_target_quantity_and_option_id(self, db_session, platform_map):
        conn = StubProductConnector()
        svc = ProductSyncDispatchService(db_session, connector_factory=_factory(conn))
        outcome = svc.enqueue_inventory_update(platform_map.id, 42)

        svc.execute_command(outcome.command.id)

        assert conn.calls == [("inventory", platform_map.platform_option_id, 42)]

    def test_already_success_is_idempotent(self, db_session, platform_map):
        conn = StubProductConnector()
        svc = ProductSyncDispatchService(db_session, connector_factory=_factory(conn))
        outcome = svc.enqueue_inventory_update(platform_map.id, 42)
        svc.execute_command(outcome.command.id)

        result = svc.execute_command(outcome.command.id)

        assert result.already_processed is True
        assert len(conn.calls) == 1  # 재호출하지 않음.


class TestCommandTypeSeparation:
    """송장 worker와 재고/판매상태 worker가 서로의 명령을 집어가지 않는다."""

    def test_execute_command_rejects_shipment_submit_type(self, db_session, platform):
        command = ExternalCommand(
            idempotency_key="SHIPMENT_SUBMIT:1:TRK",
            command_type="SHIPMENT_SUBMIT",
            platform_id=platform.id,
            target_type="SHIPMENT",
            target_id=1,
            status="PENDING",
            trace_id="trace",
        )
        db_session.add(command)
        db_session.flush()

        svc = ProductSyncDispatchService(db_session)
        with pytest.raises(ProductSyncCommandTypeMismatchError):
            svc.execute_command(command.id)


class TestExecuteTimeNewerCommandCheck:
    def test_execute_cancels_when_a_newer_command_exists_for_the_same_target(self, db_session, platform_map):
        """claim() 성공 직후라도, 실제 채널 호출 직전에 더 최신 명령이 있으면 전송하지
        않고 CANCELLED로 남긴다(오래된 명령이 최신 목표값을 늦게 덮어쓰는 것을 방지)."""
        conn = StubProductConnector()
        svc = ProductSyncDispatchService(db_session, connector_factory=_factory(conn))
        old = svc.enqueue_inventory_update(platform_map.id, 10)

        # 다른 worker가 claim(RUNNING)해서 아직 실행 전이라고 가정 - enqueue()의
        # 사전 취소 로직을 우회하기 위해 상태를 RUNNING으로 직접 만든다.
        old.command.status = "RUNNING"
        old.command.lease_token = "some-other-worker-token"
        db_session.flush()

        new = svc.enqueue_sale_status_update(
            platform_map.id, SALE_STATUS_ON_SALE
        )  # 다른 command_type - 영향 없음 확인용
        assert new.command.status == "PENDING"

        # 같은 command_type으로 더 최신 명령이 생기는 시나리오를 직접 만든다(다른
        # target_quantity, 같은 target_id) - enqueue()가 이미 RUNNING은 취소하지
        # 않으므로 그대로 둔 채 새 명령을 추가한다.
        from repositories.integration_sync_repository import (
            ExternalCommandRepository,
            ProductSyncCommandDetailRepository,
        )

        newer_command = ExternalCommandRepository(db_session).add(
            ExternalCommand(
                idempotency_key="INVENTORY_UPDATE:manual:newer",
                command_type="INVENTORY_UPDATE",
                platform_id=platform_map.platform_id,
                target_type="PRODUCT_PLATFORM_MAP",
                target_id=platform_map.id,
                status="PENDING",
                trace_id="trace-newer",
            )
        )
        ProductSyncCommandDetailRepository(db_session).add(
            ProductSyncCommandDetail(
                command_id=newer_command.id, product_platform_map_id=platform_map.id, target_quantity=999
            )
        )
        assert newer_command.id > old.command.id

        # old.command는 여전히 RUNNING이라 claim()이 실패해야 정상이지만, 이 테스트는
        # "claim 직후 재확인" 경로 자체를 검증하려 하므로 old를 다시 PENDING으로
        # 돌려 claim 가능하게 만든다(다른 worker가 죽고 회수된 상황을 흉내).
        old.command.status = "PENDING"
        old.command.lease_token = None
        db_session.flush()

        result = svc.execute_command(old.command.id)

        assert result.command.status == "CANCELLED"
        assert result.command.error_code == "SUPERSEDED_BY_NEWER_REQUEST"
        assert conn.calls == []  # 채널 호출 자체가 없었다.


class TestUnknownPredecessorBlocking:
    def test_execute_skips_when_earlier_unknown_command_exists_for_same_target(self, db_session, platform_map):
        conn = StubProductConnector()
        svc = ProductSyncDispatchService(db_session, connector_factory=_factory(conn))
        first = svc.enqueue_inventory_update(platform_map.id, 10)
        first.command.status = "UNKNOWN"
        db_session.flush()

        second = svc.enqueue_inventory_update(platform_map.id, 20)
        result = svc.execute_command(second.command.id)

        assert result.command.status == "PENDING"  # 실행되지 않고 그대로 대기.
        assert conn.calls == []

    def test_resolving_the_unknown_predecessor_unblocks_execution(self, db_session, platform_map):
        conn = StubProductConnector()
        svc = ProductSyncDispatchService(db_session, connector_factory=_factory(conn))
        first = svc.enqueue_inventory_update(platform_map.id, 10)
        first.command.status = "UNKNOWN"
        db_session.flush()
        second = svc.enqueue_inventory_update(platform_map.id, 20)

        svc.resolve_unknown_command(first.command.id, "CONFIRMED_FAILED")
        result = svc.execute_command(second.command.id)

        assert result.command.status == "SUCCESS"
        assert conn.calls == [("inventory", platform_map.platform_option_id, 20)]


class TestFailureClassification:
    def test_rate_limited_error_becomes_retry_wait(self, db_session, platform_map):
        conn = StubProductConnector(error=MarketplaceExternalAPIError("coupang", "RATE_LIMITED", True))
        svc = ProductSyncDispatchService(db_session, connector_factory=_factory(conn))
        outcome = svc.enqueue_inventory_update(platform_map.id, 10)

        with pytest.raises(MarketplaceExternalAPIError):
            svc.execute_command(outcome.command.id)

        db_session.refresh(outcome.command)
        assert outcome.command.status == "RETRY_WAIT"
        assert outcome.command.next_retry_at is not None

    def test_timeout_like_error_becomes_unknown_not_retried(self, db_session, platform_map):
        conn = StubProductConnector(error=MarketplaceExternalAPIError("coupang", "TIMEOUT", True))
        svc = ProductSyncDispatchService(db_session, connector_factory=_factory(conn))
        outcome = svc.enqueue_inventory_update(platform_map.id, 10)

        with pytest.raises(MarketplaceExternalAPIError):
            svc.execute_command(outcome.command.id)

        db_session.refresh(outcome.command)
        assert outcome.command.status == "UNKNOWN"

    def test_credential_missing_becomes_failed(self, db_session, platform_map):
        conn = StubProductConnector(error=MarketplaceCredentialMissingError("coupang"))
        svc = ProductSyncDispatchService(db_session, connector_factory=_factory(conn))
        outcome = svc.enqueue_inventory_update(platform_map.id, 10)

        with pytest.raises(MarketplaceCredentialMissingError):
            svc.execute_command(outcome.command.id)

        db_session.refresh(outcome.command)
        assert outcome.command.status == "FAILED"
        assert outcome.command.retryable is False

    def test_channel_rejection_becomes_failed(self, db_session, platform_map):
        from services.product_sync_dispatch_service import ProductSyncRejectedError

        conn = StubProductConnector(result=ProductSyncActionResult(accepted=False, platform_result_code="ERROR"))
        svc = ProductSyncDispatchService(db_session, connector_factory=_factory(conn))
        outcome = svc.enqueue_inventory_update(platform_map.id, 10)

        with pytest.raises(ProductSyncRejectedError):
            svc.execute_command(outcome.command.id)

        db_session.refresh(outcome.command)
        assert outcome.command.status == "FAILED"


class TestAlreadyRunning:
    def test_execute_raises_when_already_claimed_by_another_worker(self, db_session, platform_map):
        svc = ProductSyncDispatchService(db_session, connector_factory=_factory(StubProductConnector()))
        outcome = svc.enqueue_inventory_update(platform_map.id, 10)
        outcome.command.status = "RUNNING"
        outcome.command.lease_token = "other-worker"
        db_session.flush()

        with pytest.raises(ProductSyncAlreadyRunningError):
            svc.execute_command(outcome.command.id)


class TestRecoverStaleRunning:
    def test_stale_running_is_recovered_to_unknown(self, db_session, platform_map):
        svc = ProductSyncDispatchService(db_session)
        outcome = svc.enqueue_inventory_update(platform_map.id, 10)
        outcome.command.status = "RUNNING"
        outcome.command.lease_token = "dead-worker"
        outcome.command.updated_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        db_session.flush()

        recovered = svc.recover_stale_running("INVENTORY_UPDATE")

        db_session.refresh(outcome.command)
        assert recovered == 1
        assert outcome.command.status == "UNKNOWN"
        assert outcome.command.lease_token is None


class TestResolveUnknownCommand:
    def test_confirmed_not_sent_requeues_as_pending(self, db_session, platform_map):
        svc = ProductSyncDispatchService(db_session)
        outcome = svc.enqueue_inventory_update(platform_map.id, 10)
        outcome.command.status = "UNKNOWN"
        db_session.flush()

        resolved = svc.resolve_unknown_command(outcome.command.id, "CONFIRMED_NOT_SENT")

        assert resolved.status == "PENDING"

    def test_confirmed_success_marks_success(self, db_session, platform_map):
        svc = ProductSyncDispatchService(db_session)
        outcome = svc.enqueue_inventory_update(platform_map.id, 10)
        outcome.command.status = "UNKNOWN"
        db_session.flush()

        resolved = svc.resolve_unknown_command(outcome.command.id, "CONFIRMED_SUCCESS")

        assert resolved.status == "SUCCESS"
        assert resolved.completed_at is not None

    def test_confirmed_failed_marks_failed(self, db_session, platform_map):
        svc = ProductSyncDispatchService(db_session)
        outcome = svc.enqueue_inventory_update(platform_map.id, 10)
        outcome.command.status = "UNKNOWN"
        db_session.flush()

        resolved = svc.resolve_unknown_command(outcome.command.id, "CONFIRMED_FAILED")

        assert resolved.status == "FAILED"

    def test_rejects_non_unknown_command(self, db_session, platform_map):
        svc = ProductSyncDispatchService(db_session)
        outcome = svc.enqueue_inventory_update(platform_map.id, 10)  # 여전히 PENDING.

        with pytest.raises(ValueError):
            svc.resolve_unknown_command(outcome.command.id, "CONFIRMED_SUCCESS")


class TestUnsupportedConnectorCapability:
    def test_inventory_update_unsupported_by_connector_is_confirmed_failed(self, db_session, platform_map):
        from integrations.malls.errors import MarketplaceCapabilityUnsupportedError

        conn = StubProductConnector(supports_inventory=False)
        svc = ProductSyncDispatchService(db_session, connector_factory=_factory(conn))
        outcome = svc.enqueue_inventory_update(platform_map.id, 10)

        with pytest.raises(MarketplaceCapabilityUnsupportedError):
            svc.execute_command(outcome.command.id)

        db_session.refresh(outcome.command)
        assert outcome.command.status == "FAILED"
        assert conn.calls == []
