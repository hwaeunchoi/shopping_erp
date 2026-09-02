"""
services/claim_state_machine.py
------------------------------------
취소/반품/교환(클레임) 재수집 시 "오래된 응답이 최신 상태를 되돌리지 않게" 하는
허용 전이 테이블 - services/order_state_machine.py와 같은 설계 원칙이다.

REVIEW는 이 상태머신의 일반 상태가 아니라 특수값이다: 알 수 없는 원본 상태 코드를
만났을 때만 붙이며(normalize_claim_status), 어떤 인식된 상태도 REVIEW로 "다운그레이드"
되지 않는다(정보량이 더 적은 상태로 되돌리지 않기 위함) - 반대로 REVIEW에서는 어떤
인식된 상태로도 올라갈 수 있다(처음으로 원본 상태를 이해하게 된 경우).
"""

from typing import Optional

REVIEW_STATUS = "REVIEW"

CANCELLATION_TRANSITIONS: dict[str, frozenset[str]] = {"REQUESTED": frozenset({"COMPLETED"}), "COMPLETED": frozenset()}

RETURN_TRANSITIONS: dict[str, frozenset[str]] = {
    "REQUESTED": frozenset({"APPROVED", "REJECTED"}),
    "APPROVED": frozenset({"RECEIVED", "REJECTED"}),
    "RECEIVED": frozenset({"REFUNDED"}),
    "REFUNDED": frozenset(),
    "REJECTED": frozenset(),
}

EXCHANGE_TRANSITIONS: dict[str, frozenset[str]] = {
    "REQUESTED": frozenset({"APPROVED", "REJECTED"}),
    "APPROVED": frozenset({"SHIPPED", "REJECTED"}),
    "SHIPPED": frozenset({"COMPLETED"}),
    "COMPLETED": frozenset(),
    "REJECTED": frozenset(),
}


def should_apply_claim_status(
    current_status: str, incoming_status: str, transitions: dict[str, frozenset[str]]
) -> bool:
    """재수집으로 들어온 incoming_status를 실제로 반영해도 되는지 판단한다.

    - 같은 값이면 반영할 필요가 없다(no-op) -> False.
    - incoming이 REVIEW면 정보량이 더 적으므로 이미 알고 있는 상태를 덮어쓰지
      않는다 -> False(단, 현재도 REVIEW라면 위의 동일값 케이스로 이미 처리됨).
    - 현재가 REVIEW면 인식된 상태로는 항상 올라갈 수 있다(REVIEW 자체가 아닌 한) -> True.
    - 그 외에는 허용 전이 테이블에 있는 전진 전이만 허용한다 - 뒤로 가는 전이(예:
      REFUNDED인데 REQUESTED가 들어옴)는 오래된 응답으로 보고 무시한다.
    """
    if current_status == incoming_status:
        return False
    if incoming_status == REVIEW_STATUS:
        return False
    if current_status == REVIEW_STATUS:
        return True
    return incoming_status in transitions.get(current_status, frozenset())


def normalize_claim_status(raw_status: Optional[str], mapping: dict[str, str]) -> str:
    """원본 상태 코드를 내부 정규화 상태로 변환한다. 매핑에 없으면 완료 등으로
    추정하지 않고 REVIEW로 보존한다(운영자 확인 필요)."""
    if raw_status is None:
        return REVIEW_STATUS
    return mapping.get(raw_status, REVIEW_STATUS)
