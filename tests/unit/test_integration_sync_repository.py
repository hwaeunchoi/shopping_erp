"""
tests/unit/test_integration_sync_repository.py
------------------------------------------------------
ExternalCommandRepository/ExternalCommandLineResultRepository/
OrderStatusConflictRepository의 조회 경계조건과 원자적 claim/try_transition을
검증한다 - outbox worker(scheduler.jobs.outbox_dispatch_job)가 정확히 due한
명령만 골라 실행하고, 오래 멈춰있는 RUNNING만 회수 대상으로 보고, 두 worker가
같은 명령을 동시에 실행하지 못하는지가 핵심이다.
"""

import uuid
from datetime import datetime, timedelta, timezone

from models.integration_sync import ExternalCommand, OrderStatusConflict
from models.order import Order, OrderItem
from models.product import Product, ProductOption
from repositories.integration_sync_repository import (
    ExternalCommandLineResultRepository,
    ExternalCommandRepository,
    OrderStatusConflictRepository,
)


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


def _make_order_item(db_session, platform, poin="POIN-1"):
    order = _make_order(db_session, platform)
    product = Product(name="p", category="c", base_price=1000, status="ACTIVE")
    db_session.add(product)
    db_session.flush()
    option = ProductOption(product_id=product.id, sku_code=f"SKU-{poin}", is_active=True)
    db_session.add(option)
    db_session.flush()
    item = OrderItem(
        order_id=order.id,
        product_option_id=option.id,
        platform_order_item_no=poin,
        quantity=3,
        unit_price=1000,
        line_amount=3000,
    )
    db_session.add(item)
    db_session.flush()
    return item


class TestClaimIsAtomic:
    """두 worker가 같은 명령을 동시에 claim하려 해도 정확히 하나만 성공해야 한다."""

    def test_second_claim_on_pending_fails(self, db_session, platform):
        repo = ExternalCommandRepository(db_session)
        cmd = repo.add(_make_command(platform, "PENDING"))

        assert repo.claim(cmd.id, "lease-A") is True
        assert repo.claim(cmd.id, "lease-B") is False

        db_session.refresh(cmd)
        assert cmd.status == "RUNNING"
        assert cmd.lease_token == "lease-A"
        assert cmd.attempt_count == 1

    def test_claim_on_running_fails(self, db_session, platform):
        repo = ExternalCommandRepository(db_session)
        cmd = repo.add(_make_command(platform, "RUNNING"))

        assert repo.claim(cmd.id, "lease-X") is False

    def test_claim_on_unknown_fails(self, db_session, platform):
        """UNKNOWN(결과 확인 필요) 명령은 claim 대상이 아니다 - 자동 재시도 금지."""
        repo = ExternalCommandRepository(db_session)
        cmd = repo.add(_make_command(platform, "UNKNOWN"))

        assert repo.claim(cmd.id, "lease-X") is False

    def test_claim_on_retry_wait_succeeds(self, db_session, platform):
        repo = ExternalCommandRepository(db_session)
        cmd = repo.add(_make_command(platform, "RETRY_WAIT"))

        assert repo.claim(cmd.id, "lease-A") is True


class TestTryTransitionRespectsOwnership:
    def test_matching_lease_token_succeeds(self, db_session, platform):
        repo = ExternalCommandRepository(db_session)
        cmd = repo.add(_make_command(platform, "PENDING"))
        repo.claim(cmd.id, "lease-A")

        ok = repo.try_transition(cmd.id, "lease-A", status="SUCCESS")

        assert ok is True
        db_session.refresh(cmd)
        assert cmd.status == "SUCCESS"

    def test_stale_lease_token_is_rejected(self, db_session, platform):
        """소유권을 잃은(예: 회수된) worker가 예전 lease_token으로 쓰려 하면 무시된다."""
        repo = ExternalCommandRepository(db_session)
        cmd = repo.add(_make_command(platform, "PENDING"))
        repo.claim(cmd.id, "lease-A")
        # 회수 절차가 lease_token을 바꿨다고 가정.
        cmd.lease_token = "lease-B-from-recovery"
        db_session.flush()

        ok = repo.try_transition(cmd.id, "lease-A", status="SUCCESS")

        assert ok is False
        db_session.refresh(cmd)
        assert cmd.status != "SUCCESS"


class TestExternalCommandLineResultUpsert:
    def test_record_result_inserts_new_row(self, db_session, platform):
        cmd = ExternalCommandRepository(db_session).add(_make_command(platform, "RUNNING"))
        item = _make_order_item(db_session, platform)
        repo = ExternalCommandLineResultRepository(db_session)

        repo.record_result(cmd.id, item.id, status="SUCCESS", quantity=2, result_code="OK")

        assert repo.sum_success_quantity(item.id) == 2

    def test_record_result_updates_existing_row_instead_of_duplicating(self, db_session, platform):
        """같은 (command_id, order_item_id)로 재시도(예: 실패 후 같은 명령을 다시 실행)해도
        유니크 제약 위반 없이 기존 행을 갱신해야 한다."""
        cmd = ExternalCommandRepository(db_session).add(_make_command(platform, "RUNNING"))
        item = _make_order_item(db_session, platform)
        repo = ExternalCommandLineResultRepository(db_session)
        repo.record_result(cmd.id, item.id, status="FAILED", quantity=2, result_code="FAIL_CODE")

        repo.record_result(cmd.id, item.id, status="SUCCESS", quantity=2, result_code="OK")

        assert repo.list_success_order_item_ids(cmd.id) == {item.id}
        assert repo.sum_success_quantity(item.id) == 2  # 중복 합산되지 않는다(행 1개).

    def test_sum_success_quantity_aggregates_across_commands(self, db_session, platform):
        """같은 order_item이 서로 다른 명령(다른 Shipment)에서 부분 수량씩 성공하면
        합계로 집계된다."""
        item = _make_order_item(db_session, platform)
        cmd1 = ExternalCommandRepository(db_session).add(_make_command(platform, "RUNNING", target_id=1))
        cmd2 = ExternalCommandRepository(db_session).add(_make_command(platform, "RUNNING", target_id=2))
        repo = ExternalCommandLineResultRepository(db_session)

        repo.record_result(cmd1.id, item.id, status="SUCCESS", quantity=1, result_code="OK")
        repo.record_result(cmd2.id, item.id, status="SUCCESS", quantity=2, result_code="OK")

        assert repo.sum_success_quantity(item.id) == 3
