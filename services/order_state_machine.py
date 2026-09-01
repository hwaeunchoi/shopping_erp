"""
services/order_state_machine.py
------------------------------------
Order.status(models.order.Order)의 허용된 상태 전이표.

채널(네이버/쿠팡 등)에서 수집한 최신 상태를 내부 상태에 반영할 때, 그리고
내부 화면에서 수동으로 상태를 바꿀 때 둘 다 이 표를 거친다 - "허용된 전이만
가능"이라는 계약을 한 곳에만 둔다.

CANCELED/RETURNED/EXCHANGED/REFUNDED는 이번 1단계(송장/상태 동기화) 범위에서는
종결 상태로 취급한다(전이 없음) - 반품/교환/정산 이후 흐름은 2단계에서 다룬다.
"""

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "NEW": frozenset({"PREPARING", "CANCELED"}),
    "PREPARING": frozenset({"SHIPPING", "CANCELED"}),
    "SHIPPING": frozenset({"DELIVERED", "RETURNED", "CANCELED"}),
    "DELIVERED": frozenset({"RETURNED", "EXCHANGED", "REFUNDED"}),
    "CANCELED": frozenset(),
    "RETURNED": frozenset(),
    "EXCHANGED": frozenset(),
    "REFUNDED": frozenset(),
}

ALL_STATUSES = frozenset(ALLOWED_TRANSITIONS.keys())


class InvalidOrderTransitionError(ValueError):
    """허용되지 않은 상태 전이를 시도했을 때 던진다."""

    def __init__(self, from_status: str, to_status: str) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"허용되지 않은 상태 전이입니다: {from_status} -> {to_status}")


def is_transition_allowed(from_status: str, to_status: str) -> bool:
    """from_status에서 to_status로의 전이가 허용되는지 확인한다.

    같은 상태로의 "전이"(from_status == to_status)는 변경이 아니므로 True다 -
    호출부가 no-op으로 처리할 수 있게 한다."""
    if from_status == to_status:
        return True
    return to_status in ALLOWED_TRANSITIONS.get(from_status, frozenset())


def validate_transition(from_status: str, to_status: str) -> None:
    """허용되지 않으면 InvalidOrderTransitionError를 던진다."""
    if not is_transition_allowed(from_status, to_status):
        raise InvalidOrderTransitionError(from_status, to_status)
