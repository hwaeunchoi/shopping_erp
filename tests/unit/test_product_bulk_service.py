"""
tests/unit/test_product_bulk_service.py
------------------------------------------------------
ProductBulkService: 상용 ERP 확장(3단계, 네 번째 묶음) - 상품/옵션조합 등록과
재고/판매상태/정보수정의 대량 접수·진행상태 조회·재처리 안전성을 검증한다.
실제 채널 API는 호출하지 않는다(스텁 커넥터만 사용) - 이 서비스는 애초에
enqueue_*만 호출하고 execute_command()는 호출하지 않으므로 채널 커넥터의
create_product/update_* 메서드 자체는 테스트 대상이 아니다(기존 단건 서비스
테스트가 이미 검증함).
"""

import uuid
from typing import Any, Optional

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from integrations.malls.base_mall_connector import SALE_STATUS_ON_SALE
from integrations.malls.errors import MarketplaceCapabilityUnsupportedError
from models.integration_sync import ExternalCommand
from models.product import ProductPlatformMap, ProductPublishDraft, ProductPublishOptionGroupDraft
from repositories.integration_sync_repository import ExternalCommandRepository
from services.product_bulk_service import (
    ACCEPTED,
    BLOCKED_BY_UNKNOWN,
    DUPLICATE_OR_SUPERSEDED,
    FAILED_TO_ENQUEUE,
    RETRIED,
    RETRY_NOT_RETRYABLE,
    RETRY_UNKNOWN_REQUIRES_RESOLUTION,
    UNSUPPORTED,
    VALIDATION_FAILED,
    BulkInfoUpdateItem,
    BulkInventoryItem,
    BulkSaleStatusItem,
    ProductBulkService,
)
from services.product_option_publish_service import PRODUCT_OPTION_CREATE
from services.product_publish_service import PRODUCT_CREATE
from services.product_sync_dispatch_service import INVENTORY_UPDATE


class StubConnector:
    """capability 플래그만 껐다 켰다 하는 최소 스텁 - 이 서비스는 enqueue_*만
    호출하므로 create_product/update_* 자체는 호출되지 않는다."""

    def __init__(
        self,
        *,
        supports_product_create=True,
        supports_product_option_create=True,
        supports_inventory_update=True,
        supports_sale_status_update=True,
        supports_product_info_update=True,
    ):
        self.supports_product_create = supports_product_create
        self.supports_product_option_create = supports_product_option_create
        self.supports_inventory_update = supports_inventory_update
        self.supports_sale_status_update = supports_sale_status_update
        self.supports_product_info_update = supports_product_info_update


def _factory(connectors: dict[str, Any]):
    def factory(connector_class: str, session: Any = None, platform_id: Optional[int] = None):
        if connector_class not in connectors:
            raise MarketplaceCapabilityUnsupportedError(connector_class, "runtime-connector")
        return connectors[connector_class]

    return factory


@pytest.fixture(autouse=True)
def _enable_all_flags(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "product_publish_enabled", True)
    monkeypatch.setattr(settings, "product_option_publish_enabled", True)
    monkeypatch.setattr(settings, "product_channel_sync_enabled", True)
    monkeypatch.setattr(settings, "product_info_update_enabled", True)


def _service(db_session, connectors: Optional[dict[str, Any]] = None) -> ProductBulkService:
    connectors = connectors if connectors is not None else {"CoupangConnector": StubConnector()}
    return ProductBulkService(db_session, connector_factory=_factory(connectors))


def _draft(db_session, product_option, platform, **overrides) -> ProductPublishDraft:
    draft = ProductPublishDraft(
        product_option_id=product_option.id,
        platform_id=platform.id,
        name="테스트상품",
        sale_price=19900,
        description_html="<p>설명</p>",
        category_code="50000803",
        image_urls_json='["https://img.example.com/a.jpg"]',
        stock_quantity=10,
    )
    for k, v in overrides.items():
        setattr(draft, k, v)
    db_session.add(draft)
    db_session.flush()
    return draft


