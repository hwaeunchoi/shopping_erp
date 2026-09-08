"""
repositories/fulfillment_repository.py
------------------------------------------
상용 ERP 확장(5단계, A묶음) - 출고 배치/배치항목/작업이력 Repository.

claim_transition()은 repositories.integration_sync_repository.
ExternalCommandRepository.claim()과 동일 원칙(원자적 조건부 UPDATE)이다 -
select-then-update는 두 worker가 같은 항목을 동시에 다음 단계로 넘길 때
경합(TOCTOU)에 안전하지 않다.
"""

from datetime import datetime
from typing import Any, Optional, cast

from sqlalchemy import Select, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from models.fulfillment import FulfillmentBatch, FulfillmentBatchItem, FulfillmentBatchItemHistory
from models.order import ShipmentItem
from repositories.base_repository import BaseRepository


class FulfillmentBatchRepository(BaseRepository[FulfillmentBatch]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, FulfillmentBatch)

    def _filtered_stmt(self, status: Optional[str] = None, warehouse_id: Optional[int] = None) -> Select[Any]:
        stmt = select(FulfillmentBatch)
        if status is not None:
            stmt = stmt.where(FulfillmentBatch.status == status)
        if warehouse_id is not None:
            stmt = stmt.where(FulfillmentBatch.warehouse_id == warehouse_id)
        return stmt

    def list_filtered(
        self, status: Optional[str] = None, warehouse_id: Optional[int] = None, limit: int = 50, offset: int = 0
    ) -> list[FulfillmentBatch]:
        stmt = (
            self._filtered_stmt(status, warehouse_id).order_by(FulfillmentBatch.id.desc()).limit(limit).offset(offset)
        )
        return list(self.session.execute(stmt).scalars().all())

    def count_filtered(self, status: Optional[str] = None, warehouse_id: Optional[int] = None) -> int:
        stmt = select(func.count()).select_from(self._filtered_stmt(status, warehouse_id).subquery())
        return self.session.execute(stmt).scalar_one()


