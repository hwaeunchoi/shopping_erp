"""
tests/unit/test_cs_case_service.py
--------------------------------------
services.cs_case_service.CsCaseService - CS(고객문의) 케이스 업무로직 검증.

합성(테스트용) 문의 본문/이름만 사용한다 - 실제 고객 개인정보는 어디에도
쓰지 않는다.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from models.order import Order, OrderItem
from models.user import Role, User
from services.cs_case_service import CsCaseConflictError, CsCaseService, CsCaseValidationError


def _service(db_session) -> CsCaseService:
    return CsCaseService(db_session)


def _make_user(db_session, username: Optional[str] = None) -> User:
    role = Role(name=f"CS-TEST-ROLE-{uuid.uuid4().hex[:8]}")
    db_session.add(role)
    db_session.flush()
    user = User(
        username=username or f"cs-agent-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        name="CS 상담원",
        role_id=role.id,
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()
    return user


def _make_order(db_session, platform, order_no: Optional[str] = None) -> Order:
    order = Order(
        platform_id=platform.id,
        platform_order_no=order_no or f"CS-TEST-{uuid.uuid4().hex[:8]}",
        status="NEW",
        order_date=datetime.now(timezone.utc),
        total_amount=10000,
    )
    db_session.add(order)
    db_session.flush()
    return order


def _make_order_item(db_session, order: Order, product_option, quantity: int = 1) -> OrderItem:
    item = OrderItem(
        order_id=order.id,
        product_option_id=product_option.id,
        quantity=quantity,
        unit_price=1000,
        line_amount=1000 * quantity,
        platform_order_item_no=f"CS-LINE-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(item)
    db_session.flush()
    return item


def _to_resolved(service, db_session, case_id: int) -> None:
    """OPEN -> (담당자 배정) -> IN_PROGRESS -> RESOLVED까지 진행시키는 테스트 전용 헬퍼."""
    agent = _make_user(db_session)
    service.assign(case_id, agent.id, None, actor=None)
    service.change_status(case_id, "IN_PROGRESS", "OPEN", actor=None)
    service.change_status(case_id, "RESOLVED", "IN_PROGRESS", actor=None)


def _to_closed(service, db_session, case_id: int) -> None:
    _to_resolved(service, db_session, case_id)
    service.change_status(case_id, "CLOSED", "RESOLVED", actor=None)


class TestCreateCase:
    def test_creates_open_case(self, db_session, platform):
        service = _service(db_session)
        result = service.create_case(inquiry_type="DELIVERY", customer_message="배송이 늦어요", created_by=None)
        assert result.case.status == "OPEN"
        assert result.case.id is not None
        assert result.duplicate_of_case_id is None

    def test_unknown_inquiry_type_rejected(self, db_session):
        service = _service(db_session)
        with pytest.raises(CsCaseValidationError):
            service.create_case(inquiry_type="NOT_A_TYPE", customer_message="문의")

    def test_unknown_priority_rejected(self, db_session):
        service = _service(db_session)
        with pytest.raises(CsCaseValidationError):
            service.create_case(inquiry_type="ETC", customer_message="문의", priority="SUPER_URGENT")

    def test_empty_message_rejected(self, db_session):
        service = _service(db_session)
        with pytest.raises(CsCaseValidationError):
            service.create_case(inquiry_type="ETC", customer_message="   ")

    def test_cannot_create_as_closed(self, db_session):
        """create_case()는 status 파라미터 자체를 받지 않는다 - 시그니처로 이미
        차단되지만, 항상 OPEN으로 생성된다는 사실 자체를 회귀 검증한다."""
        service = _service(db_session)
        result = service.create_case(inquiry_type="ETC", customer_message="문의")
        assert result.case.status != "CLOSED"

    def test_links_order_and_history_recorded(self, db_session, platform, product_option):
        service = _service(db_session)
        order = _make_order(db_session, platform)
        item = _make_order_item(db_session, order, product_option)
        result = service.create_case(
            inquiry_type="DELIVERY", customer_message="문의", order_id=order.id, order_item_id=item.id
        )
        assert result.case.order_id == order.id
        assert result.case.order_item_id == item.id
        history = service.list_history(result.case.id)
        assert len(history) == 1
        assert history[0].action == "CREATED"
        assert history[0].to_value == "OPEN"

    def test_links_claim(self, db_session):
        service = _service(db_session)
        result = service.create_case(
            inquiry_type="EXCHANGE_RETURN", customer_message="교환 문의", claim_type="EXCHANGE", claim_id=42
        )
        assert result.case.claim_type == "EXCHANGE"
        assert result.case.claim_id == 42

    def test_unknown_claim_type_rejected(self, db_session):
        service = _service(db_session)
        with pytest.raises(CsCaseValidationError):
            service.create_case(inquiry_type="ETC", customer_message="문의", claim_type="REFUND", claim_id=1)

    def test_claim_type_without_id_rejected(self, db_session):
        service = _service(db_session)
        with pytest.raises(CsCaseValidationError):
            service.create_case(inquiry_type="ETC", customer_message="문의", claim_type="EXCHANGE")

    def test_duplicate_detection_within_window(self, db_session, platform):
        service = _service(db_session)
        order = _make_order(db_session, platform)
        first = service.create_case(inquiry_type="DELIVERY", customer_message="문의1", order_id=order.id)
        second = service.create_case(inquiry_type="DELIVERY", customer_message="문의2", order_id=order.id)
        assert second.duplicate_of_case_id == first.case.id

    def test_duplicate_detection_ignores_different_inquiry_type(self, db_session, platform):
        service = _service(db_session)
        order = _make_order(db_session, platform)
        service.create_case(inquiry_type="DELIVERY", customer_message="문의1", order_id=order.id)
        second = service.create_case(inquiry_type="PRODUCT", customer_message="문의2", order_id=order.id)
        assert second.duplicate_of_case_id is None

    def test_duplicate_detection_ignores_resolved_case(self, db_session, platform):
        service = _service(db_session)
        order = _make_order(db_session, platform)
        first = service.create_case(inquiry_type="DELIVERY", customer_message="문의1", order_id=order.id)
        service.change_status(first.case.id, "RESOLVED", "OPEN", actor=None)
        second = service.create_case(inquiry_type="DELIVERY", customer_message="문의2", order_id=order.id)
        assert second.duplicate_of_case_id is None


class TestAssign:
    def test_assign_updates_and_records_history(self, db_session):
        service = _service(db_session)
        agent = _make_user(db_session)
        case = service.create_case(inquiry_type="ETC", customer_message="문의").case
        updated = service.assign(case.id, agent.id, None, actor=None)
        assert updated.assignee_id == agent.id
        history = service.list_history(case.id)
        assert history[-1].action == "ASSIGNED"
        assert history[-1].to_value == str(agent.id)

    def test_assign_stale_expected_assignee_conflicts(self, db_session):
        service = _service(db_session)
        agent = _make_user(db_session)
        wrong_previous = _make_user(db_session)
        case = service.create_case(inquiry_type="ETC", customer_message="문의").case
        with pytest.raises(CsCaseConflictError):
            service.assign(case.id, agent.id, wrong_previous.id, actor=None)

    def test_assign_blocked_on_closed_case(self, db_session):
        service = _service(db_session)
        case = service.create_case(inquiry_type="ETC", customer_message="문의").case
        _to_closed(service, db_session, case.id)
        other_agent = _make_user(db_session)
        with pytest.raises(CsCaseValidationError):
            service.assign(case.id, other_agent.id, case.assignee_id, actor=None)

    def test_concurrent_assignment_only_one_wins(self, db_session):
        """두 요청이 같은 expected_assignee_id(둘 다 "아직 미배정"인 화면)를 근거로
        서로 다른 담당자를 배정하려 하면(같은 커넥션 안에서의 논리적 검증 - 실제
        두 커넥션 경합은 격리 PostgreSQL 테스트가 담당) 두 번째는 반드시 충돌로
        막혀야 한다 - assign()이 status가 아니라 assignee_id 자체를 낙관적
        동시성 기준으로 쓰기 때문에 가능하다(만약 status만 확인했다면 배정은
        status를 바꾸지 않으므로 두 요청 모두 성공해버렸을 것이다)."""
        service = _service(db_session)
        agent_a = _make_user(db_session)
        agent_b = _make_user(db_session)
        case = service.create_case(inquiry_type="ETC", customer_message="문의").case
        service.assign(case.id, agent_a.id, None, actor=None)
        with pytest.raises(CsCaseConflictError):
            service.assign(case.id, agent_b.id, None, actor=None)


class TestChangeStatus:
    def test_in_progress_requires_assignee(self, db_session):
        service = _service(db_session)
        case = service.create_case(inquiry_type="ETC", customer_message="문의").case
        with pytest.raises(CsCaseValidationError):
            service.change_status(case.id, "IN_PROGRESS", "OPEN", actor=None)

    def test_in_progress_allowed_after_assignment(self, db_session):
        service = _service(db_session)
        agent = _make_user(db_session)
        case = service.create_case(inquiry_type="ETC", customer_message="문의").case
        service.assign(case.id, agent.id, None, actor=None)
        updated = service.change_status(case.id, "IN_PROGRESS", "OPEN", actor=None)
        assert updated.status == "IN_PROGRESS"

    def test_closed_requires_resolved_first(self, db_session):
        service = _service(db_session)
        case = service.create_case(inquiry_type="ETC", customer_message="문의").case
        with pytest.raises(CsCaseValidationError):
            service.change_status(case.id, "CLOSED", "OPEN", actor=None)

    def test_full_lifecycle_to_closed(self, db_session):
        service = _service(db_session)
        case = service.create_case(inquiry_type="ETC", customer_message="문의").case
        _to_resolved(service, db_session, case.id)
        closed = service.change_status(case.id, "CLOSED", "RESOLVED", actor=None)
        assert closed.status == "CLOSED"
        assert closed.closed_at is not None
        assert closed.resolved_at is not None

    def test_stale_expected_status_conflicts(self, db_session):
        service = _service(db_session)
        case = service.create_case(inquiry_type="ETC", customer_message="문의").case
        with pytest.raises(CsCaseConflictError):
            service.change_status(case.id, "WAITING_CUSTOMER", "IN_PROGRESS", actor=None)

    def test_closed_case_cannot_change_status(self, db_session):
        service = _service(db_session)
        case = service.create_case(inquiry_type="ETC", customer_message="문의").case
        _to_closed(service, db_session, case.id)
        with pytest.raises(CsCaseValidationError):
            service.change_status(case.id, "OPEN", "CLOSED", actor=None)

    def test_invalid_transition_rejected(self, db_session):
        service = _service(db_session)
        case = service.create_case(inquiry_type="ETC", customer_message="문의").case
        _to_resolved(service, db_session, case.id)
        # RESOLVED -> WAITING_CUSTOMER는 허용되지 않는다.
        with pytest.raises(CsCaseValidationError):
            service.change_status(case.id, "WAITING_CUSTOMER", "RESOLVED", actor=None)


class TestReopen:
    def test_reopen_requires_closed_expected_status(self, db_session):
        service = _service(db_session)
        case = service.create_case(inquiry_type="ETC", customer_message="문의").case
        with pytest.raises(CsCaseValidationError):
            service.reopen_case(case.id, "OPEN", actor=None)

    def test_reopen_from_closed_succeeds_and_increments_counter(self, db_session):
        service = _service(db_session)
        case = service.create_case(inquiry_type="ETC", customer_message="문의").case
        _to_closed(service, db_session, case.id)

        reopened = service.reopen_case(case.id, "CLOSED", actor=None)

        assert reopened.status == "OPEN"
        assert reopened.closed_at is None
        assert reopened.resolved_at is None
        assert reopened.reopened_count == 1
        history = service.list_history(case.id)
        assert history[-1].action == "REOPENED"

    def test_reopen_stale_expected_status_conflicts(self, db_session):
        service = _service(db_session)
        case = service.create_case(inquiry_type="ETC", customer_message="문의").case
        _to_closed(service, db_session, case.id)
        service.reopen_case(case.id, "CLOSED", actor=None)
        with pytest.raises(CsCaseConflictError):
            service.reopen_case(case.id, "CLOSED", actor=None)


class TestMemoAndReplyDraft:
    def test_add_memo(self, db_session):
        service = _service(db_session)
        agent = _make_user(db_session)
        case = service.create_case(inquiry_type="ETC", customer_message="문의").case
        memo = service.add_memo(case.id, "내부 메모 - 재고 확인 필요", actor=agent.id)
        assert memo.target_type == "CS_CASE"
        assert memo.target_id == case.id
        memos = service.list_memos(case.id)
        assert len(memos) == 1

    def test_empty_memo_rejected(self, db_session):
        service = _service(db_session)
        case = service.create_case(inquiry_type="ETC", customer_message="문의").case
        with pytest.raises(CsCaseValidationError):
            service.add_memo(case.id, "   ", actor=None)

    def test_memo_blocked_on_closed_case(self, db_session):
        service = _service(db_session)
        case = service.create_case(inquiry_type="ETC", customer_message="문의").case
        _to_closed(service, db_session, case.id)
        with pytest.raises(CsCaseValidationError):
            service.add_memo(case.id, "메모", actor=None)

    def test_reply_draft_update(self, db_session):
        service = _service(db_session)
        agent = _make_user(db_session)
        case = service.create_case(inquiry_type="ETC", customer_message="문의").case
        updated = service.update_reply_draft(case.id, "빠른 배송 도와드리겠습니다.", "OPEN", actor=agent.id)
        assert updated.reply_draft == "빠른 배송 도와드리겠습니다."
        assert updated.last_agent_response_at is not None

    def test_memo_and_reply_draft_never_mix(self, db_session):
        """내부 메모와 고객 답변 초안은 완전히 다른 저장소(Memo 테이블 vs
        CsCase.reply_draft 컬럼)에 있다는 것을 회귀 검증한다."""
        service = _service(db_session)
        case = service.create_case(inquiry_type="ETC", customer_message="문의").case
        service.add_memo(case.id, "내부용: 절대 고객에게 보내면 안 됨", actor=None)
        service.update_reply_draft(case.id, "고객님께 안내드립니다.", "OPEN", actor=None)

        refreshed = service.get_case(case.id)
        assert refreshed is not None
        memos = service.list_memos(case.id)
        assert refreshed.reply_draft == "고객님께 안내드립니다."
        assert memos[0].content == "내부용: 절대 고객에게 보내면 안 됨"
        assert "내부용" not in (refreshed.reply_draft or "")

    def test_reply_draft_stale_expected_status_conflicts(self, db_session):
        service = _service(db_session)
        case = service.create_case(inquiry_type="ETC", customer_message="문의").case
        with pytest.raises(CsCaseConflictError):
            service.update_reply_draft(case.id, "답변", "IN_PROGRESS", actor=None)

    def test_reply_draft_blocked_on_closed_case(self, db_session):
        service = _service(db_session)
        case = service.create_case(inquiry_type="ETC", customer_message="문의").case
        _to_closed(service, db_session, case.id)
        with pytest.raises(CsCaseValidationError):
            service.update_reply_draft(case.id, "답변", "CLOSED", actor=None)


class TestDueDate:
    def test_overdue_case_counted(self, db_session):
        service = _service(db_session)
        past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        service.create_case(inquiry_type="ETC", customer_message="문의", due_at=past)
        summary = service.dashboard_summary()
        assert summary["overdue_count"] == 1

    def test_future_due_not_overdue(self, db_session):
        service = _service(db_session)
        future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
        service.create_case(inquiry_type="ETC", customer_message="문의", due_at=future)
        summary = service.dashboard_summary()
        assert summary["overdue_count"] == 0

    def test_resolved_case_not_counted_as_overdue(self, db_session):
        service = _service(db_session)
        past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        case = service.create_case(inquiry_type="ETC", customer_message="문의", due_at=past).case
        service.change_status(case.id, "RESOLVED", "OPEN", actor=None)
        summary = service.dashboard_summary()
        assert summary["overdue_count"] == 0


class TestBulkOperations:
    def test_bulk_assign_partial_failure(self, db_session):
        service = _service(db_session)
        open_case = service.create_case(inquiry_type="ETC", customer_message="문의1").case
        closed_case = service.create_case(inquiry_type="ETC", customer_message="문의2").case
        _to_closed(service, db_session, closed_case.id)
        new_agent = _make_user(db_session)

        results = service.bulk_assign([open_case.id, closed_case.id, 999999], new_agent.id, actor=None)

        by_id = {r.case_id: r for r in results}
        assert by_id[open_case.id].outcome == "ACCEPTED"
        assert by_id[closed_case.id].outcome == "BLOCKED"
        assert by_id[999999].outcome == "NOT_FOUND"

    def test_bulk_change_status_partial_failure(self, db_session):
        service = _service(db_session)
        agent = _make_user(db_session)
        assignable = service.create_case(inquiry_type="ETC", customer_message="문의1").case
        service.assign(assignable.id, agent.id, None, actor=None)
        unassigned = service.create_case(inquiry_type="ETC", customer_message="문의2").case

        results = service.bulk_change_status([assignable.id, unassigned.id], "IN_PROGRESS", actor=None)

        by_id = {r.case_id: r for r in results}
        assert by_id[assignable.id].outcome == "ACCEPTED"
        assert by_id[unassigned.id].outcome == "VALIDATION_FAILED"
        assert by_id[unassigned.id].error_code == "ASSIGNEE_REQUIRED"