class TestSubmitPublishDraftsAllAccepted:
    def test_all_accepted(self, db_session, product_option, second_product_option, platform):
        d1 = _draft(db_session, product_option, platform)
        d2 = _draft(db_session, second_product_option, platform)
        service = _service(db_session)

        result = service.submit_publish_drafts([d1.id, d2.id])

        assert result.aborted is False
        assert [item.outcome for item in result.items] == [ACCEPTED, ACCEPTED]
        assert all(item.command_id is not None for item in result.items)
        commands = db_session.query(ExternalCommand).filter(ExternalCommand.command_type == PRODUCT_CREATE).all()
        assert len(commands) == 2


class TestValidationFailuresDontBlockRest:
    def test_not_found_does_not_block_following_items(
        self, db_session, product_option, second_product_option, platform
    ):
        d1 = _draft(db_session, product_option, platform)
        d2 = _draft(db_session, second_product_option, platform)
        service = _service(db_session)

        result = service.submit_publish_drafts([999999, d1.id, d2.id])

        assert result.aborted is False
        assert result.items[0].outcome == VALIDATION_FAILED
        assert result.items[0].error_code == "NOT_FOUND"
        assert result.items[1].outcome == ACCEPTED
        assert result.items[2].outcome == ACCEPTED

    def test_already_registered_is_validation_failed_not_abort(self, db_session, product_option, platform):
        db_session.add(
            ProductPlatformMap(product_option_id=product_option.id, platform_id=platform.id, platform_option_id="EXT-1")
        )
        db_session.flush()
        d1 = _draft(db_session, product_option, platform)
        service = _service(db_session)

        result = service.submit_publish_drafts([d1.id])

        assert result.aborted is False
        assert result.items[0].outcome == VALIDATION_FAILED
        assert result.items[0].error_code == "ALREADY_REGISTERED"


class TestMidBatchDbErrorAbortsRemainingWithoutErasingPriorCommits:
    def test_infra_error_aborts_remaining_but_keeps_prior_commit(
        self, db_session, product_option, second_product_option, platform, monkeypatch
    ):
        d1 = _draft(db_session, product_option, platform)
        d2 = _draft(db_session, second_product_option, platform)
        db_session.commit()
        service = _service(db_session)

        original_commit = db_session.commit
        state = {"calls": 0}

        def flaky_commit():
            state["calls"] += 1
            if state["calls"] == 1:
                # 첫 항목은 정상 커밋시켜 실제로 확정되게 한다.
                return original_commit()
            raise OperationalError("insert", {}, Exception("connection lost"))

        monkeypatch.setattr(db_session, "commit", flaky_commit)

        result = service.submit_publish_drafts([d1.id, d2.id])

        assert result.aborted is True
        assert result.items[0].outcome == ACCEPTED
        assert result.items[1].outcome == FAILED_TO_ENQUEUE
        assert result.items[1].error_code == "DB_OR_INTERNAL_ERROR"

        # 세션 상태를 원복해 검증 쿼리가 통과하도록 한다.
        monkeypatch.setattr(db_session, "commit", original_commit)
        db_session.rollback()
        commands = db_session.query(ExternalCommand).filter(ExternalCommand.command_type == PRODUCT_CREATE).all()
        # 이미 커밋된 1번 항목의 outbox 이력은 지워지지 않는다(SAVEPOINT 미사용).
        assert len(commands) == 1
        assert commands[0].target_id == d1.id

    def test_remaining_unattempted_items_are_marked_aborted_not_silently_skipped(
        self, db_session, product_option, second_product_option, platform, monkeypatch
    ):
        third_option_product = second_product_option
        d1 = _draft(db_session, product_option, platform)
        d2 = _draft(db_session, third_option_product, platform)
        db_session.commit()
        service = _service(db_session)

        def boom():
            raise OperationalError("insert", {}, Exception("db down"))

        monkeypatch.setattr(db_session, "commit", boom)

        result = service.submit_publish_drafts([d1.id, d2.id])

        assert result.aborted is True
        assert result.items[0].outcome == FAILED_TO_ENQUEUE
        assert result.items[0].error_code == "DB_OR_INTERNAL_ERROR"
        assert result.items[1].outcome == FAILED_TO_ENQUEUE
        assert result.items[1].error_code == "ABORTED_DUE_TO_PRIOR_ERROR"


