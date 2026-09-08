"""
tests/unit/test_cs_state_machine.py
--------------------------------------
services.cs_state_machine의 허용 전이표 - services.fulfillment_state_machine
테스트와 동일 원칙(허용된 것/금지된 것을 명시적으로 나열).
"""

import pytest

from services.cs_state_machine import (
    ALL_STATUSES,
    ALLOWED_TRANSITIONS,
    InvalidCsCaseTransitionError,
    is_transition_allowed,
    validate_transition,
)


class TestAllowedTransitions:
    @pytest.mark.parametrize(
        "from_status,to_status",
        [
            ("OPEN", "IN_PROGRESS"),
            ("OPEN", "WAITING_CUSTOMER"),
            ("OPEN", "WAITING_CHANNEL"),
            ("OPEN", "RESOLVED"),
            ("IN_PROGRESS", "OPEN"),
            ("IN_PROGRESS", "WAITING_CUSTOMER"),
            ("WAITING_CUSTOMER", "IN_PROGRESS"),
            ("WAITING_CHANNEL", "IN_PROGRESS"),
            ("RESOLVED", "CLOSED"),
            ("RESOLVED", "OPEN"),
            ("RESOLVED", "IN_PROGRESS"),
            ("CLOSED", "OPEN"),
        ],
    )
    def test_allowed(self, from_status, to_status):
        assert is_transition_allowed(from_status, to_status) is True
        validate_transition(from_status, to_status)

    @pytest.mark.parametrize(
        "from_status,to_status",
        [
            ("OPEN", "CLOSED"),  # RESOLVED 전 CLOSED 차단
            ("IN_PROGRESS", "CLOSED"),
            ("WAITING_CUSTOMER", "CLOSED"),
            ("WAITING_CHANNEL", "CLOSED"),
            ("CLOSED", "IN_PROGRESS"),  # 재오픈(CLOSED->OPEN) 이외 전부 차단
            ("CLOSED", "WAITING_CUSTOMER"),
            ("CLOSED", "RESOLVED"),
        ],
    )
    def test_blocked(self, from_status, to_status):
        assert is_transition_allowed(from_status, to_status) is False
        with pytest.raises(InvalidCsCaseTransitionError):
            validate_transition(from_status, to_status)

    def test_same_status_is_noop(self):
        for status_value in ALL_STATUSES:
            assert is_transition_allowed(status_value, status_value) is True

    def test_all_statuses_have_transition_entries(self):
        assert set(ALLOWED_TRANSITIONS.keys()) == ALL_STATUSES
