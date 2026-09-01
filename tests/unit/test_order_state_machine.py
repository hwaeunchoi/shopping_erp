"""
tests/unit/test_order_state_machine.py
--------------------------------------------
services.order_state_machine의 허용/금지 전이표를 고정한다.
"""

import pytest

from services.order_state_machine import (
    ALL_STATUSES,
    InvalidOrderTransitionError,
    is_transition_allowed,
    validate_transition,
)


class TestAllowedTransitions:
    @pytest.mark.parametrize(
        "from_status,to_status",
        [
            ("NEW", "PREPARING"),
            ("NEW", "CANCELED"),
            ("PREPARING", "SHIPPING"),
            ("PREPARING", "CANCELED"),
            ("SHIPPING", "DELIVERED"),
            ("SHIPPING", "RETURNED"),
            ("SHIPPING", "CANCELED"),
            ("DELIVERED", "RETURNED"),
            ("DELIVERED", "EXCHANGED"),
            ("DELIVERED", "REFUNDED"),
        ],
    )
    def test_allowed(self, from_status, to_status):
        assert is_transition_allowed(from_status, to_status) is True
        validate_transition(from_status, to_status)  # 예외 없이 통과

    def test_same_status_is_a_noop_not_an_error(self):
        for status in ALL_STATUSES:
            assert is_transition_allowed(status, status) is True


class TestForbiddenTransitions:
    @pytest.mark.parametrize(
        "from_status,to_status",
        [
            ("DELIVERED", "NEW"),  # 역행 금지
            ("SHIPPING", "PREPARING"),  # 역행 금지
            ("CANCELED", "NEW"),  # 종결 상태에서 전이 금지
            ("CANCELED", "SHIPPING"),
            ("RETURNED", "DELIVERED"),
            ("NEW", "DELIVERED"),  # 단계 건너뛰기 금지
            ("NEW", "SHIPPING"),
        ],
    )
    def test_forbidden(self, from_status, to_status):
        assert is_transition_allowed(from_status, to_status) is False
        with pytest.raises(InvalidOrderTransitionError):
            validate_transition(from_status, to_status)
