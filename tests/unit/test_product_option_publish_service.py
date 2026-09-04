"""
tests/unit/test_product_option_publish_service.py
------------------------------------------------------
ProductOptionPublishService: 상용 ERP 확장(3단계, 세 번째 묶음) - 옵션조합 상품
등록(PRODUCT_OPTION_CREATE)의 outbox/lease/UNKNOWN/중복접수/교차등록 차단/
품목별 매핑 확정 안전성을 검증한다. 실제 채널 API는 호출하지 않는다(스텁
커넥터만 사용).
"""

import pytest

from integrations.malls.base_mall_connector import (
    ProductOptionItemResult,
    ProductOptionRegistrationStatus,
    ProductOptionsCreateResult,
)
from integrations.malls.errors import MarketplaceCapabilityUnsupportedError, MarketplaceExternalAPIError
from models.integration_sync import ExternalCommand
from models.product import Product, ProductOption, ProductPlatformMap
from repositories.product_repository import ProductPlatformMapRepository
from services.product_option_publish_service import (
    ProductOptionPublishAlreadyRegisteredError,
    ProductOptionPublishAlreadyRunningError,
    ProductOptionPublishDisabledError,
    ProductOptionPublishDraftNotFoundError,
    ProductOptionPublishItemNotFoundError,
    ProductOptionPublishService,
)


class StubOptionPublishConnector:
    supports_product_option_create = True

    def __init__(self, *, result=None, error=None, registration_status=None, status_check_error=None):
        self._result = result or ProductOptionsCreateResult(
            accepted=True, platform_result_code="SUCCESS", channel_product_id="ORIGIN-1", channel_option_id="CHANNEL-1"
        )
        self._error = error
        self._registration_status = registration_status or ProductOptionRegistrationStatus(
            status_name="APPROVED", items=[]
        )
        self._status_check_error = status_check_error
        self.calls: list = []
        self.status_calls: list = []

    def create_product_with_options(self, draft_snapshot):
        self.calls.append(draft_snapshot)
        if self._error:
            raise self._error
        return self._result

    def fetch_option_registration_status(self, channel_product_id, channel_option_id=None):
        self.status_calls.append((channel_product_id, channel_option_id))
        if self._status_check_error:
            raise self._status_check_error
        return self._registration_status


def _factory(connector):
    return lambda connector_class, session=None, platform_id=None: connector


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "product_option_publish_enabled", True)


@pytest.fixture()
def second_option_same_product(db_session, product_option) -> ProductOption:
    """product_option과 같은 상품(Product)에 속한 두 번째 SKU - 옵션조합 등록의
    "같은 상품, 여러 SKU" 전제를 검증하는 테스트에 쓴다(conftest의
    second_product_option은 서로 다른 상품이라 이 목적에 맞지 않는다)."""
    option = ProductOption(product_id=product_option.product_id, sku_code="TEST-SKU-002", is_active=True)
    db_session.add(option)
    db_session.flush()
    return option


def _valid_group_kwargs() -> dict:
    return {
        "name": "테스트 옵션조합 상품",
        "description_html": "<p>설명</p>",
        "category_code": "50000803",
        "image_urls": ["https://img.example.com/main.jpg"],
        "base_sale_price": 20000,
    }


def _build_draft_with_two_items(svc, db_session, product_option, second_option_same_product, platform):
    draft = svc.save_group_draft(product_option.product_id, platform.id, **_valid_group_kwargs())
    svc.save_item(draft.id, product_option.id, option_values=[["색상", "블랙"]], sale_price=20000, stock_quantity=10)
    svc.save_item(
        draft.id, second_option_same_product.id, option_values=[["색상", "화이트"]], sale_price=21000, stock_quantity=5
    )
    return draft


class TestDisabledByDefault:
    def test_enqueue_create_raises_when_disabled(
        self, db_session, product_option, second_option_same_product, platform, monkeypatch
    ):
        from config.settings import settings

        monkeypatch.setattr(settings, "product_option_publish_enabled", False)
        svc = ProductOptionPublishService(db_session)
        draft = _build_draft_with_two_items(svc, db_session, product_option, second_option_same_product, platform)

        with pytest.raises(ProductOptionPublishDisabledError):
            svc.enqueue_create(draft.id)
        assert db_session.query(ExternalCommand).count() == 0


