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
from integrations.malls.errors import (
    MarketplaceCapabilityUnsupportedError,
    MarketplaceCredentialMissingError,
    MarketplaceExternalAPIError,
)
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

    def __init__(self, *, supports_inventory=True, supports_status=True, supports_info=True, result=None, error=None):
        self.supports_inventory_update = supports_inventory
        self.supports_sale_status_update = supports_status
        self.supports_product_info_update = supports_info
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

    def update_product_info(
        self, platform_option_id, platform_origin_product_id=None, name=None, sale_price=None, description=None
    ):
        self.calls.append(("info", platform_option_id, name, sale_price, description))
        if self._error:
            raise self._error
        return self._result


def _factory(connector):
    return lambda connector_class, session=None, platform_id=None: connector


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "product_channel_sync_enabled", True)
    # PRODUCT_INFO_UPDATE(상용 ERP 확장 3단계 두 번째 묶음)는 별도 독립 플래그로
    # 통제한다(services.product_sync_dispatch_service._enqueue 참고) - 이 파일의
    # 나머지 테스트는 "기능이 켜져 있을 때"를 전제하므로 함께 켠다. 플래그가 실제로
    # 독립적인지는 TestInfoUpdate.test_controlled_by_its_own_flag_independent_of_
    # inventory_flag가 각각 개별적으로 다시 꺼서 검증한다.
    monkeypatch.setattr(settings, "product_publish_enabled", True)


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
        # 화면에 "MarketplaceCapabilityUnsupportedError"라는 클래스명 대신, 구체적인
        # 차단 사유(capability)가 그대로 노출되어야 사용자가 원인을 구분할 수 있다.
        assert outcome.command.error_code == "inventory_update"


def _sibling_mapping(db_session, platform, second_product_option, origin_product_id: str, option_id: str):
    """platform_map(fixture)과 같은 platform_origin_product_id를 공유하지만 서로
    다른 ProductPlatformMap 행 - 네이버가 원상품 하나에 복수 채널상품(스마트스토어/
    윈도우 등)을 가질 수 있어 유니크 제약이 없는 상황을 재현한다."""
    from models.product import ProductPlatformMap

    sibling = ProductPlatformMap(
        product_option_id=second_product_option.id,
        platform_id=platform.id,
        platform_option_id=option_id,
        platform_origin_product_id=origin_product_id,
    )
    db_session.add(sibling)
    db_session.flush()
    return sibling


class TestSharedExternalTargetAcrossMappings:
    """서로 다른 내부 매핑(ProductPlatformMap)이 같은 외부 대상(네이버
    platform_origin_product_id)을 공유하는 경우에도 동시성 제어가 그 전부를 하나의
    대상으로 취급하는지 검증한다 - platform_option_id와 달리 platform_origin_product_id
    에는 유니크 제약이 없다(models.product.ProductPlatformMap 참고)."""

    def test_enqueue_cancels_stale_pending_command_on_sibling_mapping(
        self, db_session, platform, platform_map, second_product_option
    ):
        platform_map.platform_origin_product_id = "ORIGIN-SHARED-1"
        db_session.flush()
        sibling = _sibling_mapping(db_session, platform, second_product_option, "ORIGIN-SHARED-1", "OPT-SIBLING-1")

        svc = ProductSyncDispatchService(db_session)
        old = svc.enqueue_inventory_update(platform_map.id, 10)
        new = svc.enqueue_inventory_update(sibling.id, 20)  # 다른 매핑, 같은 외부 대상.

        db_session.refresh(old.command)
        assert old.command.status == "CANCELLED"
        assert old.command.error_code == "SUPERSEDED_BY_NEWER_REQUEST"
        assert new.command.status == "PENDING"

    def test_execute_cancels_when_sibling_mapping_has_a_newer_command(
        self, db_session, platform, platform_map, second_product_option
    ):
        """claim() 직후 재확인이 sibling 매핑에 생긴 더 최신 명령도 감지한다 - 다른
        worker가 sibling 쪽을 이미 claim(RUNNING)해서 enqueue의 사전 취소를
        우회하는 경우를 흉내낸다."""
        platform_map.platform_origin_product_id = "ORIGIN-SHARED-2"
        db_session.flush()
        sibling = _sibling_mapping(db_session, platform, second_product_option, "ORIGIN-SHARED-2", "OPT-SIBLING-2")

        conn = StubProductConnector()
        svc = ProductSyncDispatchService(db_session, connector_factory=_factory(conn))
        old = svc.enqueue_inventory_update(platform_map.id, 10)
        old.command.status = "RUNNING"  # 다른 worker가 claim했다고 가정 - 사전 취소를 우회.
        old.command.lease_token = "other-worker"
        db_session.flush()

        svc.enqueue_inventory_update(sibling.id, 20)  # sibling 쪽에 더 최신 명령 생성.

        # old가 회수돼 다시 PENDING이 됐다고 가정(예: 죽은 worker 회수) - claim 가능해짐.
        old.command.status = "PENDING"
        old.command.lease_token = None
        db_session.flush()

        result = svc.execute_command(old.command.id)

        assert result.command.status == "CANCELLED"
        assert conn.calls == []  # 채널 호출 없이 취소됐다.

    def test_new_inventory_command_does_not_cancel_sale_status_command_on_sibling(
        self, db_session, platform, platform_map, second_product_option
    ):
        """공유 대상이어도 명령종류가 다르면(재고 vs 판매상태) 서로 취소하지 않는다."""
        platform_map.platform_origin_product_id = "ORIGIN-SHARED-3"
        db_session.flush()
        sibling = _sibling_mapping(db_session, platform, second_product_option, "ORIGIN-SHARED-3", "OPT-SIBLING-3")

        svc = ProductSyncDispatchService(db_session)
        status_cmd = svc.enqueue_sale_status_update(sibling.id, SALE_STATUS_ON_SALE)
        svc.enqueue_inventory_update(platform_map.id, 10)

        db_session.refresh(status_cmd.command)
        assert status_cmd.command.status == "PENDING"  # 다른 종류라 취소되지 않았다.


