"""
repositories/integration_sync_repository.py
--------------------------------------------------
ExternalCommand(outbox)/ExternalCommandLineResult(라인별 결과)/OrderStatusConflict 저장소.
"""

from datetime import datetime, timezone
from typing import Any, Optional, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from models.integration_sync import ExternalCommand, ExternalCommandLineResult, OrderStatusConflict


class ExternalCommandRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[ExternalCommand]:
        return self.session.execute(
            select(ExternalCommand).where(ExternalCommand.idempotency_key == idempotency_key)
        ).scalar_one_or_none()

    def add(self, command: ExternalCommand) -> ExternalCommand:
        self.session.add(command)
        self.session.flush()
        return command

    def get_by_id(self, command_id: int) -> Optional[ExternalCommand]:
        return self.session.get(ExternalCommand, command_id)

    def list_retryable_due(self, now: Optional[datetime] = None) -> list[ExternalCommand]:
        """자동 백오프 재처리 대상(6단계에서 사용) - RETRY_WAIT이고 next_retry_at이 지난 것."""
        now = now or datetime.now(timezone.utc)
        return list(
            self.session.execute(
                select(ExternalCommand).where(
                    ExternalCommand.status == "RETRY_WAIT",
                    ExternalCommand.next_retry_at.is_not(None),
                    ExternalCommand.next_retry_at <= now,
                )
            ).scalars()
        )

    def list_due_for_execution(self, command_type: str, now: Optional[datetime] = None) -> list[ExternalCommand]:
        """outbox worker(scheduler.jobs.outbox_dispatch_job)가 실행할 대상 - PENDING(항상 즉시
        대상) + RETRY_WAIT인데 next_retry_at이 지난 것. command_type으로 범위를 좁힌다
        (예: "SHIPMENT_SUBMIT")."""
        now = now or datetime.now(timezone.utc)
        return list(
            self.session.execute(
                select(ExternalCommand)
                .where(
                    ExternalCommand.command_type == command_type,
                    (ExternalCommand.status == "PENDING")
                    | (
                        (ExternalCommand.status == "RETRY_WAIT")
                        & ExternalCommand.next_retry_at.is_not(None)
                        & (ExternalCommand.next_retry_at <= now)
                    ),
                )
                .order_by(ExternalCommand.id)
            ).scalars()
        )

    def list_stale_running(self, command_type: str, older_than: datetime) -> list[ExternalCommand]:
        """RUNNING 상태로 너무 오래 머물러 있는 명령(worker 프로세스가 실행 중 죽은 경우
        추정) - updated_at이 older_than보다 이전인 RUNNING 명령. worker가 시작할 때마다
        먼저 이 목록을 회수한다(services.shipment_dispatch_service.recover_stale_running -
        PENDING이 아니라 UNKNOWN으로 보낸다. 채널에 실제로 도달했는지 알 수 없는 채로
        자동 재전송하면 안 되기 때문)."""
        return list(
            self.session.execute(
                select(ExternalCommand).where(
                    ExternalCommand.command_type == command_type,
                    ExternalCommand.status == "RUNNING",
                    ExternalCommand.updated_at < older_than,
                )
            ).scalars()
        )

    def claim(self, command_id: int, lease_token: str) -> bool:
        """PENDING/RETRY_WAIT 명령을 RUNNING으로 원자적으로 선점(claim)한다.

        UPDATE ... WHERE status IN (...) 자체가 DB 레벨에서 원자적이므로, 두 worker가
        동시에 같은 명령을 claim 시도해도 정확히 하나만 성공한다(rowcount==1). 나머지
        worker는 rowcount==0을 받아 자신이 선점하지 못했음을 알 수 있다 - ORM 객체를
        먼저 읽고 나중에 쓰는 방식(select-then-update)은 두 조회 사이에 다른 worker가
        끼어들 수 있어(TOCTOU) 이 목적에 안전하지 않다."""
        stmt = (
            update(ExternalCommand)
            .where(ExternalCommand.id == command_id, ExternalCommand.status.in_(("PENDING", "RETRY_WAIT")))
            .values(status="RUNNING", lease_token=lease_token, attempt_count=ExternalCommand.attempt_count + 1)
        )
        result = cast(CursorResult, self.session.execute(stmt))
        return result.rowcount == 1

    def try_transition(self, command_id: int, expected_lease_token: str, **values: Any) -> bool:
        """lease_token이 아직 자신의 것일 때만 원자적으로 최종 상태를 반영한다(소유권 검증).

        recover_stale_running()이 그 사이 이 명령을 회수(lease_token을 바꾸거나 비움)했다면
        여기서 rowcount==0이 되어 갱신되지 않는다 - 소유권을 잃은 worker가 뒤늦게 끝나도
        다른 worker/회수 절차의 결과를 덮어쓰지 못하게 하기 위함이다."""
        stmt = (
            update(ExternalCommand)
            .where(ExternalCommand.id == command_id, ExternalCommand.lease_token == expected_lease_token)
            .values(**values)
        )
        result = cast(CursorResult, self.session.execute(stmt))
        return result.rowcount == 1


class ExternalCommandLineResultRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record_result(
        self, command_id: int, order_item_id: int, status: str, quantity: int, result_code: Optional[str]
    ) -> ExternalCommandLineResult:
        """(command_id, order_item_id) 라인 결과를 기록한다 - 이미 이 조합의 행이 있으면
        (예: FAILED로 기록된 라인을 같은 명령으로 재시도) 새로 만들지 않고 갱신한다
        (uq_command_line_result 유니크 제약 위반 방지)."""
        stmt = select(ExternalCommandLineResult).where(
            ExternalCommandLineResult.command_id == command_id, ExternalCommandLineResult.order_item_id == order_item_id
        )
        existing = self.session.execute(stmt).scalar_one_or_none()
        if existing is not None:
            existing.status = status
            existing.quantity = quantity
            existing.result_code = result_code
            self.session.flush()
            return existing
        line_result = ExternalCommandLineResult(
            command_id=command_id,
            order_item_id=order_item_id,
            status=status,
            quantity=quantity,
            result_code=result_code,
        )
        self.session.add(line_result)
        self.session.flush()
        return line_result

    def list_success_order_item_ids(self, command_id: int) -> set[int]:
        """이 명령에서 이미 성공 확인된 라인(order_item_id) 집합 - 재시도 시 이 집합에
        속한 라인은 다시 전송하지 않는다(부분성공 보호)."""
        stmt = select(ExternalCommandLineResult.order_item_id).where(
            ExternalCommandLineResult.command_id == command_id, ExternalCommandLineResult.status == "SUCCESS"
        )
        return set(self.session.execute(stmt).scalars())

    def sum_success_quantity(self, order_item_id: int) -> int:
        """이 주문상품(order_item)에 대해 지금까지(여러 Shipment/여러 명령에 걸쳐) 성공
        확인된 발송 수량 합계 - 주문 전체 이행 여부를 "라인 존재"가 아니라 "수량 합계"로
        판단하기 위함이다(같은 OrderItem이 여러 Shipment로 나뉘어 부분 발송되는 경우)."""
        stmt = select(func.coalesce(func.sum(ExternalCommandLineResult.quantity), 0)).where(
            ExternalCommandLineResult.order_item_id == order_item_id, ExternalCommandLineResult.status == "SUCCESS"
        )
        return int(self.session.execute(stmt).scalar_one())


class OrderStatusConflictRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, conflict: OrderStatusConflict) -> OrderStatusConflict:
        self.session.add(conflict)
        self.session.flush()
        return conflict

    def get_by_id(self, conflict_id: int) -> Optional[OrderStatusConflict]:
        return self.session.get(OrderStatusConflict, conflict_id)

    def list_unresolved(self, order_id: Optional[int] = None) -> list[OrderStatusConflict]:
        stmt = select(OrderStatusConflict).where(OrderStatusConflict.resolved_at.is_(None))
        if order_id is not None:
            stmt = stmt.where(OrderStatusConflict.order_id == order_id)
        return list(self.session.execute(stmt.order_by(OrderStatusConflict.detected_at.desc())).scalars())

    def get_unresolved_for_status(self, order_id: int, channel_status: str) -> Optional[OrderStatusConflict]:
        """같은 주문에 같은 채널상태로 이미 미해소 충돌이 있는지 확인한다(중복 생성 방지).

        채널 상태 재조회(스케줄 작업/발송 성공 후)가 반복 실행돼도, 운영자가 아직
        해소하지 않은 동일 충돌을 매번 새 행으로 쌓지 않는다."""
        return self.session.execute(
            select(OrderStatusConflict).where(
                OrderStatusConflict.order_id == order_id,
                OrderStatusConflict.channel_status == channel_status,
                OrderStatusConflict.resolved_at.is_(None),
            )
        ).scalar_one_or_none()
