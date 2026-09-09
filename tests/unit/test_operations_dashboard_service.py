"""
tests/unit/test_operations_dashboard_service.py
------------------------------------------------------
services.operations_dashboard_service.OperationsDashboardService 검증 -
상용 ERP 확장(6단계).
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from models.cs_case import CsCase
from models.fulfillment import FulfillmentBatch, FulfillmentBatchItem
from models.integration_sync import ExternalCommand, OrderStatusConflict
from models.order import Order
from models.settlement import Settlement
from repositories.extra_repository import IntegrationStatusRepository
from services.operations_dashboard_service import (
    OperationsDashboardService,
    classify_integration_severity,
    utc_day_bounds,
)


def _make_command(
    platform, status, command_type="SHIPMENT_SUBMIT", created_at=None, attempt_count=0
) -> ExternalCommand:
    cmd = ExternalCommand(
        idempotency_key=f"KEY-{uuid.uuid4().hex}",
        command_type=command_type,
        platform_id=platform.id,
        platform_code=platform.code,
        target_type="SHIPMENT",
        target_id=1,
        status=status,
        attempt_count=attempt_count,
        trace_id=uuid.uuid4().hex,
    )
    if created_at is not None:
        cmd.created_at = created_at
    return cmd


def _make_order(db_session, platform, created_at=None) -> Order:
    order = Order(
        platform_id=platform.id,
        platform_order_no=f"OPS-{uuid.uuid4().hex[:8]}",
        status="NEW",
        order_date=datetime.now(timezone.utc),
        total_amount=1000,
    )
    db_session.add(order)
    db_session.flush()
    if created_at is not None:
        order.created_at = created_at
        db_session.flush()
    return order


class TestUtcDayBounds:
    def test_day_boundary_matches_midnight_utc(self):
        now = datetime(2026, 3, 5, 13, 45, 0, tzinfo=timezone.utc)
        start, end = utc_day_bounds(now)
        assert start == datetime(2026, 3, 5, 0, 0, 0)
        assert end == datetime(2026, 3, 6, 0, 0, 0)

    def test_boundary_just_before_midnight_stays_in_previous_day(self):
        now = datetime(2026, 3, 5, 23, 59, 59, 999999, tzinfo=timezone.utc)
        start, _ = utc_day_bounds(now)
        assert start == datetime(2026, 3, 5, 0, 0, 0)

    def test_boundary_exactly_at_midnight_starts_new_day(self):
        now = datetime(2026, 3, 6, 0, 0, 0, tzinfo=timezone.utc)
        start, _ = utc_day_bounds(now)
        assert start == datetime(2026, 3, 6, 0, 0, 0)


class TestSummaryOrders:
    def test_no_data_returns_zero_not_error(self, db_session, platform):
        summary = OperationsDashboardService(db_session).summary()
        assert summary["orders"]["collected_today"] == 0
        assert summary["orders"]["unshipped"] == 0

    def test_collected_today_uses_created_at_not_order_date(self, db_session, platform):
        """오늘 수집된 주문은 created_at(우리 DB 적재 시각) 기준이다 - order_date가
        오늘이어도 실제 적재(created_at)가 어제였으면 세지 않는다."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        yesterday_created = now - timedelta(days=1, hours=1)
        order_created_yesterday = _make_order(db_session, platform, created_at=yesterday_created)
        order_created_today = _make_order(db_session, platform, created_at=now)
        db_session.commit()

        summary = OperationsDashboardService(db_session).summary(now=datetime.now(timezone.utc))
        assert summary["orders"]["collected_today"] == 1
        assert order_created_yesterday.id != order_created_today.id  # 두 주문이 실제로 다름을 확인

    def test_future_created_at_excluded_from_today(self, db_session, platform):
        """미래 시각으로 잘못 기록된 행이 있어도 "오늘" 집계에 잘못 포함되지 않는다 -
        summary()가 받는 now보다 미래인 created_at은 today 창(now 기준 오늘)의
        상한(다음날 자정)을 넘을 수도, 넘지 않을 수도 있으나 이 테스트는 명백히
        다음날에 해당하는 시각을 써서 오늘 카운트에서 제외됨을 확인한다."""
        fixed_now = datetime(2026, 3, 5, 10, 0, 0, tzinfo=timezone.utc)
        far_future = datetime(2026, 3, 10, 0, 0, 0)
        _make_order(db_session, platform, created_at=far_future)
        db_session.commit()

        summary = OperationsDashboardService(db_session).summary(now=fixed_now)
        assert summary["orders"]["collected_today"] == 0


