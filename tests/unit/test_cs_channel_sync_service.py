"""
tests/unit/test_cs_channel_sync_service.py
----------------------------------------------
services.cs_channel_sync_service.CsChannelSyncService - 채널 CS(고객문의) 조회
동기화 검증. 실제 채널 API는 호출하지 않는다(스텁 커넥터만 사용).
"""

import uuid
from datetime import date, datetime, timezone
from typing import Any, Optional

import pytest

from integrations.malls.errors import (
    MarketplaceCapabilityUnsupportedError,
    MarketplaceCredentialMissingError,
    MarketplaceExternalAPIError,
    RequestBudget,
)
from models.order import Order
from repositories.cs_case_repository import CsCaseRepository
from services.cs_channel_sync_service import (
    COUPANG_CALL_CENTER_SOURCE,
    COUPANG_PRODUCT_INQUIRY_SOURCE,
    CsChannelSyncService,
)


class _StubConnector:
    """fetch_inquiries/fetch_product_inquiries는 실제 CoupangConnector와 동일하게
    max_pages/max_retries/request_budget을 키워드 전용으로 받는다(상용 ERP 확장
    5단계 B묶음 보완 - CS 문의 호출량 안전 상한). 이 스텁은 실제 HTTP 요청이
    없으므로 request_budget을 소비하지는 않지만, 호출부(CsChannelSyncService)가
    실제로 그 값을 넘기는지는 call_kwargs/product_call_kwargs로 검증할 수 있다."""

    supports_inquiry_sync = True
    supports_product_inquiry_sync = True

    def __init__(
        self,
        items: Optional[list[dict[str, Any]]] = None,
        error: Optional[Exception] = None,
        product_items: Optional[list[dict[str, Any]]] = None,
        product_error: Optional[Exception] = None,
    ) -> None:
        self.items = items or []
        self.error = error
        self.product_items = product_items or []
        self.product_error = product_error
        self.called_with: Optional[tuple[date, date]] = None
        self.product_called_with: Optional[tuple[date, date]] = None
        self.call_kwargs: dict[str, Any] = {}
        self.product_call_kwargs: dict[str, Any] = {}

    def fetch_inquiries(
        self,
        start_date: date,
        end_date: date,
        *,
        max_pages: Optional[int] = None,
        max_retries: Optional[int] = None,
        request_budget: Optional[Any] = None,
    ) -> list[dict[str, Any]]:
        self.called_with = (start_date, end_date)
        self.call_kwargs = {"max_pages": max_pages, "max_retries": max_retries, "request_budget": request_budget}
        if self.error is not None:
            raise self.error
        return self.items

    def fetch_product_inquiries(
        self,
        start_date: date,
        end_date: date,
        *,
        max_pages: Optional[int] = None,
        max_retries: Optional[int] = None,
        request_budget: Optional[Any] = None,
    ) -> list[dict[str, Any]]:
        self.product_called_with = (start_date, end_date)
        self.product_call_kwargs = {
            "max_pages": max_pages,
            "max_retries": max_retries,
            "request_budget": request_budget,
        }
        if self.product_error is not None:
            raise self.product_error
        return self.product_items


