"""
tests/unit/test_product_publish_service.py
------------------------------------------------------
ProductPublishService: 상용 ERP 확장(3단계, 두 번째 묶음) - 신규 상품 등록
(PRODUCT_CREATE)의 outbox/lease/UNKNOWN/중복접수/확인된 외부ID만 매핑 안전성을
검증한다. 실제 채널 API는 호출하지 않는다(스텁 커넥터만 사용).
"""

import pytest

from integrations.malls.base_mall_connector import ProductCreateResult
from integrations.malls.errors import MarketplaceCapabilityUnsupportedError, MarketplaceExternalAPIError
from models.integration_sync import ExternalCommand
from models.product import ProductPlatformMap
from repositories.product_repository import ProductPlatformMapRepository
from services.product_publish_service import (
    ProductPublishAlreadyRegisteredError,
    ProductPublishAlreadyRunningError,
    ProductPublishDisabledError,
    ProductPublishDraftNotFoundError,
    ProductPublishService,
)


class StubPublishConnector:
    """create_product()/fetch_registration_status()를 제어할 수 있는 스텁."""

    supports_product_create = True

    def __init__(self, *, result=None, error=None, registration_status=None):
        self._result = result or ProductCreateResult(
            accepted=True, platform_result_code="SUCCESS", channel_product_id="EXT-P-1", channel_option_id="EXT-O-1"
        )
        self._error = error
        self._registration_status = registration_status or {"status_name": "APPROVED", "channel_option_ids": []}
        self.calls: list = []

    def create_product(self, draft_snapshot):
        self.calls.append(draft_snapshot)
        if self._error:
            raise self._error
        return self._result

    def fetch_registration_status(self, platform_product_id):
        return self._registration_status


def _factory(connector):
    return lambda connector_class, session=None, platform_id=None: connector


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "product_publish_enabled", True)


def _valid_snapshot_kwargs() -> dict:
    return {
        "name": "테스트상품",
        "sale_price": 19900,
        "description_html": "<p>설명</p>",
        "category_code": "50000803",
        "image_urls": ["https://img.example.com/main.jpg"],
        "stock_quantity": 10,
    }


class TestDisabledByDefault:
    def test_enqueue_create_raises_when_disabled(self, db_session, product_option, platform, monkeypatch):
        from config.settings import settings

        monkeypatch.setattr(settings, "product_publish_enabled", False)
        svc = ProductPublishService(db_session)
        draft = svc.save_draft(product_option.id, platform.id, **_valid_snapshot_kwargs())
        db_session.flush()

        with pytest.raises(ProductPublishDisabledError):
            svc.enqueue_create(draft.id)
        assert db_session.query(ExternalCommand).count() == 0


class TestSaveDraft:
    def test_partial_draft_can_be_saved(self, db_session, product_option, platform):
        svc = ProductPublishService(db_session)
        draft = svc.save_draft(product_option.id, platform.id, name="이름만")
        assert draft.name == "이름만"
        assert draft.sale_price is None

    def test_rejects_file_scheme_image_url(self, db_session, product_option, platform):
        svc = ProductPublishService(db_session)
        with pytest.raises(ValueError, match="http"):
            svc.save_draft(product_option.id, platform.id, image_urls=["file:///etc/passwd"])

    def test_saving_twice_updates_the_same_draft_row(self, db_session, product_option, platform):
        svc = ProductPublishService(db_session)
        first = svc.save_draft(product_option.id, platform.id, name="A")
        second = svc.save_draft(product_option.id, platform.id, name="B")
        assert first.id == second.id
        assert second.name == "B"