class TestDuplicateRequestCreatesNoNewCommand:
    def test_identical_resubmit_is_duplicate_not_new_command(self, db_session, product_option, platform):
        d1 = _draft(db_session, product_option, platform)
        service = _service(db_session)

        first = service.submit_publish_drafts([d1.id])
        second = service.submit_publish_drafts([d1.id])

        assert first.items[0].outcome == ACCEPTED
        assert second.items[0].outcome == DUPLICATE_OR_SUPERSEDED
        assert second.items[0].command_id == first.items[0].command_id
        commands = db_session.query(ExternalCommand).filter(ExternalCommand.command_type == PRODUCT_CREATE).all()
        assert len(commands) == 1

    def test_same_target_twice_within_one_batch_is_detected(self, db_session, product_option, platform):
        d1 = _draft(db_session, product_option, platform)
        service = _service(db_session)

        result = service.submit_publish_drafts([d1.id, d1.id])

        assert result.items[0].outcome == ACCEPTED
        assert result.items[1].outcome == DUPLICATE_OR_SUPERSEDED
        assert result.items[1].command_id == result.items[0].command_id


class TestConflictsWithExistingCommands:
    def test_blocked_by_unresolved_unknown(self, db_session, product_option, platform):
        d1 = _draft(db_session, product_option, platform)
        db_session.add(
            ExternalCommand(
                idempotency_key=f"{PRODUCT_CREATE}:{d1.id}:preexisting",
                command_type=PRODUCT_CREATE,
                platform_id=platform.id,
                platform_code=platform.code,
                target_type="PRODUCT_PUBLISH_DRAFT",
                target_id=d1.id,
                status="UNKNOWN",
                trace_id=uuid.uuid4().hex,
            )
        )
        db_session.flush()
        service = _service(db_session)

        result = service.submit_publish_drafts([d1.id])

        assert result.items[0].outcome == BLOCKED_BY_UNKNOWN
        # 새 PENDING 명령을 만들지 않았어야 한다(선점된 UNKNOWN 하나만 존재).
        commands = db_session.query(ExternalCommand).filter(ExternalCommand.target_id == d1.id).all()
        assert len(commands) == 1
        assert commands[0].status == "UNKNOWN"

    def test_stale_pending_is_superseded_when_snapshot_changes(self, db_session, product_option, platform):
        from services.product_publish_service import ProductPublishService

        d1 = _draft(db_session, product_option, platform)
        service = _service(db_session)
        first = service.submit_publish_drafts([d1.id])
        assert first.items[0].outcome == ACCEPTED
        first_command_id = first.items[0].command_id

        ProductPublishService(db_session).save_draft(product_option.id, platform.id, sale_price=29900)
        db_session.commit()

        second = service.submit_publish_drafts([d1.id])
        assert second.items[0].outcome == ACCEPTED
        assert second.items[0].command_id != first_command_id
        db_session.refresh(db_session.get(ExternalCommand, first_command_id))
        stale = db_session.get(ExternalCommand, first_command_id)
        assert stale.status == "CANCELLED"
        assert stale.error_code == "SUPERSEDED_BY_NEWER_REQUEST"

    def test_isolated_across_different_targets(self, db_session, product_option, second_product_option, platform):
        d1 = _draft(db_session, product_option, platform)
        d2 = _draft(db_session, second_product_option, platform)
        db_session.add(
            ExternalCommand(
                idempotency_key=f"{PRODUCT_CREATE}:{d1.id}:preexisting",
                command_type=PRODUCT_CREATE,
                platform_id=platform.id,
                platform_code=platform.code,
                target_type="PRODUCT_PUBLISH_DRAFT",
                target_id=d1.id,
                status="UNKNOWN",
                trace_id=uuid.uuid4().hex,
            )
        )
        db_session.flush()
        service = _service(db_session)

        result = service.submit_publish_drafts([d1.id, d2.id])

        assert result.items[0].outcome == BLOCKED_BY_UNKNOWN
        assert result.items[1].outcome == ACCEPTED