class _UnsupportedConnector:
    supports_inquiry_sync = False
    supports_product_inquiry_sync = False

    def fetch_inquiries(self, start_date: date, end_date: date, **_kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("supports_inquiry_sync=False인 커넥터의 fetch_inquiries는 호출되면 안 된다.")

    def fetch_product_inquiries(self, start_date: date, end_date: date, **_kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError(
            "supports_product_inquiry_sync=False인 커넥터의 fetch_product_inquiries는 호출되면 안 된다."
        )


def _inquiry(
    inquiry_id: str = "1001",
    content: str = "배송이 늦어요",
    order_no: Optional[str] = None,
    inquiry_at: Optional[datetime] = None,
    raw_status: str = "progress:requestAnswer",
) -> dict[str, Any]:
    return {
        "platform_inquiry_id": inquiry_id,
        "content": content,
        "inquiry_at": inquiry_at or datetime(2026, 1, 10, 9, 0, 0),
        "raw_status": raw_status,
        "needs_answer": True,
        "platform_order_no": order_no,
        "customer_phone": "010-1234-5678",
    }


@pytest.fixture(autouse=True)
def _enable_cs_inquiry_sync(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "cs_inquiry_sync_enabled", True)


def _make_order(db_session, platform, order_no: str) -> Order:
    order = Order(
        platform_id=platform.id,
        platform_order_no=order_no,
        status="NEW",
        order_date=datetime.now(timezone.utc),
        total_amount=10000,
    )
    db_session.add(order)
    db_session.flush()
    return order


class TestSyncInquiries:
    def test_disabled_flag_makes_zero_external_calls(self, db_session, platform, monkeypatch):
        from config.settings import settings

        monkeypatch.setattr(settings, "cs_inquiry_sync_enabled", False)
        connector = _StubConnector(items=[_inquiry()])
        service = CsChannelSyncService(db_session)

        result = service.sync_inquiries(connector, platform.id, date(2026, 1, 1), date(2026, 1, 7))

        assert result["status"] == "DISABLED"
        assert connector.called_with is None

    def test_unsupported_connector_not_called(self, db_session, platform):
        connector = _UnsupportedConnector()
        service = CsChannelSyncService(db_session)

        result = service.sync_inquiries(connector, platform.id, date(2026, 1, 1), date(2026, 1, 7))

        assert result["status"] == "UNSUPPORTED"

    def test_creates_new_case(self, db_session, platform):
        connector = _StubConnector(items=[_inquiry()])
        service = CsChannelSyncService(db_session)

        result = service.sync_inquiries(connector, platform.id, date(2026, 1, 1), date(2026, 1, 7))

        assert result == {"status": "SUCCESS", "created": 1, "updated": 0, "failed": 0}
        case = CsCaseRepository(db_session).get_by_external(platform.id, COUPANG_CALL_CENTER_SOURCE, "1001")
        assert case is not None
        assert case.status == "OPEN"
        assert case.external_source == "COUPANG_CALL_CENTER"
        assert case.external_raw_status == "progress:requestAnswer"
        assert case.customer_message == "배송이 늦어요"

    def test_links_matching_order(self, db_session, platform):
        order = _make_order(db_session, platform, f"CSSYNC-{uuid.uuid4().hex[:8]}")
        connector = _StubConnector(items=[_inquiry(order_no=order.platform_order_no)])
        service = CsChannelSyncService(db_session)

        service.sync_inquiries(connector, platform.id, date(2026, 1, 1), date(2026, 1, 7))

        case = CsCaseRepository(db_session).get_by_external(platform.id, COUPANG_CALL_CENTER_SOURCE, "1001")
        assert case is not None
        assert case.order_id == order.id

    def test_resync_does_not_create_duplicate(self, db_session, platform):
        connector = _StubConnector(items=[_inquiry()])
        service = CsChannelSyncService(db_session)
        service.sync_inquiries(connector, platform.id, date(2026, 1, 1), date(2026, 1, 7))

        result = service.sync_inquiries(connector, platform.id, date(2026, 1, 1), date(2026, 1, 7))

        assert result == {"status": "SUCCESS", "created": 0, "updated": 1, "failed": 0}
        repo = CsCaseRepository(db_session)
        matches = [c for c in repo.list_all(limit=100) if c.external_inquiry_id == "1001"]
        assert len(matches) == 1

    def test_resync_preserves_local_assignee_and_tags(self, db_session, platform):
        connector = _StubConnector(items=[_inquiry()])
        service = CsChannelSyncService(db_session)
        service.sync_inquiries(connector, platform.id, date(2026, 1, 1), date(2026, 1, 7))
        repo = CsCaseRepository(db_session)
        case = repo.get_by_external(platform.id, COUPANG_CALL_CENTER_SOURCE, "1001")
        assert case is not None
        case.assignee_id = None  # 담당자 미배정 상태에서 태그만 먼저 달아본다.
        case.tags = "긴급,VIP"
        case.reply_draft = "고객님께 안내드릴 초안"
        db_session.flush()

        service.sync_inquiries(connector, platform.id, date(2026, 1, 1), date(2026, 1, 7))

        refreshed = repo.get_by_external(platform.id, COUPANG_CALL_CENTER_SOURCE, "1001")
        assert refreshed is not None
        assert refreshed.tags == "긴급,VIP"
        assert refreshed.reply_draft == "고객님께 안내드릴 초안"

    def test_resync_updates_last_customer_message_at_on_newer_content(self, db_session, platform):
        connector = _StubConnector(items=[_inquiry(inquiry_at=datetime(2026, 1, 10, 9, 0, 0))])
        service = CsChannelSyncService(db_session)
        service.sync_inquiries(connector, platform.id, date(2026, 1, 1), date(2026, 1, 7))

        newer_connector = _StubConnector(
            items=[_inquiry(content="추가로 문의드립니다", inquiry_at=datetime(2026, 1, 12, 9, 0, 0))]
        )
        service.sync_inquiries(newer_connector, platform.id, date(2026, 1, 1), date(2026, 1, 12))

        case = CsCaseRepository(db_session).get_by_external(platform.id, COUPANG_CALL_CENTER_SOURCE, "1001")
        assert case is not None
        assert case.last_customer_message_at == datetime(2026, 1, 12, 9, 0, 0)
        assert case.customer_message == "추가로 문의드립니다"

    def test_credential_missing_reported_as_failed(self, db_session, platform):
        connector = _StubConnector(error=MarketplaceCredentialMissingError("coupang"))
        service = CsChannelSyncService(db_session)

        result = service.sync_inquiries(connector, platform.id, date(2026, 1, 1), date(2026, 1, 7))

        assert result["status"] == "FAILED"
        assert result["reason_code"] == "CREDENTIAL_MISSING"

    def test_external_api_error_reported_with_reason_code(self, db_session, platform):
        connector = _StubConnector(error=MarketplaceExternalAPIError("coupang", "RATE_LIMITED", retryable=True))
        service = CsChannelSyncService(db_session)

        result = service.sync_inquiries(connector, platform.id, date(2026, 1, 1), date(2026, 1, 7))

        assert result["status"] == "FAILED"
        assert result["reason_code"] == "RATE_LIMITED"

    def test_capability_unsupported_raised_by_connector_itself(self, db_session, platform):
        """supports_inquiry_sync=True라고 스스로 주장하지만 실제 fetch_inquiries가
        미지원 예외를 던지는 방어적 케이스도 안전하게 UNSUPPORTED로 처리해야 한다."""
        connector = _StubConnector(error=MarketplaceCapabilityUnsupportedError("coupang", "inquiry_sync"))
        service = CsChannelSyncService(db_session)

        result = service.sync_inquiries(connector, platform.id, date(2026, 1, 1), date(2026, 1, 7))

        assert result["status"] == "UNSUPPORTED"

    def test_customer_message_is_never_logged(self, db_session, platform, caplog):
        connector = _StubConnector(items=[_inquiry(content="이 문자열은 로그에 나오면 안 된다-SECRET-MARKER")])
        service = CsChannelSyncService(db_session)

        with caplog.at_level("DEBUG"):
            service.sync_inquiries(connector, platform.id, date(2026, 1, 1), date(2026, 1, 7))

        assert "SECRET-MARKER" not in caplog.text

    def test_product_inquiry_source_creates_case_with_product_inquiry_type(self, db_session, platform):
        connector = _StubConnector(product_items=[_inquiry(inquiry_id="5001", content="재입고 문의")])
        service = CsChannelSyncService(db_session)

        result = service.sync_inquiries(
            connector, platform.id, date(2026, 1, 1), date(2026, 1, 7), source=COUPANG_PRODUCT_INQUIRY_SOURCE
        )

        assert result == {"status": "SUCCESS", "created": 1, "updated": 0, "failed": 0}
        case = CsCaseRepository(db_session).get_by_external(platform.id, COUPANG_PRODUCT_INQUIRY_SOURCE, "5001")
        assert case is not None
        assert case.external_source == COUPANG_PRODUCT_INQUIRY_SOURCE
        assert case.inquiry_type == "PRODUCT"


class TestCrossSourceDedup:
    """콜센터 문의와 상품별 문의의 inquiryId가 우연히 같아도 서로 다른 케이스로
    생성돼야 한다(models.cs_case.CsCase 클래스 docstring에 기록된 실제 버그의
    회귀 테스트) - external_source가 dedup 키에 포함돼야만 통과한다."""

    def test_same_numeric_id_in_two_sources_creates_two_separate_cases(self, db_session, platform):
        connector = _StubConnector(
            items=[_inquiry(inquiry_id="7777", content="콜센터 문의 내용")],
            product_items=[_inquiry(inquiry_id="7777", content="상품별 문의 내용")],
        )
        service = CsChannelSyncService(db_session)

        call_center_result = service.sync_inquiries(
            connector, platform.id, date(2026, 1, 1), date(2026, 1, 7), source=COUPANG_CALL_CENTER_SOURCE
        )
        product_result = service.sync_inquiries(
            connector, platform.id, date(2026, 1, 1), date(2026, 1, 7), source=COUPANG_PRODUCT_INQUIRY_SOURCE
        )

        assert call_center_result["created"] == 1
        assert product_result["created"] == 1

        call_center_case = CsCaseRepository(db_session).get_by_external(platform.id, COUPANG_CALL_CENTER_SOURCE, "7777")
        product_case = CsCaseRepository(db_session).get_by_external(platform.id, COUPANG_PRODUCT_INQUIRY_SOURCE, "7777")
        assert call_center_case is not None
        assert product_case is not None
        assert call_center_case.id != product_case.id
        assert call_center_case.customer_message == "콜센터 문의 내용"
        assert product_case.customer_message == "상품별 문의 내용"

    def test_resync_with_same_id_in_two_sources_updates_each_independently(self, db_session, platform):
        """재수집 시에도 서로의 데이터를 덮어쓰지 않는다."""
        connector = _StubConnector(
            items=[_inquiry(inquiry_id="8888", content="콜센터 최초")],
            product_items=[_inquiry(inquiry_id="8888", content="상품별 최초")],
        )
        service = CsChannelSyncService(db_session)
        service.sync_inquiries(
            connector, platform.id, date(2026, 1, 1), date(2026, 1, 7), source=COUPANG_CALL_CENTER_SOURCE
        )
        service.sync_inquiries(
            connector, platform.id, date(2026, 1, 1), date(2026, 1, 7), source=COUPANG_PRODUCT_INQUIRY_SOURCE
        )

        newer_connector = _StubConnector(
            items=[_inquiry(inquiry_id="8888", content="콜센터 갱신", inquiry_at=datetime(2026, 1, 12, 9, 0, 0))],
            product_items=[
                _inquiry(inquiry_id="8888", content="상품별 갱신", inquiry_at=datetime(2026, 1, 12, 9, 0, 0))
            ],
        )
        service.sync_inquiries(
            newer_connector, platform.id, date(2026, 1, 1), date(2026, 1, 12), source=COUPANG_CALL_CENTER_SOURCE
        )
        service.sync_inquiries(
            newer_connector, platform.id, date(2026, 1, 1), date(2026, 1, 12), source=COUPANG_PRODUCT_INQUIRY_SOURCE
        )

        call_center_case = CsCaseRepository(db_session).get_by_external(platform.id, COUPANG_CALL_CENTER_SOURCE, "8888")
        product_case = CsCaseRepository(db_session).get_by_external(platform.id, COUPANG_PRODUCT_INQUIRY_SOURCE, "8888")
        assert call_center_case is not None
        assert product_case is not None
        assert call_center_case.customer_message == "콜센터 갱신"
        assert product_case.customer_message == "상품별 갱신"


class TestSyncAllInquiries:
    def test_disabled_flag_makes_zero_external_calls(self, db_session, platform, monkeypatch):
        from config.settings import settings

        monkeypatch.setattr(settings, "cs_inquiry_sync_enabled", False)
        connector = _StubConnector(items=[_inquiry()], product_items=[_inquiry(inquiry_id="9999")])
        service = CsChannelSyncService(db_session)

        result = service.sync_all_inquiries(connector, platform.id, date(2026, 1, 1), date(2026, 1, 7))

        assert result["status"] == "DISABLED"
        assert connector.called_with is None
        assert connector.product_called_with is None

    def test_both_sources_succeed_combines_totals(self, db_session, platform):
        connector = _StubConnector(items=[_inquiry(inquiry_id="1")], product_items=[_inquiry(inquiry_id="2")])
        service = CsChannelSyncService(db_session)

        result = service.sync_all_inquiries(connector, platform.id, date(2026, 1, 1), date(2026, 1, 7))

        assert result["status"] == "SUCCESS"
        assert result["created"] == 2
        assert result["by_source"][COUPANG_CALL_CENTER_SOURCE]["created"] == 1
        assert result["by_source"][COUPANG_PRODUCT_INQUIRY_SOURCE]["created"] == 1

    def test_one_source_failure_does_not_hide_other_source_success(self, db_session, platform):
        """한 소스의 조회 실패가 다른 소스의 성공 결과를 숨기거나 지우지 않는다."""
        connector = _StubConnector(
            items=[_inquiry(inquiry_id="1")],
            product_error=MarketplaceExternalAPIError("coupang", "RATE_LIMITED", retryable=True),
        )
        service = CsChannelSyncService(db_session)

        result = service.sync_all_inquiries(connector, platform.id, date(2026, 1, 1), date(2026, 1, 7))

        assert result["status"] == "PARTIAL_SUCCESS"
        assert result["created"] == 1
        assert result["by_source"][COUPANG_CALL_CENTER_SOURCE]["status"] == "SUCCESS"
        assert result["by_source"][COUPANG_PRODUCT_INQUIRY_SOURCE]["status"] == "FAILED"
        assert result["by_source"][COUPANG_PRODUCT_INQUIRY_SOURCE]["reason_code"] == "RATE_LIMITED"
        # 성공한 소스가 만든 케이스는 실제로 커밋돼 있어야 한다(실패 소스에 의해 지워지지 않음).
        case = CsCaseRepository(db_session).get_by_external(platform.id, COUPANG_CALL_CENTER_SOURCE, "1")
        assert case is not None

    def test_one_source_unsupported_other_succeeds(self, db_session, platform):
        class _CallCenterOnlyConnector(_StubConnector):
            supports_product_inquiry_sync = False

        connector = _CallCenterOnlyConnector(items=[_inquiry(inquiry_id="1")])
        service = CsChannelSyncService(db_session)

        result = service.sync_all_inquiries(connector, platform.id, date(2026, 1, 1), date(2026, 1, 7))

        assert result["status"] == "SUCCESS"
        assert result["created"] == 1
        assert result["by_source"][COUPANG_PRODUCT_INQUIRY_SOURCE]["status"] == "UNSUPPORTED"

    def test_both_sources_unsupported(self, db_session, platform):
        connector = _UnsupportedConnector()
        service = CsChannelSyncService(db_session)

        result = service.sync_all_inquiries(connector, platform.id, date(2026, 1, 1), date(2026, 1, 7))

        assert result["status"] == "UNSUPPORTED"


class TestRequestLimitPropagation:
    """상용 ERP 확장 5단계 B묶음 보완 - 호출량 안전 상한(설정값)이 실제로 커넥터
    호출까지 전달되는지 검증한다(요구사항: "scheduler가 설정된 기간·페이지·retry·
    요청 예산을 실제로 전달")."""

    def test_sync_inquiries_without_override_passes_settings_defaults(self, db_session, platform, monkeypatch):
        from config.settings import settings

        monkeypatch.setattr(settings, "cs_inquiry_sync_max_pages_per_query", 3)
        monkeypatch.setattr(settings, "cs_inquiry_sync_max_retries_per_page", 2)
        monkeypatch.setattr(settings, "cs_inquiry_sync_max_requests_per_run", 45)
        connector = _StubConnector(items=[_inquiry()])
        service = CsChannelSyncService(db_session)

        service.sync_inquiries(connector, platform.id, date(2026, 1, 1), date(2026, 1, 7))

        assert connector.call_kwargs["max_pages"] == 3
        assert connector.call_kwargs["max_retries"] == 2
        budget = connector.call_kwargs["request_budget"]
        assert isinstance(budget, RequestBudget)
        assert budget.max_requests == 45

    def test_sync_inquiries_explicit_override_wins_over_settings(self, db_session, platform, monkeypatch):
        """수동 제한 검증(예: 상품별 문의 1소스·1일·max_pages=1·retry=0)이 settings
        기본값보다 더 엄격한 값을 명시적으로 넘길 수 있어야 한다."""
        from config.settings import settings

        monkeypatch.setattr(settings, "cs_inquiry_sync_max_pages_per_query", 3)
        monkeypatch.setattr(settings, "cs_inquiry_sync_max_retries_per_page", 2)
        connector = _StubConnector(product_items=[_inquiry(inquiry_id="5002")])
        service = CsChannelSyncService(db_session)

        service.sync_inquiries(
            connector,
            platform.id,
            date(2026, 1, 1),
            date(2026, 1, 1),
            source=COUPANG_PRODUCT_INQUIRY_SOURCE,
            max_pages=1,
            max_retries=0,
        )

        assert connector.product_call_kwargs["max_pages"] == 1
        assert connector.product_call_kwargs["max_retries"] == 0

    def test_sync_all_inquiries_shares_one_budget_across_both_sources(self, db_session, platform):
        """두 소스가 같은 RequestBudget 인스턴스를 공유해야 플랫폼 1회 실행의 전체
        요청 수 상한이 소스별이 아니라 호출 전체 기준으로 지켜진다."""
        connector = _StubConnector(items=[_inquiry(inquiry_id="1")], product_items=[_inquiry(inquiry_id="2")])
        service = CsChannelSyncService(db_session)

        service.sync_all_inquiries(connector, platform.id, date(2026, 1, 1), date(2026, 1, 7))

        call_center_budget = connector.call_kwargs["request_budget"]
        product_budget = connector.product_call_kwargs["request_budget"]
        assert call_center_budget is product_budget

    def test_page_limit_exceeded_fails_only_that_source_without_db_write(self, db_session, platform):
        """다음 페이지가 더 있다고 알리는데 max_pages에 도달한 상황을 커넥터가
        PAGE_LIMIT_EXCEEDED로 보고하면, 그 source는 FAILED로 끝나고 이미 "모아 둔"
        페이지의 항목은 DB에 전혀 저장되지 않아야 한다(연결부인 CsChannelSyncService
        입장에서는 커넥터가 raw_items를 전혀 돌려주지 못하므로 결과적으로 _upsert_one
        자체가 호출되지 않는다)."""
        connector = _StubConnector(
            error=MarketplaceExternalAPIError("coupang", "PAGE_LIMIT_EXCEEDED", False),
            product_items=[_inquiry(inquiry_id="3")],
        )
        service = CsChannelSyncService(db_session)

        result = service.sync_all_inquiries(connector, platform.id, date(2026, 1, 1), date(2026, 1, 7))

        assert result["by_source"][COUPANG_CALL_CENTER_SOURCE]["status"] == "FAILED"
        assert result["by_source"][COUPANG_CALL_CENTER_SOURCE]["reason_code"] == "PAGE_LIMIT_EXCEEDED"
        assert result["by_source"][COUPANG_PRODUCT_INQUIRY_SOURCE]["status"] == "SUCCESS"
        repo = CsCaseRepository(db_session)
        assert repo.get_by_external(platform.id, COUPANG_CALL_CENTER_SOURCE, "1001") is None
        assert repo.get_by_external(platform.id, COUPANG_PRODUCT_INQUIRY_SOURCE, "3") is not None

    def test_request_budget_exceeded_mid_run_fails_remaining_source_cleanly(self, db_session, platform):
        """요청 예산이 첫 소스에서 이미 소진됐다고 가정한 상황(REQUEST_BUDGET_EXCEEDED)도
        PAGE_LIMIT_EXCEEDED와 동일하게 안전한 FAILED로 처리되고, DB write가 없다."""
        connector = _StubConnector(
            items=[_inquiry(inquiry_id="4")],
            product_error=MarketplaceExternalAPIError("coupang", "REQUEST_BUDGET_EXCEEDED", False),
        )
        service = CsChannelSyncService(db_session)

        result = service.sync_all_inquiries(connector, platform.id, date(2026, 1, 1), date(2026, 1, 7))

        assert result["by_source"][COUPANG_PRODUCT_INQUIRY_SOURCE]["status"] == "FAILED"
        assert result["by_source"][COUPANG_PRODUCT_INQUIRY_SOURCE]["reason_code"] == "REQUEST_BUDGET_EXCEEDED"
        assert result["status"] == "PARTIAL_SUCCESS"
        repo = CsCaseRepository(db_session)
        assert repo.get_by_external(platform.id, COUPANG_CALL_CENTER_SOURCE, "4") is not None


class TestRequestBudget:
    """integrations.malls.errors.RequestBudget 자체의 단위 동작."""

    def test_consume_within_limit_succeeds(self):
        budget = RequestBudget(max_requests=2)
        budget.consume("coupang")
        budget.consume("coupang")
        assert budget.used == 2

    def test_consume_beyond_limit_raises_without_incrementing(self):
        budget = RequestBudget(max_requests=1)
        budget.consume("coupang")
        with pytest.raises(MarketplaceExternalAPIError) as exc_info:
            budget.consume("coupang")
        assert exc_info.value.reason_code == "REQUEST_BUDGET_EXCEEDED"
        assert budget.used == 1  # 초과 시도는 카운트에 반영되지 않는다(실제 요청을 보내지 않았으므로).
