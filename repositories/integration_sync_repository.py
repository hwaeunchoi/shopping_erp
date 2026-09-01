"""
repositories/integration_sync_repository.py
--------------------------------------------------
ExternalCommand(outbox)/OrderStatusConflict 저장소.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.integration_sync import ExternalCommand, OrderStatusConflict


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
        먼저 이 목록을 PENDING으로 되돌려 회수한다(모듈 docstring의 재시도 안전성 가정 참고)."""
        return list(
            self.session.execute(
                select(ExternalCommand).where(
                    ExternalCommand.command_type == command_type,
                    ExternalCommand.status == "RUNNING",
                    ExternalCommand.updated_at < older_than,
                )
            ).scalars()
        )


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