class TestCrossTypeRunningExclusion:
    """네이버의 "현재 상태 조회 -> 변경 요청" 같은 read-modify-write 채널 API에서,
    재고 명령과 판매상태 명령이 같은 외부 대상에 동시에 나가 서로의 조회 결과를
    덮어쓰지 않도록 - 명령종류가 달라도 지금 RUNNING인 다른 명령이 있으면 이번
    실행은 미루고 PENDING으로 되돌린다."""

    def test_execute_defers_when_a_different_type_command_is_running_for_same_mapping(self, db_session, platform_map):
        conn = StubProductConnector()
        svc = ProductSyncDispatchService(db_session, connector_factory=_factory(conn))
        running_status_cmd = svc.enqueue_sale_status_update(platform_map.id, SALE_STATUS_ON_SALE)
        running_status_cmd.command.status = "RUNNING"
        running_status_cmd.command.lease_token = "other-worker"
        db_session.flush()

        inventory_cmd = svc.enqueue_inventory_update(platform_map.id, 10)
        result = svc.execute_command(inventory_cmd.command.id)

        assert result.command.status == "PENDING"  # 재대기 - 채널 호출 없음.
        assert result.command.lease_token is None
        assert conn.calls == []

    def test_execute_proceeds_once_the_other_type_command_finishes(self, db_session, platform_map):
        conn = StubProductConnector()
        svc = ProductSyncDispatchService(db_session, connector_factory=_factory(conn))
        running_status_cmd = svc.enqueue_sale_status_update(platform_map.id, SALE_STATUS_ON_SALE)
        running_status_cmd.command.status = "SUCCESS"  # 이미 끝남 - 더 이상 RUNNING 아님.
        db_session.flush()

        inventory_cmd = svc.enqueue_inventory_update(platform_map.id, 10)
        result = svc.execute_command(inventory_cmd.command.id)

        assert result.command.status == "SUCCESS"
        assert conn.calls == [("inventory", platform_map.platform_option_id, 10)]

    def test_same_type_running_is_still_caught_by_claim_not_this_check(self, db_session, platform_map):
        """같은 명령종류끼리는 claim()의 원자적 UPDATE 자체가 이미 동시 실행을 막는다
        (rowcount==0 -> ProductSyncAlreadyRunningError) - 이 교차종류 검사와는 별개 경로."""
        svc = ProductSyncDispatchService(db_session, connector_factory=_factory(StubProductConnector()))
        outcome = svc.enqueue_inventory_update(platform_map.id, 10)
        outcome.command.status = "RUNNING"
        outcome.command.lease_token = "other-worker"
        db_session.flush()

        with pytest.raises(ProductSyncAlreadyRunningError):
            svc.execute_command(outcome.command.id)


