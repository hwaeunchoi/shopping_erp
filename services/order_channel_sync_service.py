"""
services/order_channel_sync_service.py
--------------------------------------------
채널에서 수집한 최신 주문상태를 내부 Order.status와 비교한다.

허용된 전이(services.order_state_machine)면 그대로 반영하고, 아니면
(예: 내부는 DELIVERED인데 채널이 NEW라고 하는 경우) 자동으로 어느 한쪽을
정답으로 덮어쓰지 않고 OrderStatusConflict로 남겨 운영자가 확인하게 한다.

기존 scheduler.order_collect_job(운영 중인 주문 수집 흐름)은 이 서비스를 사용하지
않는다(변경하지 않기 위한 의도적 범위 제한). 대신 이 서비스는 다음 두 경로에서
호출된다(둘 다 additive, order_collect_job과 독립):
1. scheduler.jobs.channel_status_sync_job - 최근 주문의 채널 상태를 읽기 전용으로
   재조회해 반영/충돌기록한다.
2. services.shipment_dispatch_service.ShipmentDispatchService.execute_command -
   송장 전송이 채널에 성공적으로 접수된 직후, 그 사실 자체를 "SHIPPING" 상태로 반영한다.
docs/COMMERCIAL_ERP_ROADMAP.md 참고.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from models.extra import AuditLog
from models.integration_sync import OrderStatusConflict
from models.order import Order
from repositories.integration_sync_repository import OrderStatusConflictRepository
from services.order_state_machine import is_transition_allowed
from services.order_sync_service import OrderSyncService


@dataclass
class ChannelSyncResult:
    applied: bool
    conflict: bool
    from_status: str
    to_status: str


class OrderChannelSyncService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.order_sync_service = OrderSyncService(session)
        self.conflict_repo = OrderStatusConflictRepository(session)

    def sync_channel_status(
        self, order: Order, channel_status: str, changed_by: Optional[int] = None
    ) -> ChannelSyncResult:
        from_status = order.status
        if from_status == channel_status:
            return ChannelSyncResult(applied=False, conflict=False, from_status=from_status, to_status=channel_status)

        if is_transition_allowed(from_status, channel_status):
            self.order_sync_service.apply_status_change(order, channel_status, warehouse_id=None)
            self.session.add(
                AuditLog(
                    entity_type="ORDER",
                    entity_id=order.id,
                    action="UPDATE",
                    before_json=f'{{"status":"{from_status}"}}',
                    after_json=f'{{"status":"{channel_status}"}}',
                    changed_by=changed_by,
                    changed_at=datetime.now(timezone.utc),
                    command="order.channel_status_sync",
                )
            )
            self.session.flush()
            return ChannelSyncResult(applied=True, conflict=False, from_status=from_status, to_status=channel_status)

        # 같은 채널상태로 이미 미해소 충돌이 있으면(반복 재조회로 인한 재감지) 중복 행을
        # 만들지 않는다 - 운영자가 아직 해소하지 않은 동일 충돌은 기존 기록 하나로 충분하다.
        if self.conflict_repo.get_unresolved_for_status(order.id, channel_status) is None:
            self.conflict_repo.add(
                OrderStatusConflict(
                    order_id=order.id,
                    internal_status=from_status,
                    channel_status=channel_status,
                    detected_at=datetime.now(timezone.utc),
                )
            )
        return ChannelSyncResult(applied=False, conflict=True, from_status=from_status, to_status=channel_status)

    def resolve_conflict(
        self, conflict_id: int, resolution: str, resolved_by: Optional[int] = None
    ) -> OrderStatusConflict:
        """resolution: ACCEPT_CHANNEL(채널 값을 내부에 강제 반영) / KEEP_INTERNAL(내부 유지, 채널 값 폐기)."""
        if resolution not in ("ACCEPT_CHANNEL", "KEEP_INTERNAL"):
            raise ValueError(f"알 수 없는 해소 방식입니다: {resolution}")
        conflict = self.conflict_repo.get_by_id(conflict_id)
        if conflict is None:
            raise ValueError(f"충돌 기록을 찾을 수 없습니다: conflict_id={conflict_id}")
        if conflict.resolved_at is not None:
            raise ValueError("이미 해소된 충돌입니다.")

        if resolution == "ACCEPT_CHANNEL":
            order = self.session.get(Order, conflict.order_id)
            if order is not None:
                from_status = order.status
                order.status = conflict.channel_status
                order.updated_at = datetime.now(timezone.utc)
                self.session.add(
                    AuditLog(
                        entity_type="ORDER",
                        entity_id=order.id,
                        action="UPDATE",
                        before_json=f'{{"status":"{from_status}"}}',
                        after_json=f'{{"status":"{conflict.channel_status}"}}',
                        changed_by=resolved_by,
                        changed_at=datetime.now(timezone.utc),
                        command="order.channel_status_conflict_resolve",
                        reason="운영자가 채널 상태를 채택함(강제 전이, 상태머신 우회)",
                    )
                )

        conflict.resolved_at = datetime.now(timezone.utc)
        conflict.resolved_by = resolved_by
        conflict.resolution = resolution
        self.session.flush()
        return conflict