class TestSaveGroupDraftAndItems:
    def test_partial_group_draft_can_be_saved(self, db_session, product_option, platform):
        svc = ProductOptionPublishService(db_session)
        draft = svc.save_group_draft(product_option.product_id, platform.id, name="이름만")
        assert draft.name == "이름만"
        assert draft.base_sale_price is None

    def test_saving_twice_updates_the_same_draft_row(self, db_session, product_option, platform):
        svc = ProductOptionPublishService(db_session)
        first = svc.save_group_draft(product_option.product_id, platform.id, name="A")
        second = svc.save_group_draft(product_option.product_id, platform.id, name="B")
        assert first.id == second.id
        assert second.name == "B"

    def test_item_defaults_seller_product_code_to_sku_code(self, db_session, product_option, platform):
        svc = ProductOptionPublishService(db_session)
        draft = svc.save_group_draft(product_option.product_id, platform.id, **_valid_group_kwargs())
        item = svc.save_item(draft.id, product_option.id, option_values=[["색상", "블랙"]])
        assert item.seller_product_code == product_option.sku_code

    def test_explicit_seller_product_code_is_preserved_on_resave(self, db_session, product_option, platform):
        svc = ProductOptionPublishService(db_session)
        draft = svc.save_group_draft(product_option.product_id, platform.id, **_valid_group_kwargs())
        svc.save_item(draft.id, product_option.id, seller_product_code="CUSTOM-CODE")
        item = svc.save_item(draft.id, product_option.id, sale_price=1000)
        assert item.seller_product_code == "CUSTOM-CODE"

    def test_rejects_option_from_another_product(self, db_session, product_option, platform):
        other_product = Product(name="다른 상품", category="테스트", base_price=5000, status="ACTIVE")
        db_session.add(other_product)
        db_session.flush()
        other_option = ProductOption(product_id=other_product.id, sku_code="OTHER-SKU-001", is_active=True)
        db_session.add(other_option)
        db_session.flush()

        svc = ProductOptionPublishService(db_session)
        draft = svc.save_group_draft(product_option.product_id, platform.id, **_valid_group_kwargs())
        with pytest.raises(ValueError, match="속하지 않습니다"):
            svc.save_item(draft.id, other_option.id)

    def test_delete_item(self, db_session, product_option, platform):
        svc = ProductOptionPublishService(db_session)
        draft = svc.save_group_draft(product_option.product_id, platform.id, **_valid_group_kwargs())
        item = svc.save_item(draft.id, product_option.id)
        svc.delete_item(draft.id, item.id)
        with pytest.raises(ProductOptionPublishItemNotFoundError):
            svc.delete_item(draft.id, item.id)


class TestEnqueueCreate:
    def test_blocks_when_no_items(self, db_session, product_option, platform):
        svc = ProductOptionPublishService(db_session)
        draft = svc.save_group_draft(product_option.product_id, platform.id, **_valid_group_kwargs())
        with pytest.raises(ValueError, match="품목"):
            svc.enqueue_create(draft.id)

    def test_blocks_when_any_item_already_registered(
        self, db_session, product_option, second_option_same_product, platform
    ):
        ProductPlatformMapRepository(db_session).add(
            ProductPlatformMap(
                product_option_id=second_option_same_product.id, platform_id=platform.id, platform_option_id="EXT-1"
            )
        )
        svc = ProductOptionPublishService(db_session)
        draft = _build_draft_with_two_items(svc, db_session, product_option, second_option_same_product, platform)
        with pytest.raises(ProductOptionPublishAlreadyRegisteredError):
            svc.enqueue_create(draft.id)

    def test_duplicate_submission_reuses_existing_command(
        self, db_session, product_option, second_option_same_product, platform
    ):
        svc = ProductOptionPublishService(db_session)
        draft = _build_draft_with_two_items(svc, db_session, product_option, second_option_same_product, platform)
        first = svc.enqueue_create(draft.id)
        second = svc.enqueue_create(draft.id)
        assert first.command.id == second.command.id
        assert db_session.query(ExternalCommand).count() == 1

    def test_editing_draft_after_enqueue_does_not_change_pending_command_snapshot(
        self, db_session, product_option, second_option_same_product, platform
    ):
        import json

        from repositories.integration_sync_repository import ProductOptionPublishCommandDetailRepository

        svc = ProductOptionPublishService(db_session)
        draft = _build_draft_with_two_items(svc, db_session, product_option, second_option_same_product, platform)
        outcome = svc.enqueue_create(draft.id)

        svc.save_group_draft(product_option.product_id, platform.id, name="편집된이름")

        detail = ProductOptionPublishCommandDetailRepository(db_session).get_by_command_id(outcome.command.id)
        assert detail is not None
        snapshot = json.loads(detail.snapshot_json)
        assert snapshot["name"] == "테스트 옵션조합 상품"
        assert len(snapshot["items"]) == 2

    def test_new_snapshot_cancels_older_pending_command(
        self, db_session, product_option, second_option_same_product, platform
    ):
        svc = ProductOptionPublishService(db_session)
        draft = _build_draft_with_two_items(svc, db_session, product_option, second_option_same_product, platform)
        first = svc.enqueue_create(draft.id)

        svc.save_group_draft(product_option.product_id, platform.id, name="변경된이름")
        second = svc.enqueue_create(draft.id)

        db_session.refresh(first.command)
        assert first.command.status == "CANCELLED"
        assert second.command.id != first.command.id


