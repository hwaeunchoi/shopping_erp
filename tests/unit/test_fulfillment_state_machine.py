"""
tests/unit/test_fulfillment_state_machine.py
------------------------------------------------
services.fulfillment_state_machine의 허용 전이표 - services.order_state_machine
테스트와 동일 원칙(허용된 것/금지된 것을 명시적으로 나열).
"""

import pytest

from services.fulfillment_state_machine import (
    ALL_STATUSES,
    ALLOWED_TRANSITIONS,
    INVENTORY_DEDUCTED_STATUSES,
    InvalidFulfillmentTransitionError,
    is_transition_allowed,
    validate_transition,
)


class TestAllowedTransitions:
    @pytest.mark.parametrize(
        "from_status,to_status",
        [
            ("READY", "PICKING"),
            ("PICKING", "PICKED"),
            ("PICKED", "VERIFYING"),
            ("VERIFYING", "VERIFIED"),
            ("VERIFIED", "PACKED"),
            ("PACKED", "SUBMIT_PENDING"),
            ("SUBMIT_PENDING", "SUBMITTED"),
            ("SUBMIT_PENDING", "PACKED"),
            ("BLOCKED", "PICKING"),
        ],
    )
    def test_allowed(self, from_status, to_status):
        assert is_transition_allowed(from_status, to_status) is True
        validate_transition(from_status, to_status)  # 예외 없이 통과해야 한다.

    @pytest.mark.parametrize(
        "from_status,to_status",
        [
            ("READY", "PACKED"),  # 검수 전 포장완료 차단
            ("PICKED", "PACKED"),  # 검수 없이 포장완료 차단
            ("PACKED", "PICKING"),  # 역행 금지
            ("SUBMITTED", "CANCELLED"),  # 이미 접수된 명령은 배치 항목만 취소 불가
            ("SUBMIT_PENDING", "CANCELLED"),
            ("CANCELLED", "READY"),  # 종결 상태에서 재활성화 불가
        ],
    )
    def test_blocked(self, from_status, to_status):
        assert is_transition_allowed(from_status, to_status) is False
        with pytest.raises(InvalidFulfillmentTransitionError):
            validate_transition(from_status, to_status)

    def test_same_status_is_noop(self):
        for status_value in ALL_STATUSES:
            assert is_transition_allowed(status_value, status_value) is True

    def test_all_statuses_have_transition_entries(self):
        assert set(ALLOWED_TRANSITIONS.keys()) == ALL_STATUSES


class TestInventoryDeductedStatuses:
    def test_packed_and_beyond_are_deducted(self):
        assert {"PACKED", "SUBMIT_PENDING", "SUBMITTED"} == INVENTORY_DEDUCTED_STATUSES

    def test_pre_pack_statuses_are_not_deducted(self):
        for status_value in ("READY", "PICKING", "PICKED", "VERIFYING", "VERIFIED", "BLOCKED", "CANCELLED"):
            assert status_value not in INVENTORY_DEDUCTED_STATUSES
