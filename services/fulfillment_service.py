"""
services/fulfillment_service.py
------------------------------------
상용 ERP 확장(5단계, A묶음) - 출고 배치(피킹/검수/포장) 업무로직.

출고대기 -> 피킹 -> 검수 -> 포장완료(재고 차감 + Shipment/ShipmentItem 생성) ->
채널전송 접수까지를 다룬다. 그 다음(실제 채널 반영 확인)은 여전히
services.shipment_dispatch_service.ShipmentDispatchService(outbox worker)가
전담하고, Order.status 반영도 그 서비스의 _sync_channel_status_after_success가
전담한다 - 이 서비스는 그 둘을 호출만 할 뿐 새로 구현하지 않는다.

동시성:
- 같은 주문라인에 대한 배치 생성 경합(초과 배정 방지)은
  ExternalCommandRepository.acquire_target_lock()을 그대로 재사용해 잠근다
  (target_type="FULFILLMENT_ORDER_ITEM") - PostgreSQL에서만 실제로 잠기고
  SQLite에서는 no-op이다(단위테스트는 단일 커넥션이라 안전, 실제 동시 보장은
  격리 PostgreSQL 통합 테스트로 검증한다).
- 같은 배치항목을 두 사용자가 동시에 다음 단계로 넘기는 경합은
  FulfillmentBatchItemRepository.claim_transition()의 원자적 조건부 UPDATE로
  막는다(정확히 하나만 성공).
- 같은 (창고, 옵션) 재고 행에 대한 동시 차감 경합은 같은 advisory lock 원리를
  재사용하되 별도 target_type("FULFILLMENT_INVENTORY")으로 잠근다(재고 행이
  아직 없을 수도 있어 warehouse_id/product_option_id 조합에서 파생한 합성 키를
  쓴다 - 재고 행 자체의 id를 키로 쓰면 행이 없을 때 잠글 대상이 없다).

재고 차감 시점: "검수완료"가 아니라 "포장완료"(pack_and_register_tracking)
시점에 정확히 한 번 차감한다(FulfillmentBatchItem.inventory_deducted_at으로
멱등 보장 - claim_transition이 VERIFIED 상태에서만 통과하므로 이미 PACKED인
항목을 다시 포장 처리해도 재고가 다시 차감되지 않는다). 채널 전송 실패는
재고 차감을 되돌리지 않는다(물리적으로는 이미 창고를 떠났을 수 있는 상태이므로
채널 응답과 무관하다) - 되돌리려면 명시적으로 cancel_item()을 호출해야 한다.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from integrations.malls.errors import MarketplaceCapabilityUnsupportedError
from models.fulfillment import FulfillmentBatch, FulfillmentBatchItem, FulfillmentBatchItemHistory
from models.inventory import InventoryStatus
from models.order import Order, OrderItem
from models.product import ProductOption
from repositories.fulfillment_repository import (
    FulfillmentBatchItemHistoryRepository,
    FulfillmentBatchItemRepository,
    FulfillmentBatchRepository,
)
from repositories.integration_sync_repository import ExternalCommandRepository
from repositories.order_repository import OrderRepository, ShipmentRepository
from services.fulfillment_carrier import (
    InvalidCarrierError,
    InvalidTrackingNoError,
    normalize_tracking_no,
    validate_carrier,
)
from services.fulfillment_state_machine import (
    INVENTORY_DEDUCTED_STATUSES,
    InvalidFulfillmentTransitionError,
    validate_transition,
)
from services.inventory_service import InsufficientStockError, InventoryService
from services.order_sync_service import UNSHIPPED_STATUSES
from services.shipment_dispatch_service import (
    ShipmentChannelSubmitDisabledError,
    ShipmentDispatchService,
    ShipmentNotReadyError,
    ShipmentPlatformMismatchError,
)
from services.shipment_service import ShipmentService

_LOCK_ORDER_ITEM = "FULFILLMENT_ORDER_ITEM"
_LOCK_INVENTORY = "FULFILLMENT_INVENTORY"
# 재고 행이 아직 없을 수도 있어(창고에 처음 들어온 조합) 행 id 대신 (창고,옵션)에서
# 파생한 합성 키를 잠금 대상으로 쓴다. product_option_id가 이 상수보다 커지면 키가
# 겹칠 수 있으나(자릿수 오버플로), 이 앱 규모에서는 현실적으로 발생하지 않는다.
_INVENTORY_LOCK_MULTIPLIER = 10_000_000


class FulfillmentValidationError(ValueError):
    """출고 배치 생성/전이 요청이 업무 규칙을 위반할 때 던진다(안전한 메시지만 담는다)."""


class FulfillmentConflictError(Exception):
    """expected_status가 현재 상태와 달라 원자적 전이가 실패했을 때(동시 변경/화면
    stale) 던진다 - API는 이를 409로 변환한다."""


@dataclass
class FulfillableOrderItem:
    order_id: int
    order_item_id: int
    platform_order_no: str
    product_option_id: int
    sku_code: str
    product_name: str
    order_quantity: int
    remaining_quantity: int


@dataclass
class BatchItemOutcome:
    batch_item_id: int
    outcome: str  # ACCEPTED/ALREADY_PROCESSED/BLOCKED/VALIDATION_FAILED/FAILED_TO_ENQUEUE
    error_code: Optional[str] = None


@dataclass
class BatchItemDisplay:
    item: FulfillmentBatchItem
    platform_order_no: str
    sku_code: str
    product_name: str


class FulfillmentService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.batch_repo = FulfillmentBatchRepository(session)
        self.item_repo = FulfillmentBatchItemRepository(session)
        self.history_repo = FulfillmentBatchItemHistoryRepository(session)
        self.order_repo = OrderRepository(session)
        self.shipment_repo = ShipmentRepository(session)
        self.command_repo = ExternalCommandRepository(session)
        self.inventory_service = InventoryService(session)
        self.shipment_service = ShipmentService(session)
        self.dispatch_service = ShipmentDispatchService(session)

    # --- 출고대기 목록 -----------------------------------------------------

    def list_fulfillable_order_items(
        self, *, platform_id: Optional[int] = None, search: Optional[str] = None, limit: int = 50, offset: int = 0
    ) -> list[FulfillableOrderItem]:
        """출고 배치에 새로 담을 수 있는 주문라인 - 아직 완전히 이행되지 않고
        (remaining > 0), 취소/반품/교환/환불/배송완료가 아닌 주문만 대상이다.

        UNSHIPPED_STATUSES(NEW/PREPARING)가 두 값이라 OrderRepository.
        list_filtered()의 status(단일값) 필터를 값마다 한 번씩 호출해 합친다 -
        remaining 계산 자체는 Python에서 하므로(committed/allocated 집계가
        DB 쪽 단일 쿼리로 표현하기 번거로운 다중 조인 집계라) 후보를 넉넉히
        가져온 뒤 걸러내고 그 결과에 대해서만 limit/offset을 적용한다."""
        candidate_orders: list[Order] = []
        for status_value in UNSHIPPED_STATUSES:
            candidate_orders.extend(
                self.order_repo.list_filtered(status=status_value, platform_id=platform_id, keyword=search, limit=500)
            )
        if not candidate_orders:
            return []
        order_ids = [o.id for o in candidate_orders]
        items = self.order_repo.list_items_by_order_ids(order_ids)
        item_ids = [i.id for i in items]

        committed = self.item_repo.sum_committed_quantity_by_order_items(item_ids)
        allocated = self.item_repo.sum_allocated_quantity_by_order_items(item_ids)
        whole_order_locked = self.item_repo.orders_with_whole_order_shipment(order_ids)

        option_ids = [i.product_option_id for i in items]
        options = {o.id: o for o in self._load_options(option_ids)}

        orders_by_id = {o.id: o for o in candidate_orders}
        matched: list[FulfillableOrderItem] = []
        for item in items:
            if item.order_id in whole_order_locked:
                continue
            remaining = item.quantity - committed.get(item.id, 0) - allocated.get(item.id, 0)
            if remaining <= 0:
                continue
            option = options.get(item.product_option_id)
            order = orders_by_id[item.order_id]
            matched.append(
                FulfillableOrderItem(
                    order_id=item.order_id,
                    order_item_id=item.id,
                    platform_order_no=order.platform_order_no,
                    product_option_id=item.product_option_id,
                    sku_code=option.sku_code if option else "",
                    product_name=option.product.name if option and option.product else "",
                    order_quantity=item.quantity,
                    remaining_quantity=remaining,
                )
            )
        matched.sort(key=lambda r: r.order_item_id, reverse=True)
        return matched[offset : offset + limit]

    def _load_options(self, option_ids: list[int]) -> list[ProductOption]:
        if not option_ids:
            return []
        unique_ids = list(dict.fromkeys(option_ids))
        return [o for o in (self.session.get(ProductOption, oid) for oid in unique_ids) if o is not None]

    # --- 배치 생성 -----------------------------------------------------

    def create_batch(
        self, warehouse_id: int, selections: list[tuple[int, int]], created_by: Optional[int]
    ) -> FulfillmentBatch:
        """selections: [(order_item_id, quantity), ...]. 같은 order_item_id가 두 번
        오면 하나로 합산하지 않고 거부한다(요청 자체의 모호함을 그대로 통과시키지
        않는다)."""
        if not selections:
            raise FulfillmentValidationError("배치에 담을 항목이 없습니다.")
        seen_ids = [oi_id for oi_id, _ in selections]
        if len(seen_ids) != len(set(seen_ids)):
            raise FulfillmentValidationError("같은 주문라인이 두 번 이상 선택되었습니다.")

        batch = self.batch_repo.add(FulfillmentBatch(warehouse_id=warehouse_id, status="READY", created_by=created_by))

        # 잠금은 항상 같은 순서(오름차순)로 걸어 서로 다른 배치가 서로 다른 순서로
        # 두 라인을 잠그며 교착(deadlock)하는 상황을 피한다.
        for order_item_id, quantity in sorted(selections, key=lambda pair: pair[0]):
            if quantity <= 0:
                raise FulfillmentValidationError(f"수량은 1 이상이어야 합니다: order_item_id={order_item_id}")
            self.command_repo.acquire_target_lock(_LOCK_ORDER_ITEM, order_item_id)

            item = self._get_order_item(order_item_id)
            if item is None:
                raise FulfillmentValidationError(f"주문라인을 찾을 수 없습니다: order_item_id={order_item_id}")
            order = self.order_repo.get_by_id(item.order_id)
            if order is None or order.status not in UNSHIPPED_STATUSES:
                raise FulfillmentValidationError(
                    f"출고할 수 없는 주문 상태입니다: order_item_id={order_item_id}, "
                    f"status={order.status if order else 'UNKNOWN'}"
                )
            if self.item_repo.orders_with_whole_order_shipment([item.order_id]):
                raise FulfillmentValidationError(f"이미 배송이 연결된 주문입니다: order_item_id={order_item_id}")

            committed = self.item_repo.sum_committed_quantity_by_order_items([order_item_id]).get(order_item_id, 0)
            allocated = self.item_repo.sum_active_allocated_quantity(order_item_id)
            remaining = item.quantity - committed - allocated
            if quantity > remaining:
                raise FulfillmentValidationError(
                    f"잔여 수량({remaining})보다 많은 수량을 요청했습니다: order_item_id={order_item_id}, "
                    f"요청={quantity}"
                )

            batch_item = self.item_repo.add(
                FulfillmentBatchItem(
                    batch_id=batch.id,
                    order_id=item.order_id,
                    order_item_id=item.id,
                    product_option_id=item.product_option_id,
                    requested_quantity=quantity,
                    status="READY",
                )
            )
            self._record_history(batch_item.id, None, "READY", created_by, None)

        return batch

    def _get_order_item(self, order_item_id: int) -> Optional[OrderItem]:
        return self.session.get(OrderItem, order_item_id)

    def get_batch_items_with_display(self, batch_id: int) -> list[BatchItemDisplay]:
        """피킹/검수 화면에 상품명·SKU·주문번호를 함께 보여주기 위한 조회."""
        items = self.item_repo.list_by_batch(batch_id)
        orders = {o.id: o for o in (self.order_repo.get_by_id(i.order_id) for i in items) if o is not None}
        options = {o.id: o for o in self._load_options([i.product_option_id for i in items])}
        display = []
        for item in items:
            order = orders.get(item.order_id)
            option = options.get(item.product_option_id)
            display.append(
                BatchItemDisplay(
                    item=item,
                    platform_order_no=order.platform_order_no if order else "",
                    sku_code=option.sku_code if option else "",
                    product_name=option.product.name if option and option.product else "",
                )
            )
        return display

    # --- 피킹/검수 -----------------------------------------------------

    def start_picking(self, batch_item_id: int, expected_status: str, actor: Optional[int]) -> FulfillmentBatchItem:
        return self._claim_or_conflict(batch_item_id, expected_status, "PICKING", actor, extra={"picked_by": actor})

    def complete_picking(
        self, batch_item_id: int, expected_status: str, actor: Optional[int], picked_quantity: int
    ) -> FulfillmentBatchItem:
        item = self._require_item(batch_item_id)
        if picked_quantity <= 0 or picked_quantity > item.requested_quantity:
            raise FulfillmentValidationError(
                f"피킹 수량은 1 이상 요청수량({item.requested_quantity}) 이하여야 합니다: {picked_quantity}"
            )
        now = datetime.now(timezone.utc)
        return self._claim_or_conflict(
            batch_item_id,
            expected_status,
            "PICKED",
            actor,
            extra={"picked_quantity": picked_quantity, "picked_by": actor, "picked_at": now},
        )

    def start_verification(
        self, batch_item_id: int, expected_status: str, actor: Optional[int]
    ) -> FulfillmentBatchItem:
        return self._claim_or_conflict(batch_item_id, expected_status, "VERIFYING", actor)

    def complete_verification(
        self, batch_item_id: int, expected_status: str, actor: Optional[int], verified_quantity: int
    ) -> FulfillmentBatchItem:
        """검수 확정 - picked_quantity와 다르면(수량 불일치) VERIFIED로 넘어가지
        않고 BLOCKED로 남긴다(추측으로 맞다고 넘어가지 않는다)."""
        item = self._require_item(batch_item_id)
        if verified_quantity < 0 or (item.picked_quantity is not None and verified_quantity > item.picked_quantity):
            raise FulfillmentValidationError(
                f"검수 수량은 0 이상 피킹수량({item.picked_quantity}) 이하여야 합니다: {verified_quantity}"
            )
        now = datetime.now(timezone.utc)
        if item.picked_quantity is not None and verified_quantity != item.picked_quantity:
            return self._claim_or_conflict(
                batch_item_id,
                expected_status,
                "BLOCKED",
                actor,
                extra={
                    "verified_quantity": verified_quantity,
                    "verified_by": actor,
                    "verified_at": now,
                    "failure_reason_code": "QUANTITY_MISMATCH",
                },
            )
        return self._claim_or_conflict(
            batch_item_id,
            expected_status,
            "VERIFIED",
            actor,
            extra={"verified_quantity": verified_quantity, "verified_by": actor, "verified_at": now},
        )

    # --- 포장완료 + 송장등록 --------------------------------------------

    def pack_and_register_tracking(
        self, batch_item_ids: list[int], carrier: str, tracking_no: str, actor: Optional[int]
    ) -> list[BatchItemOutcome]:
        """검수완료(VERIFIED)된 항목들을 하나의 송장으로 묶어 포장완료 처리한다 -
        재고를 정확히 한 번 차감하고 Shipment/ShipmentItem을 생성한다. 채널
        전송은 여기서 하지 않는다(별도로 submit_to_channel을 호출해야 한다)."""
        try:
            validate_carrier(carrier)
            normalized_tracking = normalize_tracking_no(tracking_no)
        except (InvalidCarrierError, InvalidTrackingNoError) as e:
            return [BatchItemOutcome(bid, "VALIDATION_FAILED", str(e)) for bid in batch_item_ids]

        if self._is_duplicate_tracking(carrier, normalized_tracking):
            return [
                BatchItemOutcome(bid, "VALIDATION_FAILED", "동일 택배사에 이미 등록된 송장번호입니다.")
                for bid in batch_item_ids
            ]

        results: list[BatchItemOutcome] = []
        pending_items: list[FulfillmentBatchItem] = []

        for batch_item_id in batch_item_ids:
            item = self.item_repo.get_by_id(batch_item_id)
            if item is None:
                results.append(BatchItemOutcome(batch_item_id, "VALIDATION_FAILED", "항목을 찾을 수 없습니다."))
                continue
            if item.status != "VERIFIED":
                results.append(
                    BatchItemOutcome(
                        batch_item_id, "VALIDATION_FAILED", f"검수완료 상태가 아닙니다(현재: {item.status})."
                    )
                )
                continue
            quantity = item.verified_quantity or 0
            if quantity <= 0:
                results.append(BatchItemOutcome(batch_item_id, "VALIDATION_FAILED", "검수 수량이 0입니다."))
                continue
            pending_items.append(item)

        if not pending_items:
            return results

        # 재고 차감 - 항목마다 (창고,옵션) 잠금을 걸고 부족하면 그 항목만 BLOCKED로
        # 남긴다(다른 항목의 이미 성공한 차감은 되돌리지 않는다).
        now = datetime.now(timezone.utc)
        deducted_items: list[FulfillmentBatchItem] = []
        for item in pending_items:
            batch = self.batch_repo.get_by_id(item.batch_id)
            warehouse_id = batch.warehouse_id if batch else None
            if warehouse_id is None:
                results.append(BatchItemOutcome(item.id, "VALIDATION_FAILED", "배치의 창고 정보가 없습니다."))
                continue
            lock_key = warehouse_id * _INVENTORY_LOCK_MULTIPLIER + item.product_option_id
            self.command_repo.acquire_target_lock(_LOCK_INVENTORY, lock_key)
            # 잠금 획득 전에 읽은 status는 낡았을 수 있다 - 잠금을 쥔 동안 다른
            # 트랜잭션이 이 항목을 먼저 포장 완료(커밋)했다면, 여기서 다시 조회해야만
            # 그 사실이 보인다(READ COMMITTED라 refresh가 최신 커밋본을 읽는다).
            # 이 재확인이 없으면 이미 성공한 항목의 재고가 여기서 한 번 더 차감된다 -
            # claim_transition의 원자적 UPDATE는 상태 이중전이만 막을 뿐 그 앞의
            # deduct_on_shipment 호출 자체를 막지는 못한다.
            self.session.refresh(item)
            if item.status != "VERIFIED":
                results.append(BatchItemOutcome(item.id, "ALREADY_PROCESSED", None))
                continue
            try:
                self.inventory_service.deduct_on_shipment(
                    item.product_option_id, warehouse_id, item.verified_quantity or 0, reference_id=item.id
                )
            except InsufficientStockError as e:
                self._transition(item.id, "BLOCKED", actor, failure_reason_code="INSUFFICIENT_STOCK")
                results.append(BatchItemOutcome(item.id, "BLOCKED", str(e)[:200]))
                continue
            except ValueError as e:
                self._transition(item.id, "BLOCKED", actor, failure_reason_code="NO_INVENTORY_RECORD")
                results.append(BatchItemOutcome(item.id, "BLOCKED", str(e)[:200]))
                continue
            deducted_items.append(item)

        if not deducted_items:
            return results

        shipment = self.shipment_service.create_with_items(
            [(i.order_id, i.order_item_id, i.verified_quantity or 0) for i in deducted_items],
            carrier=carrier,
            tracking_no=normalized_tracking,
        )
        for item in deducted_items:
            transitioned = self.item_repo.claim_transition(
                item.id,
                "VERIFIED",
                "PACKED",
                shipment_id=shipment.id,
                packed_by=actor,
                packed_at=now,
                inventory_deducted_at=now,
            )
            if not transitioned:
                # 재고는 이미 차감됐는데(위에서 성공) 상태 전이가 실패했다 - 다른
                # 요청이 그 사이 이 항목을 건드렸다는 뜻이다(동시 변경). 재고
                # 이중차감은 아니므로(차감은 이미 끝났다) 데이터는 안전하지만,
                # 화면에는 충돌로 알려 운영자가 확인하게 한다.
                results.append(
                    BatchItemOutcome(item.id, "VALIDATION_FAILED", "다른 요청에 의해 상태가 변경되었습니다.")
                )
                continue
            self._record_history(item.id, "VERIFIED", "PACKED", actor, f"shipment_id={shipment.id}")
            results.append(BatchItemOutcome(item.id, "ACCEPTED"))
        return results

    def _is_duplicate_tracking(self, carrier: str, normalized_tracking: str) -> bool:
        for existing in self.shipment_repo.list_by_carrier(carrier):
            if existing.tracking_no is None:
                continue
            try:
                if normalize_tracking_no(existing.tracking_no) == normalized_tracking:
                    return True
            except InvalidTrackingNoError:
                continue
        return False

    # --- 채널 전송 접수 ---------------------------------------------------

    def submit_to_channel(self, shipment_ids: list[int], actor: Optional[int]) -> list[BatchItemOutcome]:
        """포장완료(PACKED)된 항목들을 기존 ShipmentDispatchService.enqueue()로
        접수한다 - 새 전송 로직을 만들지 않는다."""
        results: list[BatchItemOutcome] = []
        for shipment_id in set(shipment_ids):
            items = [i for i in self.item_repo.list_by_shipment(shipment_id) if i.status == "PACKED"]
            if not items:
                continue
            claimed: list[FulfillmentBatchItem] = []
            for item in items:
                if self.item_repo.claim_transition(item.id, "PACKED", "SUBMIT_PENDING"):
                    claimed.append(item)
            if not claimed:
                continue
            try:
                self.dispatch_service.enqueue(shipment_id)
            except (
                ShipmentChannelSubmitDisabledError,
                ShipmentNotReadyError,
                ShipmentPlatformMismatchError,
                MarketplaceCapabilityUnsupportedError,
                ValueError,
            ) as e:
                for item in claimed:
                    self.item_repo.claim_transition(item.id, "SUBMIT_PENDING", "PACKED")
                    results.append(BatchItemOutcome(item.id, "FAILED_TO_ENQUEUE", str(e)[:200]))
                continue
            for item in claimed:
                self.item_repo.claim_transition(item.id, "SUBMIT_PENDING", "SUBMITTED")
                self._record_history(item.id, "SUBMIT_PENDING", "SUBMITTED", actor, None)
                results.append(BatchItemOutcome(item.id, "ACCEPTED"))
        return results

    def get_batch_progress(self, batch_id: int) -> list[dict]:
        items = self.item_repo.list_by_batch(batch_id)
        shipment_ids = sorted({i.shipment_id for i in items if i.shipment_id is not None})
        commands = (
            self.command_repo.list_for_targets("SHIPMENT_SUBMIT", "SHIPMENT", shipment_ids) if shipment_ids else []
        )
        command_by_shipment = {c.target_id: c for c in commands}
        progress = []
        for item in items:
            command = command_by_shipment.get(item.shipment_id) if item.shipment_id else None
            progress.append(
                {
                    "batch_item": item,
                    "command_status": command.status if command else None,
                    "command_id": command.id if command else None,
                    "command_error_code": command.error_code if command else None,
                }
            )
        return progress

    def retry_failed_items(self, batch_item_ids: list[int]) -> list[BatchItemOutcome]:
        """FAILED로 확정된 채널 전송만 재처리한다. UNKNOWN은 절대 자동 재처리하지
        않는다(services.shipment_dispatch_service.resolve_unknown_command로
        운영자가 직접 해소해야 한다)."""
        results: list[BatchItemOutcome] = []
        for batch_item_id in batch_item_ids:
            item = self.item_repo.get_by_id(batch_item_id)
            if item is None or item.shipment_id is None:
                results.append(BatchItemOutcome(batch_item_id, "VALIDATION_FAILED", "재처리할 명령이 없습니다."))
                continue
            commands = self.command_repo.list_for_targets("SHIPMENT_SUBMIT", "SHIPMENT", item.shipment_id)
            command = commands[0] if commands else None
            if command is None:
                results.append(BatchItemOutcome(batch_item_id, "VALIDATION_FAILED", "재처리할 명령이 없습니다."))
                continue
            if command.status == "UNKNOWN":
                results.append(BatchItemOutcome(batch_item_id, "BLOCKED", "UNKNOWN_REQUIRES_RESOLUTION"))
                continue
            if command.status != "FAILED":
                results.append(
                    BatchItemOutcome(
                        batch_item_id,
                        "VALIDATION_FAILED",
                        f"FAILED 상태만 재처리할 수 있습니다(현재: {command.status}).",
                    )
                )
                continue
            self.dispatch_service.retry_failed_command(command.id)
            results.append(BatchItemOutcome(batch_item_id, "ACCEPTED"))
        return results

    # --- 취소 ---------------------------------------------------------

    def cancel_item(self, batch_item_id: int, expected_status: str, actor: Optional[int]) -> FulfillmentBatchItem:
        """READY부터 PACKED까지만 취소할 수 있다 - SUBMIT_PENDING/SUBMITTED는
        상태머신에 CANCELLED 경로가 없다(모듈 docstring 및 fulfillment_state_
        machine.INVENTORY_DEDUCTED_STATUSES 주석 참고 - 이미 outbox 명령이
        생성된 뒤라 배치 항목만 취소된 것처럼 보이면 실제 전송과 어긋난다)."""
        item = self._require_item(batch_item_id)
        if item.status != expected_status:
            raise FulfillmentConflictError(f"현재 상태가 예상과 다릅니다(현재: {item.status}).")
        try:
            validate_transition(item.status, "CANCELLED")
        except InvalidFulfillmentTransitionError as e:
            raise FulfillmentValidationError(str(e)) from e

        needs_restock = item.status in INVENTORY_DEDUCTED_STATUSES and item.inventory_deducted_at is not None
        transitioned = self.item_repo.claim_transition(item.id, expected_status, "CANCELLED")
        if not transitioned:
            raise FulfillmentConflictError("다른 요청에 의해 상태가 이미 변경되었습니다.")

        if needs_restock:
            batch = self.batch_repo.get_by_id(item.batch_id)
            inventory = (
                self.inventory_service.inventory_repo.get_by_option_and_warehouse(
                    item.product_option_id, batch.warehouse_id
                )
                if batch is not None
                else None
            )
            if inventory is None:
                raise FulfillmentValidationError("재고 레코드를 찾을 수 없어 복원할 수 없습니다.")
            self.inventory_service.transition_stock(
                inventory=inventory,
                from_status=None,
                to_status=InventoryStatus.SELLABLE,
                quantity=item.verified_quantity or item.requested_quantity,
                reason="출고 취소 - 재고 복원",
                reference_type="MANUAL",
                reference_id=item.id,
            )

        self._record_history(item.id, expected_status, "CANCELLED", actor, "출고 취소")
        self.session.refresh(item)
        return item

    # --- 내부 헬퍼 -------------------------------------------------------

    def _require_item(self, batch_item_id: int) -> FulfillmentBatchItem:
        item = self.item_repo.get_by_id(batch_item_id)
        if item is None:
            raise FulfillmentValidationError(f"출고 배치 항목을 찾을 수 없습니다: id={batch_item_id}")
        return item

    def _claim_or_conflict(
        self,
        batch_item_id: int,
        expected_status: str,
        new_status: str,
        actor: Optional[int],
        extra: Optional[dict] = None,
    ) -> FulfillmentBatchItem:
        try:
            validate_transition(expected_status, new_status)
        except InvalidFulfillmentTransitionError as e:
            raise FulfillmentValidationError(str(e)) from e
        transitioned = self.item_repo.claim_transition(batch_item_id, expected_status, new_status, **(extra or {}))
        if not transitioned:
            raise FulfillmentConflictError(
                f"현재 상태가 예상({expected_status})과 다르거나 이미 변경되었습니다: batch_item_id={batch_item_id}"
            )
        self._record_history(batch_item_id, expected_status, new_status, actor, None)
        item = self.item_repo.get_by_id(batch_item_id)
        assert item is not None
        return item

    def _transition(self, batch_item_id: int, new_status: str, actor: Optional[int], **extra: object) -> None:
        item = self.item_repo.get_by_id(batch_item_id)
        if item is None:
            return
        from_status = item.status
        self.item_repo.claim_transition(batch_item_id, from_status, new_status, **extra)
        reason = extra.get("failure_reason_code")
        self._record_history(batch_item_id, from_status, new_status, actor, str(reason) if reason is not None else None)

    def _record_history(
        self, batch_item_id: int, from_status: Optional[str], to_status: str, actor: Optional[int], note: Optional[str]
    ) -> None:
        self.history_repo.add(
            FulfillmentBatchItemHistory(
                batch_item_id=batch_item_id,
                from_status=from_status,
                to_status=to_status,
                changed_by=actor,
                changed_at=datetime.now(timezone.utc),
                note=note[:300] if note else None,
            )
        )
