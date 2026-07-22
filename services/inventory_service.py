"""
services/inventory_service.py
----------------------------------
재고 예약/차감/복원/수동조정 업무로직.

models/inventory.py 설계 원칙: "모든 재고 변동은 반드시 inventory_transactions를
거친다"에 대응하여, 이 서비스의 모든 메서드는 inventory.sellable_stock/
reserved_stock을 갱신함과 동시에 InventoryTransaction 이력을 남긴다
(현재 stock을 바꾸지 않는 reserve()와, 재고량 자체를 바꾸지 않는
update_safety_stock()만 예외).
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Union

from sqlalchemy.orm import Session

from models.inventory import Inventory, InventoryStatus, InventoryTransaction
from repositories.inventory_repository import InventoryRepository


class InsufficientStockError(Exception):
    """가용재고보다 많은 수량을 출고하려 할 때 발생한다."""


class InvalidTransitionError(Exception):
    """전이 행렬에 없는 재고 상태 전이를 시도할 때 발생한다."""


class InventoryInvariantError(Exception):
    """재고 불변조건(I1~I6)을 위반하는 변경을 시도할 때 발생한다."""


@dataclass(frozen=True)
class TransitionResult:
    """transition_stock()의 반환 계약: 변경된 재고 + 생성된 이력."""

    inventory: Inventory
    transaction: InventoryTransaction


# 검수 결과 -> 도착 상태.
INSPECTION_RESULTS = {
    "PASS": InventoryStatus.SELLABLE,  # 양품 -> 판매가능
    "DEFECTIVE": InventoryStatus.DEFECTIVE,  # 불량 -> 불량보관
    "DISPOSED": InventoryStatus.DISPOSED,  # 폐기 -> 창고에 남지 않음(이벤트만)
}

# 허용 전이와 그때의 재고 칸 변화 계수 (sellable, defective).
# None = 창고 외부. 이 표에 없는 조합은 전부 금지다(DISPOSED에서 나가는 전이 포함).
_TRANSITIONS: dict[tuple[Optional[str], Optional[str]], tuple[int, int]] = {
    (None, InventoryStatus.SELLABLE): (+1, 0),  # 입고 / 검수합격
    (None, InventoryStatus.DEFECTIVE): (0, +1),  # 검수불량
    (None, InventoryStatus.DISPOSED): (0, 0),  # 검수폐기 - 어느 칸도 변하지 않음
    (InventoryStatus.SELLABLE, None): (-1, 0),  # 출고
    (InventoryStatus.SELLABLE, InventoryStatus.DEFECTIVE): (-1, +1),  # 파손 발견
    (InventoryStatus.SELLABLE, InventoryStatus.DISPOSED): (-1, 0),  # 파기
    (InventoryStatus.DEFECTIVE, InventoryStatus.SELLABLE): (+1, -1),  # 재검수 양품복귀
    (InventoryStatus.DEFECTIVE, InventoryStatus.DISPOSED): (0, -1),  # 폐기 처분
    (InventoryStatus.DEFECTIVE, None): (0, -1),  # 공급처 반송
}

# 업무 맥락(reference_type)에서 이력 type을 도출한다. transition_stock()은 type을
# 입력으로 받지 않고 항상 이 한 곳에서 일관되게 결정한다.
_TYPE_BY_REFERENCE = {"PURCHASE_ORDER": "IN", "RETURN": "INSPECT", "ORDER": "OUT", "MANUAL": "ADJUST"}


class InventoryService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.inventory_repo = InventoryRepository(session)

    # --- 재고 이동 엔진 --------------------------------------------------

    def transition_stock(
        self,
        inventory: Inventory,
        from_status: Optional[str],
        to_status: Optional[str],
        quantity: int,
        reason: str,
        reference_type: Optional[str] = None,
        reference_id: Optional[int] = None,
        actor: Union[int, str] = "SYSTEM",
    ) -> TransitionResult:
        """재고 칸 사이의 이동을 한 번 수행한다(재고 이동 엔진).

        계약
          - 대상 Inventory는 호출자가 조회/생성해 넘긴다(엔진은 찾지 않는다).
          - quantity는 항상 양수. 증감 방향은 from_status/to_status가 결정한다.
          - 한 번의 호출 = 한 번의 전이. 여러 이동은 상위 서비스가 반복 호출한다.
          - 성공하면 TransitionResult를 반환하고, 실패하면 예외를 던진다
            (False/None으로 실패를 표현하지 않는다).
          - inventory 변경과 inventory_transactions 기록은 같은 트랜잭션이다.
            여기서는 flush만 하고 commit은 호출자가 한다.

        실행 순서는 "검증 -> 변경 -> 기록"으로 고정한다. 변경 전에 모두 막으면
        되돌릴 것 자체가 생기지 않는다(롤백은 최후의 안전장치일 뿐이다).
        """
        if quantity <= 0:
            raise ValueError(f"이동 수량은 양수여야 합니다: quantity={quantity}")

        effect = _TRANSITIONS.get((from_status, to_status))
        if effect is None:
            raise InvalidTransitionError(
                f"허용되지 않는 재고 상태 전이입니다: {from_status or '외부'} -> {to_status or '외부'}"
            )

        sellable_delta = effect[0] * quantity
        defective_delta = effect[1] * quantity
        new_sellable = inventory.sellable_stock + sellable_delta
        new_defective = inventory.defective_stock + defective_delta

        # I6 - 어떤 칸도 음수가 될 수 없다(= 출발 칸에 재고가 모자라면 이동 불가).
        if new_sellable < 0 or new_defective < 0:
            raise InsufficientStockError(
                f"이동할 재고가 부족합니다: option={inventory.product_option_id}, "
                f"warehouse={inventory.warehouse_id}, 요청={quantity}, "
                f"판매가능={inventory.sellable_stock}, 불량={inventory.defective_stock}"
            )
        # I1 - 예약분이 판매가능 재고를 넘을 수 없다.
        if inventory.reserved_stock > new_sellable:
            raise InventoryInvariantError(
                f"예약 재고가 판매가능 재고를 초과합니다: "
                f"reserved={inventory.reserved_stock}, sellable={new_sellable}"
            )

        now = datetime.now(timezone.utc)
        inventory.sellable_stock = new_sellable
        inventory.defective_stock = new_defective
        inventory.updated_at = now

        # 창고 밖으로 나가는 이동만 음수로 기록한다(기존 OUT 이력 표기와 동일).
        signed_quantity = -quantity if to_status is None else quantity
        # TODO: 감사 컬럼(actor) 추가 시 여기에 기록한다. 지금은 구조화된 감사 정보를
        #       memo 문자열에 섞지 않고, 상위 계층의 감사 로그가 담당한다.
        transaction = InventoryTransaction(
            product_option_id=inventory.product_option_id,
            warehouse_id=inventory.warehouse_id,
            type=self._derive_type(to_status, reference_type),
            quantity=signed_quantity,
            from_status=from_status,
            to_status=to_status,
            reference_type=reference_type,
            reference_id=reference_id,
            memo=reason,
            created_at=now,
        )
        self.session.add(transaction)
        self.session.flush()
        return TransitionResult(inventory=inventory, transaction=transaction)

    @staticmethod
    def _derive_type(to_status: Optional[str], reference_type: Optional[str]) -> str:
        """이력 type을 업무 맥락에서 도출한다(입력으로 받지 않는다)."""
        if to_status == InventoryStatus.DISPOSED:
            return "DISPOSE"
        return _TYPE_BY_REFERENCE.get(reference_type or "", "ADJUST")

    # --- 반품 검수 업무 --------------------------------------------------

    def inspect_return(
        self,
        product_option_id: int,
        warehouse_id: int,
        quantity: int,
        result: str,
        reason: str,
        reference_id: Optional[int] = None,
        actor: Union[int, str] = "SYSTEM",
    ) -> TransitionResult:
        """반품 검수 판정(반품 재고 변경의 단일 진실 원천).

        검수 결과만 판정하고 재고는 직접 만지지 않는다 - 반드시 transition_stock()을
        통해서만 바꾼다. 그래야 재고 변경 경로가 엔진 하나로 수렴한다.

        result: PASS(양품) / DEFECTIVE(불량) / DISPOSED(폐기)
        """
        to_status = INSPECTION_RESULTS.get(result)
        if to_status is None:
            raise ValueError(f"허용되지 않는 검수 결과입니다: {result}")

        inv = self._get_or_create_inventory(product_option_id, warehouse_id)
        return self.transition_stock(
            inventory=inv,
            from_status=None,  # 회수품은 창고 밖에서 들어온다
            to_status=to_status,
            quantity=quantity,
            reason=reason,
            reference_type="RETURN",
            reference_id=reference_id,
            actor=actor,
        )

    # --- 예약 ------------------------------------------------------------

    def reserve(self, product_option_id: int, warehouse_id: int, quantity: int) -> Inventory:
        """신규 주문 접수 시 재고를 예약한다(reserved_stock 증가)."""
        inv = self._get_inventory(product_option_id, warehouse_id)
        new_reserved = inv.reserved_stock + quantity
        # I1 - 없는 재고를 예약할 수 없다(초과 판매 방지).
        if new_reserved > inv.sellable_stock:
            raise InventoryInvariantError(
                f"판매가능 재고보다 많이 예약할 수 없습니다: option={product_option_id}, "
                f"warehouse={warehouse_id}, 요청={quantity}, "
                f"예약={inv.reserved_stock}, 판매가능={inv.sellable_stock}"
            )
        inv.reserved_stock = new_reserved
        inv.updated_at = datetime.now(timezone.utc)
        self.session.flush()
        return inv

    def release_reservation(self, product_option_id: int, warehouse_id: int, quantity: int) -> Inventory:
        """주문 취소 등으로 예약을 해제한다.

        I3 - reserved_stock만 줄인다. sellable_stock은 건드리지 않는다
        (물건은 여전히 창고에 있고 배정만 풀린 것이다).
        """
        inv = self._get_inventory(product_option_id, warehouse_id)
        # I6 - 음수가 되지 않도록 하한 0.
        inv.reserved_stock = max(inv.reserved_stock - quantity, 0)
        inv.updated_at = datetime.now(timezone.utc)
        self.session.flush()
        return inv

    def deduct_on_shipment(
        self,
        product_option_id: int,
        warehouse_id: int,
        quantity: int,
        reference_id: Optional[int] = None,
        release_reserved: bool = True,
    ) -> Inventory:
        """출고 시 실재고를 차감(OUT)하고, 함께 예약 재고도 해제한다.

        I2 - sellable_stock과 reserved_stock은 반드시 함께 줄어든다. 한쪽만 줄면
        예약이 영구히 남아 재고가 잠긴다. 예약 해제를 먼저 하는 이유는, sellable을
        먼저 줄이면 그 순간 reserved > sellable이 되어 I1을 스스로 위반하기 때문이다.
        둘 다 같은 트랜잭션 안에서 일어나므로 원자성은 보장된다.
        """
        inv = self._get_inventory(product_option_id, warehouse_id)
        if inv.sellable_stock < quantity:
            raise InsufficientStockError(
                f"재고 부족: option={product_option_id}, warehouse={warehouse_id}, "
                f"요청={quantity}, 현재={inv.sellable_stock}"
            )
        if release_reserved:
            inv.reserved_stock = max(inv.reserved_stock - quantity, 0)
        result = self.transition_stock(
            inventory=inv,
            from_status=InventoryStatus.SELLABLE,
            to_status=None,  # 창고 밖으로 나간다
            quantity=quantity,
            reason="출고(발송처리)",
            reference_type="ORDER",
            reference_id=reference_id,
        )
        return result.inventory

    def receive_purchase_order(
        self, product_option_id: int, warehouse_id: int, quantity: int, reference_id: int
    ) -> Inventory:
        """발주 입고 시 실재고를 증가시킨다(IN). 이 조합에 재고 레코드가 없으면 0에서 시작해 생성한다.

        재고 변경은 엔진(transition_stock)을 통해서만 한다 - 직접 컬럼을 만지면
        전이 검증과 불변조건 검사를 우회하게 되고, from/to_status가 비어 이력으로
        재고를 재구성할 수 없게 된다.
        """
        inv = self._get_or_create_inventory(product_option_id, warehouse_id)
        result = self.transition_stock(
            inventory=inv,
            from_status=None,  # 창고 외부(공급처)에서 유입
            to_status=InventoryStatus.SELLABLE,
            quantity=quantity,
            reason="발주 입고",
            reference_type="PURCHASE_ORDER",
            reference_id=reference_id,
        )
        return result.inventory

    def manual_adjust(
        self, product_option_id: int, warehouse_id: int, delta: int, memo: Optional[str] = None
    ) -> Inventory:
        """재고 실사/입고 등으로 현재재고를 수동 증감한다(재고관리 화면 전용).
        delta는 증가(+)/감소(-) 모두 가능하며, 결과가 음수가 되는 조정은 거부한다.
        신규 조합(이 옵션×창고에 아직 재고 레코드가 없는 경우)은 0에서 시작해 생성한다.

        증감 방향은 엔진에 from/to_status로 전달한다(엔진 계약: quantity는 항상 양수).
          delta > 0  →  외부 → SELLABLE  (실사 증가/입고)
          delta < 0  →  SELLABLE → 외부  (실사 감모)
        엔진을 거치므로 예약분보다 적게 남기는 조정은 I1 위반으로 거부된다 -
        직접 컬럼을 만지던 시절에는 이 검사가 없어 reserved > sellable을 만들 수 있었다.
        """
        if delta == 0:
            raise ValueError("조정 수량이 0입니다. 재고가 변하지 않는 조정은 기록하지 않습니다.")
        inv = self._get_or_create_inventory(product_option_id, warehouse_id)
        result = self.transition_stock(
            inventory=inv,
            from_status=None if delta > 0 else InventoryStatus.SELLABLE,
            to_status=InventoryStatus.SELLABLE if delta > 0 else None,
            quantity=abs(delta),
            reason=memo or "재고 수동 조정",
            reference_type="MANUAL",
        )
        return result.inventory

    def update_safety_stock(self, product_option_id: int, warehouse_id: int, safety_stock: int) -> Inventory:
        """안전재고 기준값을 변경한다 - 재고 수량 자체는 바꾸지 않으므로 이력(InventoryTransaction)을
        남기지 않는다(재고 변동이 아니라 기준값 설정이기 때문)."""
        inv = self._get_or_create_inventory(product_option_id, warehouse_id)
        inv.safety_stock = safety_stock
        inv.updated_at = datetime.now(timezone.utc)
        self.session.flush()
        return inv

    def _get_inventory(self, product_option_id: int, warehouse_id: int) -> Inventory:
        inv = self.inventory_repo.get_by_option_and_warehouse(product_option_id, warehouse_id)
        if inv is None:
            raise ValueError(f"재고 레코드가 없습니다: option={product_option_id}, warehouse={warehouse_id}")
        return inv

    def _get_or_create_inventory(self, product_option_id: int, warehouse_id: int) -> Inventory:
        inv = self.inventory_repo.get_by_option_and_warehouse(product_option_id, warehouse_id)
        if inv is None:
            inv = self.inventory_repo.add(
                Inventory(
                    product_option_id=product_option_id,
                    warehouse_id=warehouse_id,
                    sellable_stock=0,
                    reserved_stock=0,
                    safety_stock=0,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            self.session.flush()
        return inv