class TestSuccessRate:
    def test_denominator_zero_returns_none_not_zero_percent(self, db_session, platform):
        summary = OperationsDashboardService(db_session).summary()
        assert summary["success_rate"]["last_24h"]["rate_percent"] is None
        assert summary["success_rate"]["last_24h"]["total"] == 0

    def test_pending_and_running_excluded_from_rate(self, db_session, platform):
        now = datetime.now(timezone.utc)
        naive_now = now.replace(tzinfo=None) - timedelta(minutes=1)  # until 경계(now)에 딱 걸치지 않게 여유를 둔다.
        db_session.add(_make_command(platform, "SUCCESS", created_at=naive_now))
        db_session.add(_make_command(platform, "PENDING", created_at=naive_now))
        db_session.add(_make_command(platform, "RUNNING", created_at=naive_now))
        db_session.add(_make_command(platform, "RETRY_WAIT", created_at=naive_now))
        db_session.add(_make_command(platform, "UNKNOWN", created_at=naive_now))
        db_session.add(_make_command(platform, "CANCELLED", created_at=naive_now))
        db_session.commit()

        summary = OperationsDashboardService(db_session).summary(now=now)
        rate = summary["success_rate"]["last_24h"]
        assert rate["success"] == 1
        assert rate["failed"] == 0
        assert rate["total"] == 1  # PENDING/RUNNING/RETRY_WAIT/UNKNOWN/CANCELLED 전부 제외
        assert rate["rate_percent"] == 100.0

    def test_success_and_failed_both_counted(self, db_session, platform):
        now = datetime.now(timezone.utc)
        naive_now = now.replace(tzinfo=None) - timedelta(minutes=1)
        db_session.add(_make_command(platform, "SUCCESS", created_at=naive_now))
        db_session.add(_make_command(platform, "SUCCESS", created_at=naive_now))
        db_session.add(_make_command(platform, "FAILED", created_at=naive_now))
        db_session.commit()

        summary = OperationsDashboardService(db_session).summary(now=now)
        rate = summary["success_rate"]["last_24h"]
        assert rate["success"] == 2
        assert rate["failed"] == 1
        assert rate["rate_percent"] == pytest.approx(66.67, abs=0.01)

    def test_command_outside_24h_window_excluded(self, db_session, platform):
        now = datetime.now(timezone.utc)
        old = now.replace(tzinfo=None) - timedelta(hours=25)
        db_session.add(_make_command(platform, "SUCCESS", created_at=old))
        db_session.commit()

        summary = OperationsDashboardService(db_session).summary(now=now)
        assert summary["success_rate"]["last_24h"]["total"] == 0
        # 7일 창에는 포함된다.
        assert summary["success_rate"]["last_7d"]["total"] == 1

    def test_scope_limited_to_write_command_types(self, db_session, platform):
        """WRITE_COMMAND_TYPES 밖의 명령종류는 성공률 계산에 섞이지 않는다."""
        now = datetime.now(timezone.utc)
        naive_now = now.replace(tzinfo=None)
        db_session.add(_make_command(platform, "SUCCESS", command_type="SOME_FUTURE_TYPE", created_at=naive_now))
        db_session.commit()

        summary = OperationsDashboardService(db_session).summary(now=now)
        assert summary["success_rate"]["last_24h"]["total"] == 0


class TestCommandStatusBreakdown:
    def test_shipment_and_product_commands_separated(self, db_session, platform):
        db_session.add(_make_command(platform, "FAILED", command_type="SHIPMENT_SUBMIT"))
        db_session.add(_make_command(platform, "FAILED", command_type="PRODUCT_CREATE"))
        db_session.add(_make_command(platform, "RETRY_WAIT", command_type="INVENTORY_UPDATE"))
        db_session.commit()

        summary = OperationsDashboardService(db_session).summary()
        assert summary["shipment_commands_by_status"]["FAILED"] == 1
        assert summary["shipment_commands_by_status"]["RETRY_WAIT"] == 0
        assert summary["product_commands_by_status"]["FAILED"] == 1
        assert summary["product_commands_by_status"]["RETRY_WAIT"] == 1


