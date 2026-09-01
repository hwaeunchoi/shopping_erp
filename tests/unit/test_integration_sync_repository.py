"""
tests/unit/test_integration_sync_repository.py
------------------------------------------------------
ExternalCommandRepository/OrderStatusConflictRepository의 조회 경계조건을
검증한다 - outbox worker(scheduler.jobs.outbox_dispatch_job)가 정확히 due한
명령만 골라 실행하고, 오래 멈춰있는 RUNNING만 회수 대상으로 보는지가 핵심이다.
"""

import uuid
from datetime import datetime, timedelta, timezone

from models.integration_sync import ExternalCommand, OrderStatusConflict
from models.order import Order
from repositories.integration_sync_repository import ExternalCommandRepository, OrderStatusConflictRepository


def _make_order(db_session, platform):
    order = Order(
        platform_id=platform.id,
        platform_order_no=f"ORDER-{uuid.uuid4().hex[:8]}",
        status="PREPARING",
        order_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        total_amount=1000,
    )
    db_session.add(order)
    db_session.flush()
    return order


def _make_command(platform, status, command_type="SHIPMENT_SUBMIT", next_retry_at=None, target_id=1):
    return ExternalCommand(
        idempotency_key=f"KEY-{uuid.uuid4().hex}",
        command_type=command_type,
        platform_id=platform.id,
        platform_code=platform.code,
        target_type="SHIPMENT",
        target_id=target_id,
        status=status,
        next_retry_at=next_retry_at,
        trace_id=uuid.uuid4().hex,
    )


class TestListDueForExecution:
    def test_pending_is_always_due(self, db_session, platform):
        repo = ExternalCommandRepository(db_session)
        cmd = repo.add(_make_command(platform, "PENDING"))

        due = repo.list_due_for_execution("SHIPMENT_SUBMIT")

        assert [c.id for c in due] == [cmd.id]

    def test_retry_wait_with_past_next_retry_at_is_due(self, db_session, platform):
        repo = ExternalCommandRepository(db_session)
        cmd = repo.add(
            _make_command(platform, "RETRY_WAIT", next_retry_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        )

        due = repo.list_due_for_execution("SHIPMENT_SUBMIT")

        assert [c.id for c in due] == [cmd.id]

    def test_retry_wait_with_future_next_retry_at_is_not_due(self, db_session, platform):
        repo = ExternalCommandRepository(db_session)
        repo.add(
            _make_command(platform, "RETRY_WAIT", next_retry_at=datetime.now(timezone.utc) + timedelta(minutes=30))
        )

        due = repo.list_due_for_execution("SHIPMENT_SUBMIT")

        assert due == []

    def test_running_success_failed_are_never_due(self, db_session, platform):
        repo = ExternalCommandRepository(db_session)
        for status in ("RUNNING", "SUCCESS", "FAILED", "CANCELLED"):
            repo.add(_make_command(platform, status))

        due = repo.list_due_for_execution("SHIPMENT_SUBMIT")

        assert due == []

    def test_filters_by_command_type(self, db_session, platform):
        repo = ExternalCommandRepository(db_session)
        repo.add(_make_command(platform, "PENDING", command_type="OTHER_TYPE"))

        due = repo.list_due_for_execution("SHIPMENT_SUBMIT")

        assert due == []


class TestListStaleRunning:
    def test_old_running_is_stale(self, db_session, platform):
        repo = ExternalCommandRepository(db_session)
        cmd = repo.add(_make_command(platform, "RUNNING"))
        cmd.updated_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        db_session.flush()

        threshold = datetime.now(timezone.utc) - timedelta(minutes=15)
        stale = repo.list_stale_running("SHIPMENT_SUBMIT", threshold)

        assert [c.id for c in stale] == [cmd.id]

    def test_fresh_running_is_not_stale(self, db_session, platform):
        repo = ExternalCommandRepository(db_session)
        repo.add(_make_command(platform, "RUNNING"))

        threshold = datetime.now(timezone.utc) - timedelta(minutes=15)
        stale = repo.list_stale_running("SHIPMENT_SUBMIT", threshold)

        assert stale == []

    def test_non_running_is_never_stale(self, db_session, platform):
        repo = ExternalCommandRepository(db_session)
        cmd = repo.add(_make_command(platform, "PENDING"))
        cmd.updated_at = datetime.now(timezone.utc) - timedelta(hours=5)
        db_session.flush()

        threshold = datetime.now(timezone.utc) - timedelta(minutes=15)
        stale = repo.list_stale_running("SHIPMENT_SUBMIT", threshold)

        assert stale == []


class TestUnresolvedConflictDedup:
    def test_no_unresolved_conflict_returns_none(self, db_session, platform):
        order = _make_order(db_session, platform)
        repo = OrderStatusConflictRepository(db_session)
        assert repo.get_unresolved_for_status(order_id=order.id, channel_status="SHIPPING") is None

    def test_finds_matching_unresolved_conflict(self, db_session, platform):
        order = _make_order(db_session, platform)
        repo = OrderStatusConflictRepository(db_session)
        conflict = repo.add(
            OrderStatusConflict(
                order_id=order.id,
                internal_status="CANCELED",
                channel_status="SHIPPING",
                detected_at=datetime.now(timezone.utc),
            )
        )

        found = repo.get_unresolved_for_status(order_id=order.id, channel_status="SHIPPING")

        assert found is not None
        assert found.id == conflict.id

    def test_resolved_conflict_is_not_matched(self, db_session, platform):
        order = _make_order(db_session, platform)
        repo = OrderStatusConflictRepository(db_session)
        conflict = repo.add(
            OrderStatusConflict(
                order_id=order.id,
                internal_status="CANCELED",
                channel_status="SHIPPING",
                detected_at=datetime.now(timezone.utc),
            )
        )
        conflict.resolved_at = datetime.now(timezone.utc)
        db_session.flush()

        assert repo.get_unresolved_for_status(order_id=order.id, channel_status="SHIPPING") is None

    def test_different_channel_status_is_not_matched(self, db_session, platform):
        order = _make_order(db_session, platform)
        repo = OrderStatusConflictRepository(db_session)
        repo.add(
            OrderStatusConflict(
                order_id=order.id,
                internal_status="CANCELED",
                channel_status="SHIPPING",
                detected_at=datetime.now(timezone.utc),
            )
        )

        assert repo.get_unresolved_for_status(order_id=order.id, channel_status="DELIVERED") is None