class TestUnsupportedChannel:
    def test_unsupported_connector_class(self, db_session, product_option, platform):
        d1 = _draft(db_session, product_option, platform)
        service = _service(db_session, connectors={})

        result = service.submit_publish_drafts([d1.id])

        assert result.items[0].outcome == UNSUPPORTED

    def test_unsupported_capability_on_known_connector(self, db_session, product_option, platform):
        mapping = ProductPlatformMap(
            product_option_id=product_option.id, platform_id=platform.id, platform_option_id="EXT-1"
        )
        db_session.add(mapping)
        db_session.flush()
        connector = StubConnector(supports_product_info_update=False)
        service = _service(db_session, connectors={"CoupangConnector": connector})

        result = service.submit_info_updates([BulkInfoUpdateItem(product_platform_map_id=mapping.id, name="새이름")])

        assert result.items[0].outcome == UNSUPPORTED
        assert result.items[0].error_code == "CoupangConnector"


class TestBulkInventoryAndSaleStatus:
    def test_inventory_updates_all_accepted_and_isolated(
        self, db_session, product_option, second_product_option, platform
    ):
        m1 = ProductPlatformMap(product_option_id=product_option.id, platform_id=platform.id, platform_option_id="A")
        m2 = ProductPlatformMap(
            product_option_id=second_product_option.id, platform_id=platform.id, platform_option_id="B"
        )
        db_session.add_all([m1, m2])
        db_session.flush()
        service = _service(db_session)

        result = service.submit_inventory_updates(
            [
                BulkInventoryItem(product_platform_map_id=m1.id, target_quantity=5),
                BulkInventoryItem(product_platform_map_id=m2.id, target_quantity=-1),
            ]
        )

        assert result.items[0].outcome == ACCEPTED
        assert result.items[1].outcome == VALIDATION_FAILED
        assert "0 이상" in (result.items[1].error_code or "")

    def test_sale_status_updates(self, db_session, product_option, platform):
        mapping = ProductPlatformMap(
            product_option_id=product_option.id, platform_id=platform.id, platform_option_id="A"
        )
        db_session.add(mapping)
        db_session.flush()
        service = _service(db_session)

        result = service.submit_sale_status_updates(
            [BulkSaleStatusItem(product_platform_map_id=mapping.id, target_status=SALE_STATUS_ON_SALE)]
        )

        assert result.items[0].outcome == ACCEPTED
        command = db_session.get(ExternalCommand, result.items[0].command_id)
        assert command.command_type == "SALE_STATUS_UPDATE"


class TestOptionGroupPublish:
    def test_option_group_all_accepted(self, db_session, product_option, platform):
        from models.product import ProductPublishOptionGroupItemDraft

        group = ProductPublishOptionGroupDraft(
            product_id=product_option.product_id,
            platform_id=platform.id,
            name="옵션조합상품",
            base_sale_price=19900,
            description_html="<p>설명</p>",
            category_code="50000803",
            image_urls_json='["https://img.example.com/a.jpg"]',
        )
        db_session.add(group)
        db_session.flush()
        db_session.add(
            ProductPublishOptionGroupItemDraft(
                group_draft_id=group.id, product_option_id=product_option.id, sale_price=19900, stock_quantity=5
            )
        )
        db_session.flush()
        service = _service(db_session)

        result = service.submit_option_publish_drafts([group.id])

        assert result.items[0].outcome == ACCEPTED
        command = db_session.get(ExternalCommand, result.items[0].command_id)
        assert command.command_type == PRODUCT_OPTION_CREATE


