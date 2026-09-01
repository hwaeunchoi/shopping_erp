"""
tests/unit/test_order_channel_sync_service.py
----------------------------------------------------
채널 주문상태와 내부 상태를 비교해 허용된 전이만 반영하고, 아니면
OrderStatusConflict로 남기는지 검증한다.
"""

from datetime import datetime, timezone

import pytest

from models.order import Order
from repositories.integration_sync_repository import OrderStatusConflictRepository
from services.order_channel_sync_service import OrderChannelSyncService


def _make_order(db_session, platform, status="NEW"):
    order = Order(
        platform_id=platform.id,
        platform_order_no="ORDER-CS-1",
        status=status,
        order_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        total_amount=10000,
    )
    db_session.add(order)
    db_session.flush()
    return order


class TestAllowedChannelTransitionIsApplied:
    def test_applies_and_records_audit(self, db_session, platform):
        order = _make_order(db_session, platform, status="NEW")
        service = OrderChannelSyncService(db_session)

        result = service.sync_channel_status(order, "PREPARING")

        assert result.applied is True
        assert result.conflict is False
        assert order.status == "PREPARING"

    def test_same_status_is_a_noop(self, db_session, platform):
        order = _make_order(db_session, platform, status="SHIPPING")
        service = OrderChannelSyncService(db_session)

        result = service.sync_channel_status(order, "SHIPPING")

        assert result.applied is False
        assert result.conflict is False
        assert order.status == "SHIPPING"


class TestDisallowedChannelTransitionIsAConflict:
    def test_regression_is_recorded_as_conflict_not_applied(self, db_session, platform):
        order = _make_order(db_session, platform, status="DELIVERED")
        service = OrderChannelSyncService(db_session)

        result = service.sync_channel_status(order, "NEW")

        assert result.applied is False
        assert result.conflict is True
        assert order.status == "DELIVERED"  # 내부 상태는 그대로 유지된다.

        conflicts = OrderStatusConflictRepository(db_session).list_unresolved(order_id=order.id)
        assert len(conflicts) == 1
        assert conflicts[0].internal_status == "DELIVERED"
        assert conflicts[0].channel_status == "NEW"

    def test_repeated_detection_of_same_conflict_does_not_duplicate(self, db_session, platform):
        """같은 채널상태 불일치가 반복 감지돼도(스케줄 재조회 등) 미해소 충돌 행이
        중복 생성되지 않는다."""
        order = _make_order(db_session, platform, status="DELIVERED")
        service = OrderChannelSyncService(db_session)

        service.sync_channel_status(order, "NEW")
        service.sync_channel_status(order, "NEW")
        service.sync_channel_status(order, "NEW")

        conflicts = OrderStatusConflictRepository(db_session).list_unresolved(order_id=order.id)
        assert len(conflicts) == 1

    def test_different_channel_status_creates_a_second_conflict(self, db_session, platform):
        """중복 방지는 같은 channel_status끼리만 적용된다 - 서로 다른 채널상태 불일치는
        별개 충돌로 남아야 운영자가 둘 다 확인할 수 있다."""
        order = _make_order(db_session, platform, status="DELIVERED")
        service = OrderChannelSyncService(db_session)

        service.sync_channel_status(order, "NEW")
        service.sync_channel_status(order, "PREPARING")

        conflicts = OrderStatusConflictRepository(db_session).list_unresolved(order_id=order.id)
        assert len(conflicts) == 2


class TestResolveConflict:
    def test_accept_channel_applies_channel_status(self, db_session, platform):
        order = _make_order(db_session, platform, status="DELIVERED")
        service = OrderChannelSyncService(db_session)
        result = service.sync_channel_status(order, "NEW")
        conflict_id = OrderStatusConflictRepository(db_session).list_unresolved(order_id=order.id)[0].id
        assert result.conflict is True

        resolved = service.resolve_conflict(conflict_id, "ACCEPT_CHANNEL", resolved_by=None)

        assert resolved.resolved_at is not None
        assert resolved.resolution == "ACCEPT_CHANNEL"
        assert order.status == "NEW"

    def test_keep_internal_leaves_order_status_untouched(self, db_session, platform):
        order = _make_order(db_session, platform, status="DELIVERED")
        service = OrderChannelSyncService(db_session)
        service.sync_channel_status(order, "NEW")
        conflict_id = OrderStatusConflictRepository(db_session).list_unresolved(order_id=order.id)[0].id

        resolved = service.resolve_conflict(conflict_id, "KEEP_INTERNAL", resolved_by=None)

        assert resolved.resolution == "KEEP_INTERNAL"
        assert order.status == "DELIVERED"

    def test_already_resolved_conflict_rejects_second_resolution(self, db_session, platform):
        order = _make_order(db_session, platform, status="DELIVERED")
        service = OrderChannelSyncService(db_session)
        service.sync_channel_status(order, "NEW")
        conflict_id = OrderStatusConflictRepository(db_session).list_unresolved(order_id=order.id)[0].id
        service.resolve_conflict(conflict_id, "KEEP_INTERNAL")

        with pytest.raises(ValueError):
            service.resolve_conflict(conflict_id, "ACCEPT_CHANNEL")