class TestFulfillmentBuckets:
    def _make_item(self, db_session, warehouse, platform, product_option, status) -> FulfillmentBatchItem:
        from models.order import OrderItem

        order = _make_order(db_session, platform)
        order_item = OrderItem(
            order_id=order.id, product_option_id=product_option.id, quantity=1, unit_price=1000, line_amount=1000
        )
        db_session.add(order_item)
        db_session.flush()
        batch = FulfillmentBatch(warehouse_id=warehouse.id, status="IN_PROGRESS")
        db_session.add(batch)
        db_session.flush()
        item = FulfillmentBatchItem(
            batch_id=batch.id,
            order_id=order.id,
            order_item_id=order_item.id,
            product_option_id=product_option.id,
            requested_quantity=1,
            status=status,
        )
        db_session.add(item)
        db_session.flush()
        return item

    def test_kpi_buckets_group_raw_statuses(self, db_session, warehouse, platform, product_option):
        self._make_item(db_session, warehouse, platform, product_option, "READY")
        self._make_item(db_session, warehouse, platform, product_option, "PICKING")
        self._make_item(db_session, warehouse, platform, product_option, "PICKED")
        self._make_item(db_session, warehouse, platform, product_option, "PACKED")
        db_session.commit()

        summary = OperationsDashboardService(db_session).summary()
        buckets = summary["fulfillment"]["by_kpi_bucket"]
        assert buckets["READY_TO_PICK"] == 1
        assert buckets["PICKING"] == 2  # PICKING + PICKED
        assert buckets["PACKED"] == 1
        assert summary["fulfillment"]["raw_by_status"]["READY"] == 1


class TestOrderStatusConflictsAndCs:
    def test_unresolved_conflict_counted(self, db_session, platform):
        order = _make_order(db_session, platform)
        db_session.add(
            OrderStatusConflict(
                order_id=order.id,
                internal_status="NEW",
                channel_status="CANCELED",
                detected_at=datetime.now(timezone.utc),
            )
        )
        db_session.commit()

        summary = OperationsDashboardService(db_session).summary()
        assert summary["order_status_conflicts_unresolved"] == 1

    def test_cs_dashboard_reused(self, db_session):
        db_session.add(
            CsCase(inquiry_type="ETC", priority="NORMAL", status="OPEN", customer_message="문의", reopened_count=0)
        )
        db_session.commit()

        summary = OperationsDashboardService(db_session).summary()
        assert summary["cs"]["by_status"]["OPEN"] == 1


class TestSettlementByStatus:
    def test_settlement_counts_by_status(self, db_session, platform):
        db_session.add(
            Settlement(
                platform_id=platform.id,
                settlement_cycle="2026-03",
                settlement_type="MONTHLY",
                status="SCHEDULED",
                created_at=datetime.now(timezone.utc),
            )
        )
        db_session.commit()
        summary = OperationsDashboardService(db_session).summary()
        assert summary["settlement_by_status"]["SCHEDULED"] == 1


class TestTimeseries:
    def test_timeseries_length_matches_days_and_buckets_by_day(self, db_session, platform):
        now = datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
        db_session.add(_make_command(platform, "SUCCESS", created_at=datetime(2026, 3, 9, 5, 0, 0)))
        db_session.add(_make_command(platform, "FAILED", created_at=datetime(2026, 3, 10, 1, 0, 0)))
        db_session.commit()

        rows = OperationsDashboardService(db_session).timeseries(days=3, now=now)
        assert len(rows) == 3
        by_date = {r["date"]: r for r in rows}
        assert by_date["2026-03-09"]["success"] == 1
        assert by_date["2026-03-10"]["failed"] == 1
        assert by_date["2026-03-08"] == {"date": "2026-03-08", "success": 0, "failed": 0}