class TestEnqueueCreate:
    def test_blocks_when_already_registered(self, db_session, product_option, platform):
        ProductPlatformMapRepository(db_session).add(
            ProductPlatformMap(product_option_id=product_option.id, platform_id=platform.id, platform_option_id="EXT-1")
        )
        svc = ProductPublishService(db_session)
        draft = svc.save_draft(product_option.id, platform.id, **_valid_snapshot_kwargs())
        with pytest.raises(ProductPublishAlreadyRegisteredError):
            svc.enqueue_create(draft.id)

    def test_duplicate_submission_reuses_existing_command(self, db_session, product_option, platform):
        svc = ProductPublishService(db_session)
        draft = svc.save_draft(product_option.id, platform.id, **_valid_snapshot_kwargs())
        first = svc.enqueue_create(draft.id)
        second = svc.enqueue_create(draft.id)
        assert first.command.id == second.command.id
        assert db_session.query(ExternalCommand).count() == 1

    def test_editing_draft_after_enqueue_does_not_change_pending_command_snapshot(
        self, db_session, product_option, platform
    ):
        import json

        from repositories.integration_sync_repository import ProductPublishCommandDetailRepository

        svc = ProductPublishService(db_session)
        draft = svc.save_draft(product_option.id, platform.id, **_valid_snapshot_kwargs())
        outcome = svc.enqueue_create(draft.id)

        svc.save_draft(product_option.id, platform.id, name="편집된이름")

        detail = ProductPublishCommandDetailRepository(db_session).get_by_command_id(outcome.command.id)
        assert detail is not None
        snapshot = json.loads(detail.snapshot_json)
        assert snapshot["name"] == "테스트상품"  # 편집 전 값 그대로 얼려져 있어야 한다.

    def test_new_snapshot_cancels_older_pending_command(self, db_session, product_option, platform):
        svc = ProductPublishService(db_session)
        draft = svc.save_draft(product_option.id, platform.id, **_valid_snapshot_kwargs())
        first = svc.enqueue_create(draft.id)

        svc.save_draft(product_option.id, platform.id, name="변경된이름")
        second = svc.enqueue_create(draft.id)

        db_session.refresh(first.command)
        assert first.command.status == "CANCELLED"
        assert first.command.error_code == "SUPERSEDED_BY_NEWER_REQUEST"
        assert second.command.id != first.command.id


