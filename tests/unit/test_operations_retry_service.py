"""
tests/unit/test_operations_retry_service.py
------------------------------------------------
services.operations_retry_service.OperationsRetryService 검증 - 상용 ERP
확장(6단계) 통합 실패 작업함의 라우팅 계층(FAILED 대량 재처리/UNKNOWN 해소).

이 서비스는 새 검증 로직이 없다 - command_type별 기존 단건 서비스
(ShipmentDispatchService/ProductPublishService/ProductOptionPublishService/
ProductSyncDispatchService)의 retry_failed_command()/resolve_unknown_command()를
그대로 호출하므로, 여기서는 "올바른 서비스로 라우팅되는지"와 "차단 정책
(UNKNOWN/RETRY_WAIT/RUNNING/미지원 command_type)이 실제로 지켜지는지"만
검증한다 - 각 서비스 자체의 상세 동작은 그 서비스의 기존 단위테스트가 이미
검증했다.

CONFIRMED_SUCCESS 해소는 SHIPMENT_SUBMIT 명령이면 연결된 Shipment/Order를
실제로 조회해 채널상태 동기화를 시도한다(ShipmentDispatchService 고유 동작) -
이 서비스의 라우팅 자체를 검증하는 목적에는 그 조회가 필요 없는 INVENTORY_UPDATE
(ProductSyncDispatchService)를 기본으로 쓴다.
"""

import uuid
from typing import Optional

import pytest

from models.extra import AuditLog
from models.integration_sync import ExternalCommand
from models.user import Role, User
from services.operations_retry_service import (
    MAX_BULK_RETRY_ITEMS,
    NOT_FOUND,
    NOT_RETRYABLE,
    RETRIED,
    UNKNOWN_REQUIRES_RESOLUTION,
    OperationsRetryService,
    OperationsRetryValidationError,
)