class TestIntegrationsStatus:
    def test_partial_status_maps_to_warning_severity(self, db_session, platform):
        IntegrationStatusRepository(db_session).upsert_partial("CLAIM", platform.code, "일부 실패")
        db_session.commit()

        rows = OperationsDashboardService(db_session).integrations_status()
        row = next(r for r in rows if r.integration_type == "CLAIM")
        assert row.status == "PARTIAL"
        assert row.severity == "WARNING"

    def test_error_never_succeeded_is_critical(self, db_session):
        now = datetime.now(timezone.utc)
        severity = classify_integration_severity("ERROR", None, now)
        assert severity == "CRITICAL"

    def test_error_recent_success_is_error_not_critical(self, db_session):
        now = datetime.now(timezone.utc)
        severity = classify_integration_severity("ERROR", now - timedelta(hours=1), now)
        assert severity == "ERROR"

    def test_error_long_since_success_is_critical(self, db_session):
        now = datetime.now(timezone.utc)
        severity = classify_integration_severity("ERROR", now - timedelta(days=10), now)
        assert severity == "CRITICAL"

    def test_normal_status_is_info(self, db_session, platform):
        IntegrationStatusRepository(db_session).upsert_success("MALL", platform.code)
        db_session.commit()
        rows = OperationsDashboardService(db_session).integrations_status()
        row = next(r for r in rows if r.integration_type == "MALL")
        assert row.severity == "INFO"


class TestSchedulerJobsStatus:
    def test_latest_per_target_only(self, db_session):
        from models.extra import TaskExecutionHistory

        now = datetime.now(timezone.utc)
        db_session.add(
            TaskExecutionHistory(
                task_type="FULL_SYNC",
                target="claim_sync",
                trigger_type="SCHEDULE",
                status="FAILED",
                started_at=now - timedelta(hours=1),
                finished_at=now - timedelta(hours=1),
                error_message="old failure",
            )
        )
        db_session.add(
            TaskExecutionHistory(
                task_type="FULL_SYNC",
                target="claim_sync",
                trigger_type="SCHEDULE",
                status="SUCCESS",
                started_at=now,
                finished_at=now,
            )
        )
        db_session.commit()

        rows = OperationsDashboardService(db_session).scheduler_jobs_status()
        claim_rows = [r for r in rows if r.target == "claim_sync"]
        assert len(claim_rows) == 1
        assert claim_rows[0].status == "SUCCESS"  # 최신 실행만 남는다(오래된 FAILED는 감춰짐).

    def test_failed_job_has_error_severity(self, db_session):
        from models.extra import TaskExecutionHistory

        now = datetime.now(timezone.utc)
        db_session.add(
            TaskExecutionHistory(
                task_type="FULL_SYNC",
                target="settlement_sync",
                trigger_type="SCHEDULE",
                status="FAILED",
                started_at=now,
                finished_at=now,
                error_message="boom",
            )
        )
        db_session.commit()
        rows = OperationsDashboardService(db_session).scheduler_jobs_status()
        row = next(r for r in rows if r.target == "settlement_sync")
        assert row.severity == "ERROR"

    def test_different_targets_sharing_task_type_are_not_confused(self, db_session):
        """settlement_sync/claim_sync/cs_inquiry_sync가 전부 task_type="FULL_SYNC"를
        공유해도 target으로 구분되어 서로의 최신 실행을 덮어쓰지 않는다."""
        from models.extra import TaskExecutionHistory

        now = datetime.now(timezone.utc)
        for target, status in (
            ("settlement_sync", "SUCCESS"),
            ("claim_sync", "FAILED"),
            ("cs_inquiry_sync", "SUCCESS"),
        ):
            db_session.add(
                TaskExecutionHistory(
                    task_type="FULL_SYNC", target=target, trigger_type="SCHEDULE", status=status, started_at=now
                )
            )
        db_session.commit()

        rows = OperationsDashboardService(db_session).scheduler_jobs_status()
        statuses_by_target = {r.target: r.status for r in rows}
        assert statuses_by_target["settlement_sync"] == "SUCCESS"
        assert statuses_by_target["claim_sync"] == "FAILED"
        assert statuses_by_target["cs_inquiry_sync"] == "SUCCESS"
