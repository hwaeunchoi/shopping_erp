"""
services/cs_case_service.py
------------------------------
상용 ERP 확장(5단계, B묶음) - CS(고객문의) 케이스 업무로직.

내부 메모/첨부파일 메타데이터는 새 테이블을 만들지 않고 기존
models.extra.Memo/Attachment를 target_type="CS_CASE"로 재사용한다(둘 다
이미 target_type/target_id 다형성 패턴을 쓰고 있어 그대로 맞는다). 실제
파일 업로드 저장소는 이 코드베이스 어디에도 없으므로 이번 단계에서 새로
만들지 않는다 - Attachment 메타데이터 조회/등록만 지원하고, 실제 업로드는
"미지원"으로 API 문서에 명시한다.

고객에게 보낼 답변(reply_draft)과 내부 메모(Memo)는 완전히 다른 저장소다 -
절대 섞이지 않는다. reply_draft는 CsCase 자체의 컬럼이고, 내부 메모는
Memo 테이블에 별도로 쌓인다. 이번 단계에서는 어떤 채널로도 실제 답변
전송(outbox)을 구현하지 않는다(services/cs_channel_sync_service.py
모듈 docstring 및 docs/COMMERCIAL_ERP_ROADMAP.md 5-B단계 절 참고 - 공식
계약상 안전하게 확정할 수 없는 필드가 있어 초안 저장까지만 지원한다).

상태 전이:
- CLOSED 상태는 생성 시 절대 받지 않는다(create_case가 status를 파라미터로
  받지 않고 항상 "OPEN"으로 고정 생성).
- CLOSED 건은 담당자/상태/메모/답변초안 등 무엇이든 수정할 수 없다 - 아래
  _ensure_not_closed()가 모든 수정 경로 앞단에서 막는다. 재오픈은
  reopen_case() 전용 메서드로만 가능하다(change_status의 일반 경로가 아님 -
  services/cs_state_machine.py 모듈 docstring 참고).
- RESOLVED가 아닌 상태에서 CLOSED로 바로 가는 것은 change_status에서
  명시적으로 막는다(전이표 자체도 RESOLVED->CLOSED만 허용).
- IN_PROGRESS로 전환하려면 담당자가 먼저 배정돼 있어야 한다(업무 요구:
  담당자 없는 진행중 상태를 허용하지 않는다 - 화면에서 "담당자 미배정"으로
  방치되는 것을 방지).
- 상태변경/답변초안 요청은 expected_status(낙관적 동시성 - 화면이 본 마지막
  상태)를, 담당자 배정 요청은 expected_assignee_id(화면이 본 마지막 담당자)를
  받아 CsCaseRepository.claim_transition()/claim_field_update()/
  claim_assignee()의 원자적 UPDATE...WHERE로 stale 덮어쓰기를 막는다(배정은
  status를 바꾸지 않으므로 status가 아니라 담당자 자체를 기준으로 삼는다 -
  안 그러면 "두 사람이 같은 미배정 건에 서로 다른 담당자를 동시에 배정"하는
  경합을 못 잡는다).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from models.cs_case import CS_CASE_INQUIRY_TYPES, CS_CASE_PRIORITIES, CsCase, CsCaseHistory
from models.extra import Attachment, Memo
from repositories.cs_case_repository import CsCaseHistoryRepository, CsCaseRepository
from repositories.extra_repository import AttachmentRepository, MemoRepository
from services.cs_state_machine import InvalidCsCaseTransitionError, validate_transition

# 같은 주문에 대해 같은 문의유형으로 이 기간 안에 아직 안 끝난 케이스가 있으면
# "중복 문의 감지" 경고 대상으로 본다(자동 병합하지 않고 화면에 안내만 한다).
DUPLICATE_LOOKBACK_HOURS = 24
_VALID_CLAIM_TYPES = frozenset({"EXCHANGE", "RETURN", "CANCELLATION"})


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CsCaseValidationError(ValueError):
    """CS 케이스 생성/수정 요청이 업무 규칙을 위반할 때 던진다(안전한 메시지만 담는다)."""


class CsCaseConflictError(Exception):
    """expected_status가 현재 상태와 달라 원자적 전이가 실패했을 때(동시 변경/화면
    stale) 던진다 - API는 이를 409로 변환한다."""


@dataclass
class CreateCaseResult:
    case: CsCase
    duplicate_of_case_id: Optional[int] = None


@dataclass
class BulkCaseOutcome:
    case_id: int
    outcome: str  # ACCEPTED/BLOCKED/VALIDATION_FAILED/NOT_FOUND
    error_code: Optional[str] = None


class CsCaseService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.case_repo = CsCaseRepository(session)
        self.history_repo = CsCaseHistoryRepository(session)
        self.memo_repo = MemoRepository(session)
        self.attachment_repo = AttachmentRepository(session)

    # --- 생성 -----------------------------------------------------------

    def create_case(
        self,
        *,
        inquiry_type: str,
        customer_message: str,
        priority: str = "NORMAL",
        subject: Optional[str] = None,
        order_id: Optional[int] = None,
        order_item_id: Optional[int] = None,
        product_option_id: Optional[int] = None,
        shipment_id: Optional[int] = None,
        fulfillment_batch_item_id: Optional[int] = None,
        claim_type: Optional[str] = None,
        claim_id: Optional[int] = None,
        customer_id: Optional[int] = None,
        due_at: Optional[datetime] = None,
        tags: Optional[str] = None,
        created_by: Optional[int] = None,
    ) -> CreateCaseResult:
        """수기 CS 케이스 생성 - status는 파라미터로 받지 않고 항상 OPEN으로
        고정한다(CLOSED 직접 생성 금지 요구사항을 구조적으로 만족)."""
        if inquiry_type not in CS_CASE_INQUIRY_TYPES:
            raise CsCaseValidationError(f"알 수 없는 문의유형입니다: {inquiry_type}")
        if priority not in CS_CASE_PRIORITIES:
            raise CsCaseValidationError(f"알 수 없는 우선순위입니다: {priority}")
        if not customer_message or not customer_message.strip():
            raise CsCaseValidationError("문의 내용을 입력해야 합니다.")
        if claim_type is not None and claim_type not in _VALID_CLAIM_TYPES:
            raise CsCaseValidationError(f"알 수 없는 클레임 유형입니다: {claim_type}")
        if claim_type is not None and claim_id is None:
            raise CsCaseValidationError("클레임 유형을 지정하려면 claim_id도 함께 지정해야 합니다.")

        duplicate: Optional[CsCase] = None
        if order_id is not None:
            since = _now() - timedelta(hours=DUPLICATE_LOOKBACK_HOURS)
            duplicate = self.case_repo.find_recent_duplicate(order_id=order_id, inquiry_type=inquiry_type, since=since)

        now = _now()
        case = self.case_repo.add(
            CsCase(
                inquiry_type=inquiry_type,
                priority=priority,
                status="OPEN",
                subject=subject,
                customer_message=customer_message,
                order_id=order_id,
                order_item_id=order_item_id,
                product_option_id=product_option_id,
                shipment_id=shipment_id,
                fulfillment_batch_item_id=fulfillment_batch_item_id,
                claim_type=claim_type,
                claim_id=claim_id,
                customer_id=customer_id,
                due_at=due_at,
                tags=tags,
                created_by=created_by,
                last_customer_message_at=now,
            )
        )
        self._record_history(case.id, "CREATED", None, "OPEN", created_by, None)
        return CreateCaseResult(case=case, duplicate_of_case_id=duplicate.id if duplicate else None)

    # --- 조회 -----------------------------------------------------------

    def get_case(self, case_id: int) -> Optional[CsCase]:
        return self.case_repo.get_by_id(case_id)

    def list_cases(
        self,
        *,
        status: Optional[str] = None,
        platform_id: Optional[int] = None,
        inquiry_type: Optional[str] = None,
        priority: Optional[str] = None,
        assignee_id: Optional[int] = None,
        unassigned_only: bool = False,
        overdue_only: bool = False,
        search: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CsCase]:
        return self.case_repo.list_filtered(
            status=status,
            platform_id=platform_id,
            inquiry_type=inquiry_type,
            priority=priority,
            assignee_id=assignee_id,
            unassigned_only=unassigned_only,
            overdue_only=overdue_only,
            search=search,
            limit=limit,
            offset=offset,
        )

    def list_history(self, case_id: int) -> list[CsCaseHistory]:
        return self.history_repo.list_by_case(case_id)

    def list_memos(self, case_id: int) -> list[Memo]:
        return self.memo_repo.list_by_target("CS_CASE", case_id)

    def list_attachments(self, case_id: int) -> list[Attachment]:
        return self.attachment_repo.list_by_target("CS_CASE", case_id)

    def dashboard_summary(self) -> dict[str, Any]:
        return {
            "by_status": self.case_repo.count_by_status(),
            "unassigned_count": self.case_repo.count_unassigned(),
            "overdue_count": self.case_repo.count_overdue(),
        }

    # --- 배정/상태전이/메모/답변초안 -------------------------------------

    def assign(
        self, case_id: int, assignee_id: Optional[int], expected_assignee_id: Optional[int], actor: Optional[int]
    ) -> CsCase:
        """expected_assignee_id는 화면이 마지막으로 본 담당자(미배정이면 None)다 -
        상태(status)가 아니라 담당자 자체를 낙관적 동시성 기준으로 쓴다(배정은
        status를 바꾸지 않으므로 status 기준 가드로는 "두 사람이 동시에 같은
        건에 서로 다른 담당자를 배정"하는 경합을 잡지 못한다)."""
        case = self._require_case(case_id)
        self._ensure_not_closed(case)
        updated = self.case_repo.claim_assignee(case_id, expected_assignee_id, assignee_id)
        if not updated:
            raise CsCaseConflictError(
                f"현재 담당자가 예상({expected_assignee_id})과 다르거나 이미 변경되었습니다: case_id={case_id}"
            )
        self._record_history(
            case_id,
            "ASSIGNED",
            str(expected_assignee_id) if expected_assignee_id is not None else None,
            str(assignee_id) if assignee_id is not None else None,
            actor,
            None,
        )
        self.session.refresh(case)
        return case

    def change_status(self, case_id: int, new_status: str, expected_status: str, actor: Optional[int]) -> CsCase:
        case = self._require_case(case_id)
        self._ensure_not_closed(case)
        try:
            validate_transition(expected_status, new_status)
        except InvalidCsCaseTransitionError as e:
            raise CsCaseValidationError(str(e)) from e
        if new_status == "IN_PROGRESS" and case.assignee_id is None:
            raise CsCaseValidationError("담당자를 먼저 배정해야 진행중으로 전환할 수 있습니다.")
        if new_status == "CLOSED" and expected_status != "RESOLVED":
            raise CsCaseValidationError("해결완료(RESOLVED) 상태에서만 종결할 수 있습니다.")

        now = _now()
        extra: dict[str, Any] = {}
        if new_status == "RESOLVED":
            extra["resolved_at"] = now
        if new_status == "CLOSED":
            extra["closed_at"] = now

        updated = self.case_repo.claim_transition(case_id, expected_status, new_status, **extra)
        if not updated:
            raise CsCaseConflictError(
                f"현재 상태가 예상({expected_status})과 다르거나 이미 변경되었습니다: case_id={case_id}"
            )
        self._record_history(case_id, "STATUS_CHANGE", expected_status, new_status, actor, None)
        self.session.refresh(case)
        return case

    def reopen_case(self, case_id: int, expected_status: str, actor: Optional[int]) -> CsCase:
        """CLOSED 건을 OPEN으로 되돌리는 유일한 경로 - change_status()는 이 전이를
        허용하지 않는다."""
        if expected_status != "CLOSED":
            raise CsCaseValidationError("종결(CLOSED)된 건만 재오픈할 수 있습니다.")
        case = self._require_case(case_id)
        updated = self.case_repo.claim_transition(
            case_id, "CLOSED", "OPEN", closed_at=None, resolved_at=None, reopened_count=CsCase.reopened_count + 1
        )
        if not updated:
            raise CsCaseConflictError(f"현재 상태가 CLOSED가 아니거나 이미 변경되었습니다: case_id={case_id}")
        self._record_history(case_id, "REOPENED", "CLOSED", "OPEN", actor, None)
        self.session.refresh(case)
        return case

    def add_memo(self, case_id: int, content: str, actor: Optional[int]) -> Memo:
        case = self._require_case(case_id)
        self._ensure_not_closed(case)
        if not content or not content.strip():
            raise CsCaseValidationError("메모 내용을 입력해야 합니다.")
        memo = self.memo_repo.add(
            Memo(target_type="CS_CASE", target_id=case_id, content=content, created_by=actor, created_at=_now())
        )
        self.case_repo.claim_field_update(case_id, case.status, last_agent_response_at=_now())
        self._record_history(case_id, "MEMO_ADDED", None, None, actor, None)
        return memo

    def update_reply_draft(self, case_id: int, reply_draft: str, expected_status: str, actor: Optional[int]) -> CsCase:
        case = self._require_case(case_id)
        self._ensure_not_closed(case)
        updated = self.case_repo.claim_field_update(
            case_id, expected_status, reply_draft=reply_draft, last_agent_response_at=_now()
        )
        if not updated:
            raise CsCaseConflictError(
                f"현재 상태가 예상({expected_status})과 다르거나 이미 변경되었습니다: case_id={case_id}"
            )
        self._record_history(case_id, "REPLY_DRAFT_UPDATED", None, None, actor, None)
        self.session.refresh(case)
        return case

    # --- 대량처리 ---------------------------------------------------------

    def bulk_assign(
        self, case_ids: list[int], assignee_id: Optional[int], actor: Optional[int]
    ) -> list[BulkCaseOutcome]:
        results: list[BulkCaseOutcome] = []
        for case_id in case_ids:
            case = self.case_repo.get_by_id(case_id)
            if case is None:
                results.append(BulkCaseOutcome(case_id, "NOT_FOUND"))
                continue
            if case.status == "CLOSED":
                results.append(BulkCaseOutcome(case_id, "BLOCKED", "CASE_CLOSED"))
                continue
            old_assignee = case.assignee_id
            updated = self.case_repo.claim_assignee(case_id, old_assignee, assignee_id)
            if not updated:
                results.append(BulkCaseOutcome(case_id, "BLOCKED", "CONCURRENT_UPDATE"))
                continue
            self._record_history(
                case_id,
                "ASSIGNED",
                str(old_assignee) if old_assignee is not None else None,
                str(assignee_id) if assignee_id is not None else None,
                actor,
                "bulk",
            )
            results.append(BulkCaseOutcome(case_id, "ACCEPTED"))
        return results

    def bulk_change_status(self, case_ids: list[int], new_status: str, actor: Optional[int]) -> list[BulkCaseOutcome]:
        results: list[BulkCaseOutcome] = []
        for case_id in case_ids:
            case = self.case_repo.get_by_id(case_id)
            if case is None:
                results.append(BulkCaseOutcome(case_id, "NOT_FOUND"))
                continue
            if case.status == "CLOSED":
                results.append(BulkCaseOutcome(case_id, "BLOCKED", "CASE_CLOSED"))
                continue
            try:
                validate_transition(case.status, new_status)
            except InvalidCsCaseTransitionError:
                results.append(BulkCaseOutcome(case_id, "VALIDATION_FAILED", "INVALID_TRANSITION"))
                continue
            if new_status == "IN_PROGRESS" and case.assignee_id is None:
                results.append(BulkCaseOutcome(case_id, "VALIDATION_FAILED", "ASSIGNEE_REQUIRED"))
                continue
            if new_status == "CLOSED" and case.status != "RESOLVED":
                results.append(BulkCaseOutcome(case_id, "VALIDATION_FAILED", "MUST_BE_RESOLVED_FIRST"))
                continue
            now = _now()
            extra: dict[str, Any] = {}
            if new_status == "RESOLVED":
                extra["resolved_at"] = now
            if new_status == "CLOSED":
                extra["closed_at"] = now
            updated = self.case_repo.claim_transition(case_id, case.status, new_status, **extra)
            if not updated:
                results.append(BulkCaseOutcome(case_id, "BLOCKED", "CONCURRENT_UPDATE"))
                continue
            self._record_history(case_id, "STATUS_CHANGE", case.status, new_status, actor, "bulk")
            results.append(BulkCaseOutcome(case_id, "ACCEPTED"))
        return results

    # --- 내부 헬퍼 --------------------------------------------------------

    def _require_case(self, case_id: int) -> CsCase:
        case = self.case_repo.get_by_id(case_id)
        if case is None:
            raise CsCaseValidationError(f"CS 케이스를 찾을 수 없습니다: id={case_id}")
        return case

    def _ensure_not_closed(self, case: CsCase) -> None:
        if case.status == "CLOSED":
            raise CsCaseValidationError("종결된 건은 수정할 수 없습니다(재오픈 후 다시 시도하세요).")

    def _record_history(
        self,
        case_id: int,
        action: str,
        from_value: Optional[str],
        to_value: Optional[str],
        actor: Optional[int],
        note: Optional[str],
    ) -> None:
        self.history_repo.add(
            CsCaseHistory(
                case_id=case_id,
                action=action,
                from_value=from_value,
                to_value=to_value,
                changed_by=actor,
                changed_at=_now(),
                note=note[:300] if note else None,
            )
        )
