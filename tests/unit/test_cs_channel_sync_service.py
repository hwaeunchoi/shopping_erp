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
)
from models.order import Order
from repositories.cs_case_repository import CsCaseRepository
from services.cs_channel_sync_service import (
    COUPANG_CALL_CENTER_SOURCE,
    COUPANG_PRODUCT_INQUIRY_SOURCE,
    CsChannelSyncService,
)


class _StubConnector:
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

    def fetch_inquiries(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        self.called_with = (start_date, end_date)
        if self.error is not None:
            raise self.error
        return self.items

    def fetch_product_inquiries(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        self.product_called_with = (start_date, end_date)
        if self.product_error is not None:
            raise self.product_error
        return self.product_items


class _UnsupportedConnector:
    supports_inquiry_sync = False
    supports_product_inquiry_sync = False

    def fetch_inquiries(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        raise AssertionError("supports_inquiry_sync=False인 커넥터의 fetch_inquiries는 호출되면 안 된다.")

    def fetch_product_inquiries(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
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