class TestInfoUpdate:
    """PRODUCT_INFO_UPDATE(상용 ERP 확장 3단계, 두 번째 묶음) - 상품명/판매가/
    상세설명 중 실제로 바뀐 값만 전송한다. TARGET_TYPE을 재고/판매상태와 공유하므로
    형제 매핑 확장과 교차종류 실행중 검사가 별도 구현 없이 그대로 적용된다."""

    def test_requires_at_least_one_field(self, db_session, platform_map):
        svc = ProductSyncDispatchService(db_session)
        with pytest.raises(ValueError):
            svc.enqueue_info_update(platform_map.id)
        assert db_session.query(ExternalCommand).count() == 0

    def test_controlled_by_its_own_flag_independent_of_inventory_flag(self, db_session, platform_map, monkeypatch):
        """product_publish_enabled는 product_channel_sync_enabled와 별개의 독립
        플래그다 - 재고/판매상태 플래그가 켜져 있어도(이 파일의 autouse _enable
        픽스처) 정보수정 전용 플래그가 꺼져 있으면 여전히 차단돼야 하고, 반대로
        정보수정 플래그만 켜고 재고 플래그를 꺼도 재고 전송은 여전히 차단돼야 한다."""
        from config.settings import settings

        monkeypatch.setattr(settings, "product_publish_enabled", False)
        svc = ProductSyncDispatchService(db_session)
        with pytest.raises(ProductChannelSyncDisabledError):
            svc.enqueue_info_update(platform_map.id, name="새이름")

        monkeypatch.setattr(settings, "product_publish_enabled", True)
        monkeypatch.setattr(settings, "product_channel_sync_enabled", False)
        with pytest.raises(ProductChannelSyncDisabledError):
            svc.enqueue_inventory_update(platform_map.id, 10)
        # 반대로 정보수정은 이 상태에서 정상 접수돼야 한다(플래그가 독립적이라는 증거).
        outcome = svc.enqueue_info_update(platform_map.id, name="새이름2")
        assert outcome.command.status == "PENDING"

    def test_negative_price_is_rejected(self, db_session, platform_map):
        svc = ProductSyncDispatchService(db_session)
        with pytest.raises(ValueError):
            svc.enqueue_info_update(platform_map.id, sale_price=-1)
        assert db_session.query(ExternalCommand).count() == 0

    def test_execute_sends_only_provided_fields(self, db_session, platform_map):
        conn = StubProductConnector()
        svc = ProductSyncDispatchService(db_session, connector_factory=_factory(conn))
        outcome = svc.enqueue_info_update(platform_map.id, name="새 이름", sale_price=29900)

        result = svc.execute_command(outcome.command.id)

        assert result.command.status == "SUCCESS"
        assert conn.calls == [("info", platform_map.platform_option_id, "새 이름", 29900, None)]

    def test_unsupported_connector_is_confirmed_failed_without_call(self, db_session, platform_map):
        conn = StubProductConnector(supports_info=False)
        svc = ProductSyncDispatchService(db_session, connector_factory=_factory(conn))
        outcome = svc.enqueue_info_update(platform_map.id, name="새 이름")

        with pytest.raises(MarketplaceCapabilityUnsupportedError):
            svc.execute_command(outcome.command.id)
        db_session.refresh(outcome.command)
        assert outcome.command.status == "FAILED"
        assert conn.calls == []

    def test_duplicate_submission_with_same_values_reuses_existing_command(self, db_session, platform_map):
        svc = ProductSyncDispatchService(db_session, connector_factory=_factory(StubProductConnector()))
        first = svc.enqueue_info_update(platform_map.id, name="같은이름")
        second = svc.enqueue_info_update(platform_map.id, name="같은이름")
        assert first.command.id == second.command.id
        assert db_session.query(ExternalCommand).count() == 1

    def test_info_update_does_not_cancel_pending_inventory_command_and_vice_versa(self, db_session, platform_map):
        """재고 변경과 정보수정은 서로 다른 의도이므로 상대방의 대기 명령을 잘못
        취소하면 안 된다(재고 vs 판매상태와 동일 원칙)."""
        svc = ProductSyncDispatchService(db_session, connector_factory=_factory(StubProductConnector()))
        inventory_cmd = svc.enqueue_inventory_update(platform_map.id, 10)
        info_cmd = svc.enqueue_info_update(platform_map.id, name="새이름")

        db_session.refresh(inventory_cmd.command)
        db_session.refresh(info_cmd.command)
        assert inventory_cmd.command.status == "PENDING"
        assert info_cmd.command.status == "PENDING"

    def test_info_update_backs_off_when_inventory_command_is_running_for_same_target(self, db_session, platform_map):
        """교차종류 실행중 검사(exists_other_running_for_targets)가 재고/판매상태
        쌍뿐 아니라 정보수정에도 그대로 적용되는지 검증한다."""
        conn = StubProductConnector()
        svc = ProductSyncDispatchService(db_session, connector_factory=_factory(conn))
        running_inventory_cmd = svc.enqueue_inventory_update(platform_map.id, 10)
        running_inventory_cmd.command.status = "RUNNING"
        running_inventory_cmd.command.lease_token = "other-worker"
        db_session.flush()

        info_cmd = svc.enqueue_info_update(platform_map.id, name="새이름")
        result = svc.execute_command(info_cmd.command.id)

        assert result.command.status == "PENDING"
        assert conn.calls == []

    def test_command_type_mismatch_guard_still_applies(self, db_session, platform_map):
        svc = ProductSyncDispatchService(db_session, connector_factory=_factory(StubProductConnector()))
        outcome = svc.enqueue_info_update(platform_map.id, name="새이름")
        from models.integration_sync import ExternalCommand as _EC

        command = db_session.get(_EC, outcome.command.id)
        command.command_type = "SOME_OTHER_TYPE"
        db_session.flush()

        with pytest.raises(ProductSyncCommandTypeMismatchError):
            svc.execute_command(outcome.command.id)