class TestExecuteCommand:
    def test_success_sets_shared_identifiers_without_creating_item_mappings(
        self, db_session, product_option, second_option_same_product, platform
    ):
        connector = StubOptionPublishConnector()
        svc = ProductOptionPublishService(db_session, connector_factory=_factory(connector))
        draft = _build_draft_with_two_items(svc, db_session, product_option, second_option_same_product, platform)
        outcome = svc.enqueue_create(draft.id)

        result = svc.execute_command(outcome.command.id)

        assert result.command.status == "SUCCESS"
        db_session.refresh(draft)
        assert draft.channel_product_id == "ORIGIN-1"
        assert draft.channel_option_id == "CHANNEL-1"
        assert draft.registered_at is not None
        # 두 채널 모두 등록 응답에는 품목별 식별자가 없다 - 매핑은 아직 만들어지지 않아야 한다.
        assert ProductPlatformMapRepository(db_session).list_by_option(product_option.id) == []
        assert ProductPlatformMapRepository(db_session).list_by_option(second_option_same_product.id) == []

    def test_unsupported_connector_is_confirmed_failed_without_call(
        self, db_session, product_option, second_option_same_product, platform
    ):
        connector = StubOptionPublishConnector()
        connector.supports_product_option_create = False
        svc = ProductOptionPublishService(db_session, connector_factory=_factory(connector))
        draft = _build_draft_with_two_items(svc, db_session, product_option, second_option_same_product, platform)
        outcome = svc.enqueue_create(draft.id)

        with pytest.raises(MarketplaceCapabilityUnsupportedError):
            svc.execute_command(outcome.command.id)
        db_session.refresh(outcome.command)
        assert outcome.command.status == "FAILED"
        assert connector.calls == []

    def test_timeout_ends_as_unknown_and_blocks_successor(
        self, db_session, product_option, second_option_same_product, platform
    ):
        connector = StubOptionPublishConnector(error=MarketplaceExternalAPIError("naver", "TIMEOUT", True))
        svc = ProductOptionPublishService(db_session, connector_factory=_factory(connector))
        draft = _build_draft_with_two_items(svc, db_session, product_option, second_option_same_product, platform)
        outcome = svc.enqueue_create(draft.id)

        with pytest.raises(MarketplaceExternalAPIError):
            svc.execute_command(outcome.command.id)
        db_session.refresh(outcome.command)
        assert outcome.command.status == "UNKNOWN"

        calls_before = len(connector.calls)
        svc.save_group_draft(product_option.product_id, platform.id, name="다른값")
        second_outcome = svc.enqueue_create(draft.id)
        result = svc.execute_command(second_outcome.command.id)
        assert result.command.status == "PENDING"
        assert len(connector.calls) == calls_before

    def test_already_running_raises_when_claim_fails(
        self, db_session, product_option, second_option_same_product, platform
    ):
        connector = StubOptionPublishConnector()
        svc = ProductOptionPublishService(db_session, connector_factory=_factory(connector))
        draft = _build_draft_with_two_items(svc, db_session, product_option, second_option_same_product, platform)
        outcome = svc.enqueue_create(draft.id)
        outcome.command.status = "RUNNING"
        outcome.command.lease_token = "other-worker"
        db_session.flush()

        with pytest.raises(ProductOptionPublishAlreadyRunningError):
            svc.execute_command(outcome.command.id)


