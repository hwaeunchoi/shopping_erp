"""
tests/unit/test_product_publish_service.py
------------------------------------------------------
ProductPublishService: 상용 ERP 확장(3단계, 두 번째 묶음) - 신규 상품 등록
(PRODUCT_CREATE)의 outbox/lease/UNKNOWN/중복접수/확인된 외부ID만 매핑 안전성을
검증한다. 실제 채널 API는 호출하지 않는다(스텁 커넥터만 사용).
"""

import pytest

from integrations.malls.base_mall_connector import ProductCreateResult
from integrations.malls.errors import (
    MarketplaceCapabilityUnsupportedError,
    MarketplaceExternalAPIError,
    MarketplaceValidationError,
)
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

    def __init__(self, *, result=None, error=None, registration_status=None, status_check_error=None):
        self._result = result or ProductCreateResult(
            accepted=True, platform_result_code="SUCCESS", channel_product_id="EXT-P-1", channel_option_id="EXT-O-1"
        )
        self._error = error
        self._registration_status = registration_status or {"status_name": "APPROVED", "channel_option_ids": []}
        self._status_check_error = status_check_error
        self.calls: list = []

    def create_product(self, draft_snapshot):
        self.calls.append(draft_snapshot)
        if self._error:
            raise self._error
        return self._result

    def fetch_registration_status(self, platform_product_id):
        if self._status_check_error:
            raise self._status_check_error
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
        """create_product()가 필수 항목 누락으로 MarketplaceValidationError를
        던지면(예: 네이버 커넥터의 항목별 안내 - 이 타입은 필드명·정적 문구만
        담도록 이 코드베이스가 직접 관리하는, 노출해도 안전하다고 확인된 타입),
        error_code에 "ValueError"라는 클래스명만 남기지 않고 실제 메시지를
        그대로 보존해야 한다 - 그렇지 않으면 화면에 "사유: ValueError"만 보여
        운영자가 어떤 항목이 비었는지 알 수 없다(실제 클릭 검증으로 발견된
        결함)."""
        connector = StubPublishConnector(
            error=MarketplaceValidationError(
                "네이버 상품 등록에 필요한 항목이 비어 있습니다(추측 금지): 카테고리 코드, 판매가"
            )
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

    def test_unrecognized_exception_message_is_not_leaked_into_error_code(self, db_session, product_option, platform):
        """create_product()가 우리가 안전하다고 표시하지 않은 예외(bare
        ValueError 포함 - 커넥터 버그나 예상 못한 라이브러리 오류를 흉내낸다)를
        던지면, 그 예외의 원본 메시지(합성 Secret/URL 쿼리/응답 본문을 담고
        있다고 가정)가 error_code에 그대로 저장되면 안 된다 - 500자 절단은
        보호 수단이 아니다(원문이 500자 미만이면 그대로 노출될 것이기 때문).
        안전하다고 확인된 타입(MarketplaceValidationError 등)만 화이트리스트를
        통과한다."""
        synthetic_secret_payload = (
            "internal call failed: url=https://api.example-mall.test/v2/products?"
            "access_token=SECRET_TOKEN_ABC123&vendor_id=SENSITIVE_VENDOR_9 "
            'response_body={"authorization":"Bearer sk_live_synthetic_0001"}'
        )
        connector = StubPublishConnector(error=ValueError(synthetic_secret_payload))
        svc = ProductPublishService(db_session, connector_factory=_factory(connector))
        draft = svc.save_draft(product_option.id, platform.id, **_valid_snapshot_kwargs())
        outcome = svc.enqueue_create(draft.id)

        with pytest.raises(ValueError):
            svc.execute_command(outcome.command.id)

        db_session.refresh(outcome.command)
        assert outcome.command.status == "FAILED"
        assert outcome.command.error_code is not None
        assert "SECRET_TOKEN_ABC123" not in outcome.command.error_code
        assert "sk_live_synthetic_0001" not in outcome.command.error_code
        assert "SENSITIVE_VENDOR_9" not in outcome.command.error_code
        assert "example-mall.test" not in outcome.command.error_code
        assert outcome.command.error_code == "INTERNAL_ERROR:ValueError"

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

    def test_confirm_mapping_blocks_when_status_check_fails(self, db_session, product_option, platform):
        """확정 직전 재조회(fetch_registration_status)가 실패하면(자격증명 문제,
        외부 API 오류 등) 그 예외를 그대로 전파해야 한다 - 조회 실패를 "후보
        없음"이나 "실패했지만 일단 확정"으로 조용히 넘기지 않고, 매핑도 만들지
        않는다(실제 클릭 검증: '쇼핑몰 연결정보를 확인해 주세요' 오류가 뜨고
        매핑 확정 UI 자체가 나타나지 않음을 확인)."""
        from integrations.malls.errors import MarketplaceCredentialMissingError

        connector = StubPublishConnector(
            result=ProductCreateResult(
                accepted=True, platform_result_code="SUCCESS", channel_product_id="SELLER-4", channel_option_id=None
            ),
            status_check_error=MarketplaceCredentialMissingError("coupang"),
        )
        svc = ProductPublishService(db_session, connector_factory=_factory(connector))
        draft = svc.save_draft(product_option.id, platform.id, **_valid_snapshot_kwargs())
        outcome = svc.enqueue_create(draft.id)
        svc.execute_command(outcome.command.id)

        with pytest.raises(MarketplaceCredentialMissingError):
            svc.confirm_mapping(draft.id, "ANY-CANDIDATE")

        from repositories.product_repository import ProductPlatformMapRepository

        assert ProductPlatformMapRepository(db_session).list_by_option(product_option.id) == []
        db_session.refresh(draft)
        assert draft.pending_platform_product_id == "SELLER-4"
        assert draft.registered_at is None

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


def _make_user(db_session, username="etc-confirmer"):
    from models.user import Role, User

    role = Role(name=f"role-{username}")
    db_session.add(role)
    db_session.flush()
    user = User(username=username, password_hash="x", name="테스터", role_id=role.id, is_active=True)
    db_session.add(user)
    db_session.flush()
    return user.id


class TestConfirmEtcNotice:
    """네이버 ETC 카테고리 적합성 "운영자 확인" 감사 기록(confirm_etc_notice) -
    integrations.malls.naver_smartstore_connector 모듈 docstring 참고: 이 확인은
    공식 API가 검증한 결과가 아니라 운영자 확인 기록일 뿐이고, 원시 channel_fields
    JSON에 categoryNoticeTypeConfirmedByOperator를 직접 써넣는 방식만으로는 우회할
    수 없어야 한다."""

    @staticmethod
    def _etc_channel_fields(notice_type: str = "ETC", *, raw_confirmed_flag: bool = False) -> dict:
        return {
            "productInfoProvidedNotice": {
                "productInfoProvidedNoticeType": notice_type,
                "categoryNoticeTypeConfirmedByOperator": raw_confirmed_flag,
                "etc": {},
            }
        }

    def test_confirm_records_confirmer_and_timestamp(self, db_session, product_option, platform):
        user_id = _make_user(db_session)
        svc = ProductPublishService(db_session)
        draft = svc.save_draft(
            product_option.id, platform.id, category_code="50000803", channel_fields=self._etc_channel_fields()
        )
        confirmed = svc.confirm_etc_notice(draft.id, confirmed_by=user_id)
        assert confirmed.etc_notice_confirmed_by == user_id
        assert confirmed.etc_notice_confirmed_at is not None
        assert confirmed.etc_notice_confirmed_category_code == "50000803"
        assert confirmed.etc_notice_confirmed_notice_type == "ETC"

    def test_confirm_rejects_when_notice_type_is_not_etc(self, db_session, product_option, platform):
        user_id = _make_user(db_session)
        svc = ProductPublishService(db_session)
        draft = svc.save_draft(
            product_option.id,
            platform.id,
            category_code="50000803",
            channel_fields=self._etc_channel_fields(notice_type="TOGETHER"),
        )
        with pytest.raises(ValueError, match="ETC"):
            svc.confirm_etc_notice(draft.id, confirmed_by=user_id)

    def test_confirm_rejects_when_category_code_missing(self, db_session, product_option, platform):
        user_id = _make_user(db_session)
        svc = ProductPublishService(db_session)
        draft = svc.save_draft(product_option.id, platform.id, channel_fields=self._etc_channel_fields())
        with pytest.raises(ValueError, match="카테고리"):
            svc.confirm_etc_notice(draft.id, confirmed_by=user_id)

    def test_confirm_draft_not_found(self, db_session):
        user_id = _make_user(db_session)
        svc = ProductPublishService(db_session)
        with pytest.raises(ProductPublishDraftNotFoundError):
            svc.confirm_etc_notice(999999, confirmed_by=user_id)

    def test_changing_category_code_invalidates_prior_confirmation(self, db_session, product_option, platform):
        user_id = _make_user(db_session)
        svc = ProductPublishService(db_session)
        draft = svc.save_draft(
            product_option.id, platform.id, category_code="50000803", channel_fields=self._etc_channel_fields()
        )
        svc.confirm_etc_notice(draft.id, confirmed_by=user_id)

        svc.save_draft(product_option.id, platform.id, category_code="50000999")

        db_session.refresh(draft)
        assert draft.etc_notice_confirmed_at is None
        assert draft.etc_notice_confirmed_by is None
        assert draft.etc_notice_confirmed_category_code is None
        assert draft.etc_notice_confirmed_notice_type is None

    def test_changing_notice_type_invalidates_prior_confirmation(self, db_session, product_option, platform):
        user_id = _make_user(db_session)
        svc = ProductPublishService(db_session)
        draft = svc.save_draft(
            product_option.id, platform.id, category_code="50000803", channel_fields=self._etc_channel_fields()
        )
        svc.confirm_etc_notice(draft.id, confirmed_by=user_id)

        svc.save_draft(product_option.id, platform.id, channel_fields=self._etc_channel_fields(notice_type="TOGETHER"))

        db_session.refresh(draft)
        assert draft.etc_notice_confirmed_at is None

    def test_unrelated_field_save_does_not_invalidate_confirmation(self, db_session, product_option, platform):
        user_id = _make_user(db_session)
        svc = ProductPublishService(db_session)
        draft = svc.save_draft(
            product_option.id, platform.id, category_code="50000803", channel_fields=self._etc_channel_fields()
        )
        svc.confirm_etc_notice(draft.id, confirmed_by=user_id)

        svc.save_draft(product_option.id, platform.id, name="이름만 변경")

        db_session.refresh(draft)
        assert draft.etc_notice_confirmed_at is not None

    def test_snapshot_reflects_tracked_confirmation_not_raw_json_flag(self, db_session, product_option, platform):
        """원시 channel_fields JSON에 categoryNoticeTypeConfirmedByOperator를
        True로 직접 써넣어도(운영자가 화면의 확인 절차를 거치지 않고 JSON
        텍스트만 수정한 경우), confirm_etc_notice()를 호출하지 않았다면
        _draft_snapshot()이 만드는 실제 전송값은 False로 덮어써야 한다(우회
        차단) - 반대로 confirm_etc_notice()를 호출했다면 raw JSON이 False라고
        써 있어도 True로 계산돼야 한다(추적 기록이 유일한 근거)."""
        user_id = _make_user(db_session)
        svc = ProductPublishService(db_session)
        draft = svc.save_draft(
            product_option.id,
            platform.id,
            category_code="50000803",
            channel_fields=self._etc_channel_fields(raw_confirmed_flag=True),
        )
        snapshot = svc._draft_snapshot(draft)
        assert snapshot["channel_fields"]["productInfoProvidedNotice"]["categoryNoticeTypeConfirmedByOperator"] is False

        svc.confirm_etc_notice(draft.id, confirmed_by=user_id)
        draft2 = svc.save_draft(
            product_option.id, platform.id, channel_fields=self._etc_channel_fields(raw_confirmed_flag=False)
        )
        snapshot2 = svc._draft_snapshot(draft2)
        assert snapshot2["channel_fields"]["productInfoProvidedNotice"]["categoryNoticeTypeConfirmedByOperator"] is True