class FulfillmentBatchItemRepository(BaseRepository[FulfillmentBatchItem]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, FulfillmentBatchItem)

    def list_by_batch(self, batch_id: int) -> list[FulfillmentBatchItem]:
        stmt = (
            select(FulfillmentBatchItem)
            .where(FulfillmentBatchItem.batch_id == batch_id)
            .order_by(FulfillmentBatchItem.id)
        )
        return list(self.session.execute(stmt).scalars().all())

    def list_by_shipment(self, shipment_id: int) -> list[FulfillmentBatchItem]:
        stmt = select(FulfillmentBatchItem).where(FulfillmentBatchItem.shipment_id == shipment_id)
        return list(self.session.execute(stmt).scalars().all())

    def list_by_ids(self, ids: list[int]) -> list[FulfillmentBatchItem]:
        if not ids:
            return []
        stmt = select(FulfillmentBatchItem).where(FulfillmentBatchItem.id.in_(ids))
        return list(self.session.execute(stmt).scalars().all())

    def sum_active_allocated_quantity(self, order_item_id: int) -> int:
        """이 주문라인에 대해 아직 포장(재고 차감)되지 않은 채로 살아있는(취소되지
        않은) 배치 항목의 요청수량 합계 - 포장 이후(PACKED 이상)는 이미
        shipment_items에 반영되므로 여기서 이중으로 세지 않는다(services.
        fulfillment_service.FulfillmentService._remaining_quantity 참고)."""
        stmt = select(func.coalesce(func.sum(FulfillmentBatchItem.requested_quantity), 0)).where(
            FulfillmentBatchItem.order_item_id == order_item_id,
            FulfillmentBatchItem.status.notin_(("CANCELLED", "PACKED", "SUBMIT_PENDING", "SUBMITTED")),
        )
        return int(self.session.execute(stmt).scalar_one())

    def sum_allocated_quantity_by_order_items(self, order_item_ids: list[int]) -> dict[int, int]:
        """여러 주문라인의 미포장 배정 수량을 한 번에 조회한다(목록 화면 N+1 방지)."""
        if not order_item_ids:
            return {}
        stmt = (
            select(
                FulfillmentBatchItem.order_item_id, func.coalesce(func.sum(FulfillmentBatchItem.requested_quantity), 0)
            )
            .where(
                FulfillmentBatchItem.order_item_id.in_(order_item_ids),
                FulfillmentBatchItem.status.notin_(("CANCELLED", "PACKED", "SUBMIT_PENDING", "SUBMITTED")),
            )
            .group_by(FulfillmentBatchItem.order_item_id)
        )
        return {row[0]: int(row[1]) for row in self.session.execute(stmt).all()}

    def claim_transition(self, batch_item_id: int, expected_status: str, new_status: str, **extra_values: Any) -> bool:
        """expected_status일 때만 new_status로 원자적으로 전이한다(+ 부가 컬럼 갱신).

        UPDATE ... WHERE id=? AND status=? 자체가 DB 레벨에서 원자적이므로, 같은
        항목을 두 사용자가 동시에 다음 단계로 넘기려 해도 정확히 하나만
        성공한다(rowcount==1) - 화면이 stale한 상태를 근거로 보낸 요청도 이
        조건에서 자연히 막힌다(낙관적 동시성)."""
        stmt = (
            update(FulfillmentBatchItem)
            .where(FulfillmentBatchItem.id == batch_item_id, FulfillmentBatchItem.status == expected_status)
            .values(status=new_status, **extra_values)
        )
        result = cast(CursorResult, self.session.execute(stmt))
        return result.rowcount == 1

    def mark_inventory_deducted(self, batch_item_id: int, when: datetime) -> None:
        stmt = (
            update(FulfillmentBatchItem)
            .where(FulfillmentBatchItem.id == batch_item_id)
            .values(inventory_deducted_at=when)
        )
        self.session.execute(stmt)

    def sum_committed_quantity_by_order_items(self, order_item_ids: list[int]) -> dict[int, int]:
        """이미 shipment_items에 배정된(=포장 완료되어 실물이 확정된) 수량 합계 -
        order_item_id가 채워진 라인만 센다(NULL=구(舊) 단건 플로우의 "주문 전체"
        연결은 _has_whole_order_shipment_item으로 별도 판정한다)."""
        if not order_item_ids:
            return {}
        stmt = (
            select(ShipmentItem.order_item_id, func.coalesce(func.sum(ShipmentItem.quantity), 0))
            .where(ShipmentItem.order_item_id.in_(order_item_ids), ShipmentItem.quantity.is_not(None))
            .group_by(ShipmentItem.order_item_id)
        )
        return {row[0]: int(row[1]) for row in self.session.execute(stmt).all()}

    def orders_with_whole_order_shipment(self, order_ids: list[int]) -> set[int]:
        """order_item_id가 NULL인(=주문 전체를 가리키는, 구 단건/합포장 플로우)
        shipment_item이 있는 주문 id 집합 - 이런 주문은 이미 통째로 배송에
        연결되어 있으므로 이 배치 플로우로 라인 단위 부분출고를 새로 시작하면
        같은 실물을 두 번 잡을 위험이 있어 잔여수량을 0으로 취급한다."""
        if not order_ids:
            return set()
        stmt = (
            select(ShipmentItem.order_id)
            .where(ShipmentItem.order_id.in_(order_ids), ShipmentItem.order_item_id.is_(None))
            .distinct()
        )
        return set(self.session.execute(stmt).scalars().all())


class FulfillmentBatchItemHistoryRepository(BaseRepository[FulfillmentBatchItemHistory]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, FulfillmentBatchItemHistory)

    def list_by_batch_item(self, batch_item_id: int) -> list[FulfillmentBatchItemHistory]:
        stmt = (
            select(FulfillmentBatchItemHistory)
            .where(FulfillmentBatchItemHistory.batch_item_id == batch_item_id)
            .order_by(FulfillmentBatchItemHistory.changed_at)
        )
        return list(self.session.execute(stmt).scalars().all())

    def list_by_batch(self, batch_id: int) -> list[FulfillmentBatchItemHistory]:
        stmt = (
            select(FulfillmentBatchItemHistory)
            .join(FulfillmentBatchItem, FulfillmentBatchItem.id == FulfillmentBatchItemHistory.batch_item_id)
            .where(FulfillmentBatchItem.batch_id == batch_id)
            .order_by(FulfillmentBatchItemHistory.changed_at)
        )
        return list(self.session.execute(stmt).scalars().all())
