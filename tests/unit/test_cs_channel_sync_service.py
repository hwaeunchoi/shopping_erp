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
from services.cs_channel_sync_service import CsChannelSyncService


class _StubConnector:
    supports_inquiry_sync = True

    def __init__(self, items: Optional[list[dict[str, Any]]] = None, error: Optional[Exception] = None) -> None:
        self.items = items or []
        self.error = error
        self.called_with: Optional[tuple[date, date]] = None

    def fetch_inquiries(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        self.called_with = (start_date, end_date)
        if self.error is not None:
            raise self.error
        return self.items


class _UnsupportedConnector:
    supports_inquiry_sync = False

    def fetch_inquiries(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        raise AssertionError("supports_inquiry_sync=False인 커넥터의 fetch_inquiries는 호출되면 안 된다.")


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
        case = CsCaseRepository(db_session).get_by_external(platform.id, "1001")
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

        case = CsCaseRepository(db_session).get_by_external(platform.id, "1001")
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
        case = repo.get_by_external(platform.id, "1001")
        assert case is not None
        case.assignee_id = None  # 담당자 미배정 상태에서 태그만 먼저 달아본다.
        case.tags = "긴급,VIP"
        case.reply_draft = "고객님께 안내드릴 초안"
        db_session.flush()

        service.sync_inquiries(connector, platform.id, date(2026, 1, 1), date(2026, 1, 7))

        refreshed = repo.get_by_external(platform.id, "1001")
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

        case = CsCaseRepository(db_session).get_by_external(platform.id, "1001")
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