class TestExecuteCommand:
    def test_success_with_channel_option_id_creates_mapping(self, db_session, product_option, platform):
        connector = StubPublishConnector(
            result=ProductCreateResult(
                accepted=True, platform_result_code="SUCCESS", channel_product_id="ORIGIN-1", channel_option_id="OPT-1"
            )
        )
        svc = ProductPublishService(db_session, connector_factory=_factory(connector))
        draft = svc.save_draft(product_option.id, platform.id, **_valid_snapshot_kwargs())
        outcome = svc.enqueue_create(draft.id)

        result = svc.execute_command(outcome.command.id)

        assert result.command.status == "SUCCESS"
        mapping = ProductPlatformMapRepository(db_session).get_by_option_id(platform.id, "OPT-1")
        assert mapping is not None
        assert mapping.product_option_id == product_option.id
        assert mapping.platform_product_id == "ORIGIN-1"
        db_session.refresh(draft)
        assert draft.registered_at is not None

    def test_success_without_channel_option_id_only_sets_pending_product_id(self, db_session, product_option, platform):
        connector = StubPublishConnector(
            result=ProductCreateResult(
                accepted=True,
                platform_result_code="SUCCESS",
                channel_product_id="SELLER-PRODUCT-1",
                channel_option_id=None,
            )
        )
        svc = ProductPublishService(db_session, connector_factory=_factory(connector))
        draft = svc.save_draft(product_option.id, platform.id, **_valid_snapshot_kwargs())
        outcome = svc.enqueue_create(draft.id)

        svc.execute_command(outcome.command.id)

        assert ProductPlatformMapRepository(db_session).list_by_option(product_option.id) == []
        db_session.refresh(draft)
        assert draft.pending_platform_product_id == "SELLER-PRODUCT-1"
        assert draft.registered_at is None

    def test_unsupported_connector_is_confirmed_failed_without_call(self, db_session, product_option, platform):
        connector = StubPublishConnector()
        connector.supports_product_create = False
        svc = ProductPublishService(db_session, connector_factory=_factory(connector))
        draft = svc.save_draft(product_option.id, platform.id, **_valid_snapshot_kwargs())
        outcome = svc.enqueue_create(draft.id)

        with pytest.raises(MarketplaceCapabilityUnsupportedError):
            svc.execute_command(outcome.command.id)

        db_session.refresh(outcome.command)
        assert outcome.command.status == "FAILED"
        assert connector.calls == []

    def test_validation_error_message_is_preserved_in_error_code_not_just_class_name(
        self, db_session, product_option, platform
    ):
        """create_product()가 필수 항목 누락으로 ValueError를 던지면(예: 네이버
        커넥터의 항목별 안내), error_code에 "ValueError"라는 클래스명만 남기지
        않고 실제 메시지를 그대로 보존해야 한다 - 그렇지 않으면 화면에 "사유:
        ValueError"만 보여 운영자가 어떤 항목이 비었는지 알 수 없다(실제 클릭
        검증으로 발견된 결함)."""
        connector = StubPublishConnector(
            error=ValueError("네이버 상품 등록에 필요한 항목이 비어 있습니다(추측 금지): 카테고리 코드, 판매가")
        )
        svc = ProductPublishService(db_session, connector_factory=_factory(connector))
        draft = svc.save_draft(product_option.id, platform.id, **_valid_snapshot_kwargs())
        outcome = svc.enqueue_create(draft.id)

        with pytest.raises(ValueError):
            svc.execute_command(outcome.command.id)

        db_session.refresh(outcome.command)
        assert outcome.command.status == "FAILED"
        assert (
            outcome.command.error_code
            == "네이버 상품 등록에 필요한 항목이 비어 있습니다(추측 금지): 카테고리 코드, 판매가"
        )

    def test_timeout_ends_as_unknown_and_blocks_successor(self, db_session, product_option, platform):
        connector = StubPublishConnector(error=MarketplaceExternalAPIError("naver", "TIMEOUT", True))
        svc = ProductPublishService(db_session, connector_factory=_factory(connector))
        draft = svc.save_draft(product_option.id, platform.id, **_valid_snapshot_kwargs())
        outcome = svc.enqueue_create(draft.id)

        with pytest.raises(MarketplaceExternalAPIError):
            svc.execute_command(outcome.command.id)
        db_session.refresh(outcome.command)
        assert outcome.command.status == "UNKNOWN"

        # 선행 UNKNOWN이 해소되지 않았으므로, 같은 초안에 대한 재시도(같은 명령 재실행
        # 시도)는 채널을 호출하지 않고 그대로 PENDING 유지해야 한다. execute_command는
        # 같은 command_id를 다시 실행하는 게 아니라, 새로 만든 후속 명령을 대상으로
        # 검사하므로 여기서는 새 명령을 만들어 재현한다.
        calls_before = len(connector.calls)
        svc.save_draft(product_option.id, platform.id, name="다른값")
        second_outcome = svc.enqueue_create(draft.id)
        result = svc.execute_command(second_outcome.command.id)
        assert result.command.status == "PENDING"
        assert len(connector.calls) == calls_before

    def test_already_running_raises_when_claim_fails(self, db_session, product_option, platform):
        connector = StubPublishConnector()
        svc = ProductPublishService(db_session, connector_factory=_factory(connector))
        draft = svc.save_draft(product_option.id, platform.id, **_valid_snapshot_kwargs())
        outcome = svc.enqueue_create(draft.id)
        outcome.command.status = "RUNNING"
        outcome.command.lease_token = "other-worker"
        db_session.flush()

        with pytest.raises(ProductPublishAlreadyRunningError):
            svc.execute_command(outcome.command.id)


