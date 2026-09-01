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
