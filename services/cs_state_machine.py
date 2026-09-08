"""
services/cs_state_machine.py
------------------------------
CS 케이스 상태 전이 검증표. services/fulfillment_state_machine.py와 동일한
설계 원칙(허용 전이는 dict[str, frozenset[str]], 같은 상태로의 전이는 항상
허용되는 no-op, 서버가 최종 검증).

CLOSED -> OPEN(재오픈)은 이 표에서는 "구조적으로 유효한 상태 변화"로
허용하지만, 실제로는 services.cs_case_service.CsCaseService.change_status()가
from_status == "CLOSED"인 모든 일반 전이 요청을 무조건 거부하고
reopen_case() 전용 메서드로만 CLOSED -> OPEN을 수행한다(services.
fulfillment_service.FulfillmentService.cancel_item()이 일반 change_status
경로 밖에서 전용 메서드로만 취소를 허용하는 것과 동일한 방어 원칙 -
"전용 동작으로만 허용"이라는 요구사항을 상태표 자체가 아니라 서비스
계층에서 강제한다).

CLOSED에서 재오픈 이외의 다른 모든 전이는 이 표 자체에서 막는다(예:
CLOSED -> RESOLVED, CLOSED -> WAITING_CUSTOMER는 애초에 표에 없음).
"""

from models.cs_case import CS_CASE_STATUSES

ALL_STATUSES = CS_CASE_STATUSES

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "OPEN": frozenset({"IN_PROGRESS", "WAITING_CUSTOMER", "WAITING_CHANNEL", "RESOLVED"}),
    "IN_PROGRESS": frozenset({"OPEN", "WAITING_CUSTOMER", "WAITING_CHANNEL", "RESOLVED"}),
    "WAITING_CUSTOMER": frozenset({"OPEN", "IN_PROGRESS", "WAITING_CHANNEL", "RESOLVED"}),
    "WAITING_CHANNEL": frozenset({"OPEN", "IN_PROGRESS", "WAITING_CUSTOMER", "RESOLVED"}),
    "RESOLVED": frozenset({"OPEN", "IN_PROGRESS", "CLOSED"}),
    # CLOSED -> OPEN만 구조적으로 유효(재오픈 전용 메서드 경유) - 그 외는 전부 차단.
    "CLOSED": frozenset({"OPEN"}),
}


class InvalidCsCaseTransitionError(ValueError):
    """허용되지 않는 CS 케이스 상태 전이를 요청했을 때 던진다."""


def is_transition_allowed(from_status: str, to_status: str) -> bool:
    if from_status == to_status:
        return True
    return to_status in ALLOWED_TRANSITIONS.get(from_status, frozenset())


def validate_transition(from_status: str, to_status: str) -> None:
    if not is_transition_allowed(from_status, to_status):
        raise InvalidCsCaseTransitionError(f"허용되지 않는 상태 전이입니다: {from_status} -> {to_status}")
