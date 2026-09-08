"""
services/fulfillment_state_machine.py
------------------------------------------
FulfillmentBatchItem.status(models.fulfillment)의 허용된 상태 전이표 -
services.order_state_machine과 동일한 원칙(허용된 전이만 가능, 검증을 한
곳에만 둔다).

READY -> PICKING -> PICKED -> VERIFYING -> VERIFIED -> PACKED ->
SUBMIT_PENDING -> SUBMITTED가 정상 경로다. BLOCKED는 검수 수량 불일치처럼
운영자 판단이 필요한 정지 상태로, PICKING으로 되돌려 다시 시도하거나
CANCELLED로 종결할 수 있다. SUBMITTED 이후의 실제 채널 처리 결과는 이
상태머신이 아니라 ExternalCommand.status(SHIPMENT_SUBMIT)가 전담한다 -
이 모듈은 "물리적으로 무엇을 했는가"까지만 다룬다.
"""

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "READY": frozenset({"PICKING", "CANCELLED"}),
    "PICKING": frozenset({"PICKED", "BLOCKED", "CANCELLED"}),
    "PICKED": frozenset({"VERIFYING", "CANCELLED"}),
    "VERIFYING": frozenset({"VERIFIED", "BLOCKED"}),
    "VERIFIED": frozenset({"PACKED", "BLOCKED"}),
    "PACKED": frozenset({"SUBMIT_PENDING", "CANCELLED"}),
    "SUBMIT_PENDING": frozenset({"SUBMITTED", "PACKED"}),  # PACKED로: 접수 자체가 실패해 되돌아옴(재시도 가능).
    "SUBMITTED": frozenset(),
    "BLOCKED": frozenset({"PICKING", "CANCELLED"}),
    "CANCELLED": frozenset(),
}

ALL_STATUSES = frozenset(ALLOWED_TRANSITIONS.keys())

# 재고가 이미 차감된(포장완료 이후) 상태 - 취소하려면 재고 복원이 필요하다
# (services.fulfillment_service.FulfillmentService.cancel_item 참고). SUBMIT_PENDING/
# SUBMITTED는 취소 대상에서 제외한다(상태머신에도 CANCELLED로 가는 경로가 없다) -
# SUBMITTED가 되는 순간 ExternalCommand(PENDING)가 이미 생성되어 스케줄러가 언제든
# 이를 집어 채널로 보낼 수 있다. 그 명령 자체를 취소하는 기능은 아직 없으므로
# (outbox에 "취소" 개념이 없다), 여기서 배치 항목만 취소된 것처럼 보이게 하면
# 실제로는 채널 전송이 그대로 진행되는 오인 상태가 생긴다 - 그래서 포장완료
# (PACKED)까지만 취소를 허용한다.
INVENTORY_DEDUCTED_STATUSES = frozenset({"PACKED", "SUBMIT_PENDING", "SUBMITTED"})


class InvalidFulfillmentTransitionError(ValueError):
    """허용되지 않은 상태 전이를 시도했을 때 던진다."""

    def __init__(self, from_status: str, to_status: str) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"허용되지 않은 출고 상태 전이입니다: {from_status} -> {to_status}")


def is_transition_allowed(from_status: str, to_status: str) -> bool:
    if from_status == to_status:
        return True
    return to_status in ALLOWED_TRANSITIONS.get(from_status, frozenset())


def validate_transition(from_status: str, to_status: str) -> None:
    if not is_transition_allowed(from_status, to_status):
        raise InvalidFulfillmentTransitionError(from_status, to_status)