class TestFeatureFlagOff:
    def test_flag_off_returns_validation_failed_without_any_repo_call(
        self, db_session, product_option, platform, monkeypatch
    ):
        from config.settings import settings

        monkeypatch.setattr(settings, "product_publish_enabled", False)
        d1 = _draft(db_session, product_option, platform)
        service = _service(db_session)

        def _fail(*args, **kwargs):
            raise AssertionError("flag OFF인데 리포지토리 조회가 실행됨")

        monkeypatch.setattr(ExternalCommandRepository, "list_for_targets", _fail)

        result = service.submit_publish_drafts([d1.id])

        assert result.items[0].outcome == VALIDATION_FAILED
        assert result.items[0].error_code == "FEATURE_DISABLED"

    def test_flag_off_for_inventory_separately_controlled(self, db_session, product_option, platform, monkeypatch):
        from config.settings import settings

        monkeypatch.setattr(settings, "product_channel_sync_enabled", False)
        mapping = ProductPlatformMap(
            product_option_id=product_option.id, platform_id=platform.id, platform_option_id="A"
        )
        db_session.add(mapping)
        db_session.flush()
        service = _service(db_session)

        result = service.submit_inventory_updates(
            [BulkInventoryItem(product_platform_map_id=mapping.id, target_quantity=5)]
        )

        assert result.items[0].outcome == VALIDATION_FAILED
        assert result.items[0].error_code == "FEATURE_DISABLED"


class TestErrorMessagesDoNotLeak:
    def test_unexpected_exception_message_is_not_exposed(self, db_session, product_option, platform, monkeypatch):
        d1 = _draft(db_session, product_option, platform)
        service = _service(db_session)

        def boom(*args, **kwargs):
            raise RuntimeError("secret=abc123 url=https://internal.example.com/leak")

        monkeypatch.setattr(service.publish_service, "enqueue_create", boom)

        result = service.submit_publish_drafts([d1.id])

        assert result.items[0].outcome == FAILED_TO_ENQUEUE
        assert result.items[0].error_code == "DB_OR_INTERNAL_ERROR"
        assert "secret" not in (result.items[0].error_code or "")
        assert "internal.example.com" not in (result.items[0].error_code or "")


class TestConcurrentIdempotencyKeyRace:
    def test_integrity_error_on_insert_retries_and_resolves_as_existing_command(
        self, db_session, product_option, platform, monkeypatch
    ):
        """다른 worker가 같은 순간 같은 멱등키로 먼저 커밋한 상황을 흉내낸다 -
        add()가 첫 호출에서 유니크 제약 위반을 던지게 하고, 롤백 후 재시도에서는
        (그 worker가 이미 커밋한 것처럼) 정상 add가 성공하게 한다."""
        d1 = _draft(db_session, product_option, platform)
        service = _service(db_session)

        db_session.commit()
        original_add = ExternalCommandRepository.add
        state = {"calls": 0}

        def flaky_add(self, obj):
            state["calls"] += 1
            if state["calls"] == 1:
                raise IntegrityError("insert", {}, Exception("unique violation"))
            return original_add(self, obj)

        monkeypatch.setattr(ExternalCommandRepository, "add", flaky_add)

        result = service.submit_publish_drafts([d1.id])

        assert result.aborted is False
        assert result.items[0].outcome == ACCEPTED
        assert state["calls"] == 2


class TestGetCommandsStatus:
    def test_returns_only_requested_commands(self, db_session, product_option, second_product_option, platform):
        d1 = _draft(db_session, product_option, platform)
        d2 = _draft(db_session, second_product_option, platform)
        service = _service(db_session)
        r1 = service.submit_publish_drafts([d1.id])
        r2 = service.submit_publish_drafts([d2.id])
        id1, id2 = r1.items[0].command_id, r2.items[0].command_id
        assert id1 is not None and id2 is not None

        statuses = service.get_commands_status([id1, id2])

        assert {c.id for c in statuses} == {id1, id2}

    def test_empty_list_returns_empty(self, db_session):
        service = _service(db_session)
        assert service.get_commands_status([]) == []