class TestRegistrationStatusAndConfirmMapping:
    def test_check_status_then_confirm_mapping(self, db_session, product_option, platform):
        connector = StubPublishConnector(
            result=ProductCreateResult(
                accepted=True, platform_result_code="SUCCESS", channel_product_id="SELLER-1", channel_option_id=None
            ),
            registration_status={"status_name": "APPROVED", "channel_option_ids": ["VENDOR-ITEM-1"]},
        )
        svc = ProductPublishService(db_session, connector_factory=_factory(connector))
        draft = svc.save_draft(product_option.id, platform.id, **_valid_snapshot_kwargs())
        outcome = svc.enqueue_create(draft.id)
        svc.execute_command(outcome.command.id)

        status_result = svc.check_registration_status(draft.id)
        assert status_result["channel_option_ids"] == ["VENDOR-ITEM-1"]

        mapping = svc.confirm_mapping(draft.id, "VENDOR-ITEM-1")
        assert mapping.platform_option_id == "VENDOR-ITEM-1"
        assert mapping.platform_product_id == "SELLER-1"
        db_session.refresh(draft)
        assert draft.pending_platform_product_id is None
        assert draft.registered_at is not None

    def test_confirm_mapping_rejects_option_id_not_in_channel_confirmed_list(
        self, db_session, product_option, platform
    ):
        """운영자가 입력한 옵션 식별자를 그대로 믿지 않는다 - 채널이 실제로 확인해
        준 후보 목록(channel_option_ids)에 없는 값(오타/다른 상품 ID 등)은 매핑을
        만들지 않고 차단해야 한다."""
        connector = StubPublishConnector(
            result=ProductCreateResult(
                accepted=True, platform_result_code="SUCCESS", channel_product_id="SELLER-2", channel_option_id=None
            ),
            registration_status={"status_name": "APPROVED", "channel_option_ids": ["VENDOR-ITEM-REAL"]},
        )
        svc = ProductPublishService(db_session, connector_factory=_factory(connector))
        draft = svc.save_draft(product_option.id, platform.id, **_valid_snapshot_kwargs())
        outcome = svc.enqueue_create(draft.id)
        svc.execute_command(outcome.command.id)

        with pytest.raises(ValueError, match="후보 목록"):
            svc.confirm_mapping(draft.id, "VENDOR-ITEM-WRONG-OR-TYPO")

        from repositories.product_repository import ProductPlatformMapRepository

        assert ProductPlatformMapRepository(db_session).list_by_option(product_option.id) == []
        db_session.refresh(draft)
        assert draft.pending_platform_product_id == "SELLER-2"  # 확정되지 않고 그대로 보류.

    def test_confirm_mapping_rejects_when_no_candidates_confirmed_yet(self, db_session, product_option, platform):
        """아직 심사가 끝나지 않아 채널이 옵션 후보를 하나도 확인해 주지 못한
        상태(channel_option_ids 빈 목록)면, 어떤 값을 입력해도 매핑을 만들면 안
        된다(공식적으로 확인되지 않은 상태를 확정된 것처럼 취급하지 않는다)."""
        connector = StubPublishConnector(
            result=ProductCreateResult(
                accepted=True, platform_result_code="SUCCESS", channel_product_id="SELLER-3", channel_option_id=None
            ),
            registration_status={"status_name": "REVIEWING", "channel_option_ids": []},
        )
        svc = ProductPublishService(db_session, connector_factory=_factory(connector))
        draft = svc.save_draft(product_option.id, platform.id, **_valid_snapshot_kwargs())
        outcome = svc.enqueue_create(draft.id)
        svc.execute_command(outcome.command.id)

        with pytest.raises(ValueError):
            svc.confirm_mapping(draft.id, "GUESSED-ID")

    def test_check_status_without_pending_id_raises(self, db_session, product_option, platform):
        svc = ProductPublishService(db_session)
        draft = svc.save_draft(product_option.id, platform.id, **_valid_snapshot_kwargs())
        with pytest.raises(ValueError):
            svc.check_registration_status(draft.id)

    def test_draft_not_found(self, db_session):
        svc = ProductPublishService(db_session)
        with pytest.raises(ProductPublishDraftNotFoundError):
            svc.check_registration_status(999999)