def _make_user(db_session, username: Optional[str] = None) -> User:
    role = Role(name=f"OPS-TEST-ROLE-{uuid.uuid4().hex[:8]}")
    db_session.add(role)
    db_session.flush()
    user = User(
        username=username or f"ops-agent-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        name="운영 담당자",
        role_id=role.id,
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()
    return user


def _make_command(
    platform, status, command_type="INVENTORY_UPDATE", target_type="PRODUCT_PLATFORM_MAP", target_id=1
) -> ExternalCommand:
    return ExternalCommand(
        idempotency_key=f"KEY-{uuid.uuid4().hex}",
        command_type=command_type,
        platform_id=platform.id,
        platform_code=platform.code,
        target_type=target_type,
        target_id=target_id,
        status=status,
        trace_id=uuid.uuid4().hex,
    )


def _service(db_session) -> OperationsRetryService:
    return OperationsRetryService(db_session)


class TestBulkRetryRouting:
    def test_failed_shipment_command_retried(self, db_session, platform):
        """retry_failed_command()는 Shipment/Order를 조회하지 않는다(상태값만
        되돌린다) - Shipment fixture 없이도 라우팅이 SHIPMENT_SUBMIT까지
        닿는지 확인할 수 있다."""
        actor = _make_user(db_session)
        cmd = _make_command(platform, "FAILED", command_type="SHIPMENT_SUBMIT", target_type="SHIPMENT")
        db_session.add(cmd)
        db_session.commit()

        result = _service(db_session).bulk_retry([cmd.id], actor_id=actor.id)

        assert len(result.items) == 1
        assert result.items[0].outcome == RETRIED
        db_session.refresh(cmd)
        assert cmd.status == "PENDING"

    def test_failed_product_create_command_retried(self, db_session, platform):
        actor = _make_user(db_session)
        cmd = _make_command(
            platform, "FAILED", command_type="PRODUCT_CREATE", target_type="PRODUCT_PUBLISH_DRAFT", target_id=1
        )
        db_session.add(cmd)
        db_session.commit()

        result = _service(db_session).bulk_retry([cmd.id], actor_id=actor.id)

        assert result.items[0].outcome == RETRIED
        db_session.refresh(cmd)
        assert cmd.status == "PENDING"

    def test_failed_inventory_update_retried(self, db_session, platform):
        actor = _make_user(db_session)
        cmd = _make_command(platform, "FAILED", command_type="INVENTORY_UPDATE")
        db_session.add(cmd)
        db_session.commit()

        result = _service(db_session).bulk_retry([cmd.id], actor_id=actor.id)

        assert result.items[0].outcome == RETRIED
        db_session.refresh(cmd)
        assert cmd.status == "PENDING"

    def test_audit_log_recorded_on_successful_retry(self, db_session, platform):
        actor = _make_user(db_session)
        cmd = _make_command(platform, "FAILED")
        db_session.add(cmd)
        db_session.commit()

        _service(db_session).bulk_retry([cmd.id], actor_id=actor.id)

        logs = db_session.query(AuditLog).filter_by(entity_type="EXTERNAL_COMMAND", entity_id=cmd.id).all()
        assert any(log.command == "operations.bulk_retry" and log.changed_by == actor.id for log in logs)


class TestBulkRetryBlocksUnsafeStates:
    def test_unknown_is_blocked_not_retried(self, db_session, platform):
        actor = _make_user(db_session)
        cmd = _make_command(platform, "UNKNOWN")
        db_session.add(cmd)
        db_session.commit()

        result = _service(db_session).bulk_retry([cmd.id], actor_id=actor.id)

        assert result.items[0].outcome == UNKNOWN_REQUIRES_RESOLUTION
        db_session.refresh(cmd)
        assert cmd.status == "UNKNOWN"  # 상태가 전혀 바뀌지 않았다.

    def test_retry_wait_is_not_retryable_via_bulk(self, db_session, platform):
        actor = _make_user(db_session)
        cmd = _make_command(platform, "RETRY_WAIT")
        db_session.add(cmd)
        db_session.commit()

        result = _service(db_session).bulk_retry([cmd.id], actor_id=actor.id)

        assert result.items[0].outcome == NOT_RETRYABLE
        assert result.items[0].error_code == "CURRENT_STATUS_RETRY_WAIT"

    def test_running_is_not_retryable_via_bulk(self, db_session, platform):
        """정상 lease든 stale든 이 API로는 재처리하지 않는다 - 기존 복구 정책
        (recover_stale_running)만 RUNNING을 회수할 수 있다."""
        actor = _make_user(db_session)
        cmd = _make_command(platform, "RUNNING")
        db_session.add(cmd)
        db_session.commit()

        result = _service(db_session).bulk_retry([cmd.id], actor_id=actor.id)

        assert result.items[0].outcome == NOT_RETRYABLE
        assert result.items[0].error_code == "CURRENT_STATUS_RUNNING"

    def test_success_command_is_not_retryable(self, db_session, platform):
        """이미 성공한 명령은 재전송하지 않는다."""
        actor = _make_user(db_session)
        cmd = _make_command(platform, "SUCCESS")
        db_session.add(cmd)
        db_session.commit()

        result = _service(db_session).bulk_retry([cmd.id], actor_id=actor.id)
        assert result.items[0].outcome == NOT_RETRYABLE

    def test_cancelled_command_is_not_retryable(self, db_session, platform):
        actor = _make_user(db_session)
        cmd = _make_command(platform, "CANCELLED")
        db_session.add(cmd)
        db_session.commit()

        result = _service(db_session).bulk_retry([cmd.id], actor_id=actor.id)
        assert result.items[0].outcome == NOT_RETRYABLE

    def test_not_found_command(self, db_session, platform):
        actor = _make_user(db_session)
        result = _service(db_session).bulk_retry([999999], actor_id=actor.id)
        assert result.items[0].outcome == NOT_FOUND

    def test_unsupported_command_type_blocked(self, db_session, platform):
        actor = _make_user(db_session)
        cmd = _make_command(platform, "FAILED", command_type="SOME_FUTURE_TYPE", target_type="ETC")
        db_session.add(cmd)
        db_session.commit()

        result = _service(db_session).bulk_retry([cmd.id], actor_id=actor.id)
        assert result.items[0].outcome == NOT_RETRYABLE
        assert result.items[0].error_code == "UNSUPPORTED_COMMAND_TYPE"


class TestBulkRetryValidation:
    def test_empty_selection_rejected(self, db_session):
        with pytest.raises(OperationsRetryValidationError):
            _service(db_session).bulk_retry([], actor_id=1)

    def test_over_max_selection_rejected(self, db_session):
        with pytest.raises(OperationsRetryValidationError):
            _service(db_session).bulk_retry(list(range(1, MAX_BULK_RETRY_ITEMS + 2)), actor_id=1)

    def test_duplicate_ids_deduplicated(self, db_session, platform):
        actor = _make_user(db_session)
        cmd = _make_command(platform, "FAILED")
        db_session.add(cmd)
        db_session.commit()

        result = _service(db_session).bulk_retry([cmd.id, cmd.id, cmd.id], actor_id=actor.id)

        assert len(result.items) == 1  # 같은 id 세 번 요청해도 결과는 한 건.
        assert result.items[0].outcome == RETRIED


class TestBulkRetryPartialFailureAndIdempotency:
    def test_retrying_already_retried_command_is_not_retryable(self, db_session, platform):
        """같은 요청을 두 번 보내도(중복 클릭) 두 번째는 안전하게 거부된다 -
        첫 재처리로 이미 PENDING이 됐으므로 더 이상 FAILED가 아니다."""
        actor = _make_user(db_session)
        cmd = _make_command(platform, "FAILED")
        db_session.add(cmd)
        db_session.commit()

        service = _service(db_session)
        first = service.bulk_retry([cmd.id], actor_id=actor.id)
        assert first.items[0].outcome == RETRIED

        second = service.bulk_retry([cmd.id], actor_id=actor.id)
        assert second.items[0].outcome == NOT_RETRYABLE
        assert second.items[0].error_code == "CURRENT_STATUS_PENDING"

    def test_one_retryable_and_one_blocked_both_get_independent_outcomes(self, db_session, platform):
        """일부 실패가 전체 성공으로 표시되지 않는다 - 항목별 결과를 각각 돌려준다."""
        actor = _make_user(db_session)
        retryable = _make_command(platform, "FAILED")
        blocked = _make_command(platform, "UNKNOWN")
        db_session.add_all([retryable, blocked])
        db_session.commit()

        result = _service(db_session).bulk_retry([retryable.id, blocked.id], actor_id=actor.id)

        outcomes = {i.command_id: i.outcome for i in result.items}
        assert outcomes[retryable.id] == RETRIED
        assert outcomes[blocked.id] == UNKNOWN_REQUIRES_RESOLUTION
        assert result.aborted is False


class TestResolveUnknown:
    def test_confirmed_success_transitions_to_success(self, db_session, platform):
        actor = _make_user(db_session)
        cmd = _make_command(platform, "UNKNOWN")
        db_session.add(cmd)
        db_session.commit()

        updated = _service(db_session).resolve_unknown(
            cmd.id, "CONFIRMED_SUCCESS", "쿠팡 판매자센터에서 전송 완료 확인함", actor_id=actor.id
        )
        assert updated.status == "SUCCESS"

    def test_confirmed_not_sent_returns_to_pending(self, db_session, platform):
        actor = _make_user(db_session)
        cmd = _make_command(platform, "UNKNOWN")
        db_session.add(cmd)
        db_session.commit()

        updated = _service(db_session).resolve_unknown(
            cmd.id, "CONFIRMED_NOT_SENT", "채널 관리자 화면에 미반영 확인함", actor_id=actor.id
        )
        assert updated.status == "PENDING"

    def test_confirmed_failed_transitions_to_failed(self, db_session, platform):
        actor = _make_user(db_session)
        cmd = _make_command(platform, "UNKNOWN")
        db_session.add(cmd)
        db_session.commit()

        updated = _service(db_session).resolve_unknown(
            cmd.id, "CONFIRMED_FAILED", "채널이 거부 처리했음을 확인함", actor_id=actor.id
        )
        assert updated.status == "FAILED"

    def test_evidence_note_required_minimum_length(self, db_session, platform):
        actor = _make_user(db_session)
        cmd = _make_command(platform, "UNKNOWN")
        db_session.add(cmd)
        db_session.commit()

        with pytest.raises(OperationsRetryValidationError):
            _service(db_session).resolve_unknown(cmd.id, "CONFIRMED_SUCCESS", "ok", actor_id=actor.id)

    def test_unknown_resolution_value_rejected(self, db_session, platform):
        actor = _make_user(db_session)
        cmd = _make_command(platform, "UNKNOWN")
        db_session.add(cmd)
        db_session.commit()

        with pytest.raises(OperationsRetryValidationError):
            _service(db_session).resolve_unknown(cmd.id, "MAYBE", "충분히 긴 근거 텍스트입니다", actor_id=actor.id)

    def test_missing_command_raises(self, db_session, platform):
        actor = _make_user(db_session)
        with pytest.raises(OperationsRetryValidationError):
            _service(db_session).resolve_unknown(
                999999, "CONFIRMED_SUCCESS", "충분히 긴 근거 텍스트입니다", actor_id=actor.id
            )

    def test_non_unknown_command_rejected_by_underlying_service(self, db_session, platform):
        """UNKNOWN이 아닌 명령은 해소 대상이 아니다 - 하위 서비스의 기존 검증이 막는다."""
        actor = _make_user(db_session)
        cmd = _make_command(platform, "FAILED")
        db_session.add(cmd)
        db_session.commit()

        with pytest.raises(ValueError):
            _service(db_session).resolve_unknown(
                cmd.id, "CONFIRMED_SUCCESS", "충분히 긴 근거 텍스트입니다", actor_id=actor.id
            )

    def test_audit_log_captures_evidence_note(self, db_session, platform):
        actor = _make_user(db_session)
        cmd = _make_command(platform, "UNKNOWN")
        db_session.add(cmd)
        db_session.commit()

        _service(db_session).resolve_unknown(
            cmd.id,
            "CONFIRMED_SUCCESS",
            "쿠팡 판매자센터 주문상세에서 배송중 상태 확인(2026-03-05 10:00)",
            actor_id=actor.id,
        )

        log = (
            db_session.query(AuditLog)
            .filter_by(entity_type="EXTERNAL_COMMAND", entity_id=cmd.id, command="operations.resolve_unknown")
            .one()
        )
        assert log.changed_by == actor.id
        assert "쿠팡 판매자센터" in log.reason


class TestListUnknownActions:
    def test_unknown_command_has_three_actions(self, db_session, platform):
        cmd = _make_command(platform, "UNKNOWN")
        db_session.add(cmd)
        db_session.commit()

        actions = _service(db_session).list_unknown_actions(cmd.id)
        assert set(actions) == {"CONFIRMED_NOT_SENT", "CONFIRMED_SUCCESS", "CONFIRMED_FAILED"}

    def test_non_unknown_command_has_no_actions(self, db_session, platform):
        cmd = _make_command(platform, "FAILED")
        db_session.add(cmd)
        db_session.commit()

        assert _service(db_session).list_unknown_actions(cmd.id) == []

    def test_missing_command_has_no_actions(self, db_session, platform):
        assert _service(db_session).list_unknown_actions(999999) == []