class TestRetryCommands:
    def test_retries_only_failed_items(self, db_session, product_option, second_product_option, platform):
        d1 = _draft(db_session, product_option, platform)
        d2 = _draft(db_session, second_product_option, platform)
        service = _service(db_session)
        r1 = service.submit_publish_drafts([d1.id])
        r2 = service.submit_publish_drafts([d2.id])
        c1 = db_session.get(ExternalCommand, r1.items[0].command_id)
        c1.status = "FAILED"
        c2 = db_session.get(ExternalCommand, r2.items[0].command_id)
        c2.status = "SUCCESS"
        db_session.commit()

        result = service.retry_commands([c1.id, c2.id])

        assert result.items[0].outcome == RETRIED
        assert result.items[1].outcome == RETRY_NOT_RETRYABLE
        db_session.refresh(c1)
        assert c1.status == "PENDING"

    def test_unknown_cannot_be_retried(self, db_session, product_option, platform):
        d1 = _draft(db_session, product_option, platform)
        service = _service(db_session)
        r1 = service.submit_publish_drafts([d1.id])
        command = db_session.get(ExternalCommand, r1.items[0].command_id)
        command.status = "UNKNOWN"
        db_session.commit()

        result = service.retry_commands([command.id])

        assert result.items[0].outcome == RETRY_UNKNOWN_REQUIRES_RESOLUTION
        db_session.refresh(command)
        assert command.status == "UNKNOWN"

    def test_retry_infra_error_aborts_remaining(
        self, db_session, product_option, second_product_option, platform, monkeypatch
    ):
        d1 = _draft(db_session, product_option, platform)
        d2 = _draft(db_session, second_product_option, platform)
        service = _service(db_session)
        r1 = service.submit_publish_drafts([d1.id])
        r2 = service.submit_publish_drafts([d2.id])
        c1 = db_session.get(ExternalCommand, r1.items[0].command_id)
        c1.status = "FAILED"
        c2 = db_session.get(ExternalCommand, r2.items[0].command_id)
        c2.status = "FAILED"
        db_session.commit()

        def boom(*args, **kwargs):
            raise OperationalError("update", {}, Exception("db down"))

        monkeypatch.setattr(db_session, "commit", boom)

        result = service.retry_commands([c1.id, c2.id])

        assert result.aborted is True
        assert result.items[0].outcome == FAILED_TO_ENQUEUE
        assert result.items[1].outcome == FAILED_TO_ENQUEUE
        assert result.items[1].error_code == "ABORTED_DUE_TO_PRIOR_ERROR"

    def test_retry_dispatches_sync_command_types_correctly(self, db_session, product_option, platform):
        mapping = ProductPlatformMap(
            product_option_id=product_option.id, platform_id=platform.id, platform_option_id="A"
        )
        db_session.add(mapping)
        db_session.flush()
        service = _service(db_session)
        result = service.submit_inventory_updates(
            [BulkInventoryItem(product_platform_map_id=mapping.id, target_quantity=5)]
        )
        command = db_session.get(ExternalCommand, result.items[0].command_id)
        assert command.command_type == INVENTORY_UPDATE
        command.status = "FAILED"
        db_session.commit()

        retry_result = service.retry_commands([command.id], resolved_by=None)

        assert retry_result.items[0].outcome == RETRIED
        db_session.refresh(command)
        assert command.status == "PENDING"


class TestExistingSingleItemPathsStillWork:
    """대량 서비스가 기존 단건 서비스를 우회하지 않고 그대로 호출한다는 것을
    확인한다 - 대량으로 접수한 명령이 기존 execute_command() 실행 경로와
    완전히 호환된다(같은 ExternalCommand 테이블, 같은 command_type)."""

    def test_bulk_created_command_executes_through_existing_dispatch_path(self, db_session, product_option, platform):
        from integrations.malls.base_mall_connector import ProductCreateResult
        from services.product_publish_service import ProductPublishService

        d1 = _draft(db_session, product_option, platform)
        bulk_service = _service(db_session)
        result = bulk_service.submit_publish_drafts([d1.id])
        assert result.items[0].outcome == ACCEPTED

        class ExecConnector(StubConnector):
            def create_product(self, draft_snapshot):
                return ProductCreateResult(
                    accepted=True,
                    platform_result_code="SUCCESS",
                    channel_product_id="EXT-P-1",
                    channel_option_id="EXT-O-1",
                )

        command_id = result.items[0].command_id
        assert command_id is not None
        exec_service = ProductPublishService(
            db_session, connector_factory=_factory({"CoupangConnector": ExecConnector()})
        )
        outcome = exec_service.execute_command(command_id)

        assert outcome.command.status == "SUCCESS"