class TestCheckRegistrationStatusAndConfirmMapping:
    def test_unambiguous_matches_are_auto_mapped_and_overall_status_progresses(
        self, db_session, product_option, second_option_same_product, platform
    ):
        connector = StubOptionPublishConnector()
        svc = ProductOptionPublishService(db_session, connector_factory=_factory(connector))
        draft = _build_draft_with_two_items(svc, db_session, product_option, second_option_same_product, platform)
        outcome = svc.enqueue_create(draft.id)
        svc.execute_command(outcome.command.id)

        # 아직 채널이 아무 후보도 확인해 주지 않은 상태(예: 심사 중).
        connector._registration_status = ProductOptionRegistrationStatus(status_name="IN_REVIEW", items=[])
        status1 = svc.check_registration_status(draft.id)
        assert status1["overall_status"] == "PENDING_REVIEW"
        assert all(not i["mapped"] for i in status1["items"])

        # 첫 번째 SKU만 승인 확인됨(쿠팡 vendorItemId 확정).
        connector._registration_status = ProductOptionRegistrationStatus(
            status_name="APPROVED",
            items=[ProductOptionItemResult(seller_product_code=product_option.sku_code, channel_option_id="VI-1")],
        )
        status2 = svc.check_registration_status(draft.id)
        assert status2["overall_status"] == "PARTIALLY_MAPPED"
        mapping = ProductPlatformMapRepository(db_session).get_by_option_id(platform.id, "VI-1")
        assert mapping is not None
        assert mapping.product_option_id == product_option.id
        assert mapping.platform_product_id == "ORIGIN-1"  # 쿠팡: sellerProductId(공유).

        # 두 번째 SKU도 승인 확인됨 -> 전부 확정.
        connector._registration_status = ProductOptionRegistrationStatus(
            status_name="APPROVED",
            items=[
                ProductOptionItemResult(seller_product_code=product_option.sku_code, channel_option_id="VI-1"),
                ProductOptionItemResult(
                    seller_product_code=second_option_same_product.sku_code, channel_option_id="VI-2"
                ),
            ],
        )
        status3 = svc.check_registration_status(draft.id)
        assert status3["overall_status"] == "FULLY_MAPPED"
        assert all(i["mapped"] for i in status3["items"])
        by_option = {i["product_option_id"]: i["channel_option_id"] for i in status3["items"]}
        assert by_option[product_option.id] == "VI-1"
        assert by_option[second_option_same_product.id] == "VI-2"

    def test_already_mapped_item_still_shows_its_identifier_when_channel_no_longer_lists_it(
        self, db_session, product_option, second_option_same_product, platform
    ):
        """실제 클릭 검증으로 발견된 결함: 이미 매핑된 SKU라도, 이번 호출의 채널
        응답에 그 판매자 관리코드가 다시 나타나지 않으면(승인 후 조회 목록에서
        빠지는 등) channel_option_id를 빈 값으로 보여주면 안 된다 - 매핑이 이미
        존재한다는 사실 자체를 근거로 그 매핑에 저장된 식별자를 그대로 돌려줘야
        한다."""
        connector = StubOptionPublishConnector(
            registration_status=ProductOptionRegistrationStatus(
                status_name="APPROVED",
                items=[ProductOptionItemResult(seller_product_code=product_option.sku_code, channel_option_id="VI-1")],
            )
        )
        svc = ProductOptionPublishService(db_session, connector_factory=_factory(connector))
        draft = _build_draft_with_two_items(svc, db_session, product_option, second_option_same_product, platform)
        outcome = svc.enqueue_create(draft.id)
        svc.execute_command(outcome.command.id)
        first = svc.check_registration_status(draft.id)
        assert first["overall_status"] == "PARTIALLY_MAPPED"

        # 두 번째 호출에서는 채널 응답에 이 코드가 더 이상 없다(승인 목록 갱신 등).
        connector._registration_status = ProductOptionRegistrationStatus(status_name="APPROVED", items=[])
        second = svc.check_registration_status(draft.id)
        item = next(i for i in second["items"] if i["product_option_id"] == product_option.id)
        assert item["mapped"] is True
        assert item["channel_option_id"] == "VI-1"

    def test_ambiguous_candidate_is_left_unmapped(
        self, db_session, product_option, second_option_same_product, platform
    ):
        connector = StubOptionPublishConnector(
            registration_status=ProductOptionRegistrationStatus(
                status_name="APPROVED",
                items=[
                    ProductOptionItemResult(seller_product_code=product_option.sku_code, channel_option_id="VI-1"),
                    ProductOptionItemResult(seller_product_code=product_option.sku_code, channel_option_id="VI-1-DUP"),
                ],
            )
        )
        svc = ProductOptionPublishService(db_session, connector_factory=_factory(connector))
        draft = _build_draft_with_two_items(svc, db_session, product_option, second_option_same_product, platform)
        outcome = svc.enqueue_create(draft.id)
        svc.execute_command(outcome.command.id)

        status = svc.check_registration_status(draft.id)
        item = next(i for i in status["items"] if i["product_option_id"] == product_option.id)
        assert item["mapped"] is False
        assert item["ambiguous"] is True
        assert ProductPlatformMapRepository(db_session).get_by_option_id(platform.id, "VI-1") is None

    def test_naver_combination_id_is_namespaced_to_avoid_collision(
        self, db_session, product_option, second_option_same_product, naver_platform
    ):
        connector = StubOptionPublishConnector(
            registration_status=ProductOptionRegistrationStatus(
                status_name="SALE",
                items=[ProductOptionItemResult(seller_product_code=product_option.sku_code, channel_option_id="111")],
            )
        )
        svc = ProductOptionPublishService(db_session, connector_factory=_factory(connector))
        draft = _build_draft_with_two_items(svc, db_session, product_option, second_option_same_product, naver_platform)
        outcome = svc.enqueue_create(draft.id)
        svc.execute_command(outcome.command.id)

        svc.check_registration_status(draft.id)

        mappings = ProductPlatformMapRepository(db_session).list_by_option(product_option.id)
        assert len(mappings) == 1
        assert mappings[0].platform_option_id == "COMBO-ORIGIN-1-111"
        assert mappings[0].platform_product_id == "CHANNEL-1"  # 네이버: channelProductNo(공유).
        assert mappings[0].platform_origin_product_id == "ORIGIN-1"

    def test_confirm_item_mapping_rejects_id_not_confirmed_for_this_sku(
        self, db_session, product_option, second_option_same_product, platform
    ):
        connector = StubOptionPublishConnector(
            registration_status=ProductOptionRegistrationStatus(
                status_name="APPROVED",
                items=[
                    ProductOptionItemResult(
                        seller_product_code=second_option_same_product.sku_code, channel_option_id="VI-2"
                    )
                ],
            )
        )
        svc = ProductOptionPublishService(db_session, connector_factory=_factory(connector))
        draft = _build_draft_with_two_items(svc, db_session, product_option, second_option_same_product, platform)
        outcome = svc.enqueue_create(draft.id)
        svc.execute_command(outcome.command.id)

        with pytest.raises(ValueError, match="후보 목록"):
            svc.confirm_item_mapping(draft.id, product_option.id, "VI-2")  # 다른 SKU의 후보다.
        assert ProductPlatformMapRepository(db_session).list_by_option(product_option.id) == []

    def test_confirm_item_mapping_succeeds_with_verified_candidate(
        self, db_session, product_option, second_option_same_product, platform
    ):
        connector = StubOptionPublishConnector(
            registration_status=ProductOptionRegistrationStatus(
                status_name="APPROVED",
                items=[ProductOptionItemResult(seller_product_code=product_option.sku_code, channel_option_id="VI-1")],
            )
        )
        svc = ProductOptionPublishService(db_session, connector_factory=_factory(connector))
        draft = _build_draft_with_two_items(svc, db_session, product_option, second_option_same_product, platform)
        outcome = svc.enqueue_create(draft.id)
        svc.execute_command(outcome.command.id)

        mapping = svc.confirm_item_mapping(draft.id, product_option.id, "VI-1")
        assert mapping.platform_option_id == "VI-1"

        with pytest.raises(ValueError, match="이미 매핑"):
            svc.confirm_item_mapping(draft.id, product_option.id, "VI-1")

    def test_check_status_without_registration_raises(
        self, db_session, product_option, second_option_same_product, platform
    ):
        svc = ProductOptionPublishService(db_session)
        draft = _build_draft_with_two_items(svc, db_session, product_option, second_option_same_product, platform)
        with pytest.raises(ValueError, match="등록 접수"):
            svc.check_registration_status(draft.id)

    def test_draft_not_found(self, db_session):
        svc = ProductOptionPublishService(db_session)
        with pytest.raises(ProductOptionPublishDraftNotFoundError):
            svc.check_registration_status(999999)
