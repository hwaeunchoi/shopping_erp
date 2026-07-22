"""
services/blocked_reason_service.py
--------------------------------------
막힌 사유(Blocked Reason) 엔진 — ERP 전 화면이 공유하는 공통 언어.

운영자가 마감 때 묻는 질문은 하나다: "왜 이 주문이 아직 안 나갔나?"
이 엔진은 그 답을 주문 상세를 열지 않고도 목록에서 바로 보이게 만든다.

설계 원칙
- 모든 사유는 코드·표시명·심각도·해결화면·해결동작을 갖는다
- 차단(BLOCK) 사유는 원칙적으로 주문관리 화면에서 그 자리에서 해결 가능해야 한다
- 판정은 항상 벌크로 수행한다(목록 50~500건에 대해 N+1 쿼리를 내지 않는다)

일부 사유는 아직 스키마가 없어 판정을 못 한다(detectable=False). 레지스트리에는
미리 정의해 두고, 해당 컬럼이 추가되는 단계에서 판정만 활성화한다 - 사유 코드가
전 화면의 공통 언어이므로 목록 자체는 먼저 확정해 두는 것이 안전하다.
"""

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from models.order import Order
from repositories.inventory_repository import InventoryRepository
from repositories.order_repository import OrderRepository, ShipmentRepository
from repositories.purchase_order_repository import PurchaseOrderItemRepository
from repositories.supplier_repository import ProductSupplierMapRepository

# 심각도 - 화면 표시 우선순위이자 정렬 기준
BLOCK = "BLOCK"  # 🔴 발송 불가
DELAY = "DELAY"  # 🟠 대기 중(외부 요인)
WARN = "WARN"  # 🟡 확인 필요
INFO = "INFO"  # ⚪ 정보

SEVERITY_ORDER = {BLOCK: 0, DELAY: 1, WARN: 2, INFO: 3}


@dataclass(frozen=True)
class ReasonDef:
    code: str
    label: str
    severity: str
    resolve_screen: str  # 어디서 푸는가
    resolve_action: str  # 무엇을 하면 풀리는가
    detectable: bool = True  # False면 스키마 대기(판정 미구현)


# 업무 설계서 5장의 17종 사유 레지스트리.
REASON_REGISTRY: dict[str, ReasonDef] = {
    d.code: d
    for d in [
        ReasonDef("PRODUCT_UNMATCHED", "상품 미매칭", BLOCK, "주문관리", "SKU 지정 → 매핑저장"),
        ReasonDef("OPTION_UNMATCHED", "옵션 미매칭", BLOCK, "주문관리", "옵션 매핑", detectable=False),
        ReasonDef("SKU_NOT_REGISTERED", "SKU 미등록", BLOCK, "상품관리", "상품 신규 등록", detectable=False),
        ReasonDef("SUPPLIER_UNASSIGNED", "공급처 없음", BLOCK, "주문관리", "공급처 지정"),
        ReasonDef("STOCK_SHORTAGE", "재고 부족", BLOCK, "주문관리", "발주 생성"),
        ReasonDef("PO_REQUIRED", "발주 필요", BLOCK, "주문관리", "인라인 발주"),
        ReasonDef("PO_PENDING_RECEIPT", "입고 대기", DELAY, "발주관리", "입고 등록"),
        ReasonDef("PO_DELAYED", "입고 지연", DELAY, "발주관리", "공급처 확인", detectable=False),
        ReasonDef("INVOICE_MISSING", "송장 없음", DELAY, "주문관리", "송장 등록"),
        ReasonDef("CARRIER_UNASSIGNED", "택배사 미지정", WARN, "주문관리", "택배사 지정"),
        ReasonDef("SHIPPING_FEE_CHECK", "배송비 확인", WARN, "주문관리", "배송비 확정", detectable=False),
        ReasonDef("CHANNEL_SEND_FAILED", "채널 전송 실패", BLOCK, "주문관리", "재전송", detectable=False),
        ReasonDef("ADDRESS_INVALID", "주소 오류", BLOCK, "주문관리", "주소 수정"),
        ReasonDef("PAYMENT_INCOMPLETE", "결제 미완료", DELAY, "주문관리", "입금 확인"),
        ReasonDef("ORDER_HOLD", "주문 보류", INFO, "주문관리", "보류 해제", detectable=False),
        ReasonDef("CS_IN_PROGRESS", "CS 진행중", WARN, "주문관리", "CS 처리"),
        ReasonDef("SETTLEMENT_MISMATCH", "정산 차액", WARN, "정산", "차액 원인 규명", detectable=False),
    ]
}

# 발송을 실제로 막는 사유(액션 큐 '피킹대기' 판정에 쓴다).
BLOCKING_CODES = {code for code, d in REASON_REGISTRY.items() if d.severity == BLOCK}

# 이미 발송된 주문에는 출고 관련 사유를 매기지 않는다.
_SHIPPED_STATUSES = {"SHIPPING", "DELIVERED"}
_CLOSED_STATUSES = {"CANCELED", "RETURNED", "REFUNDED", "EXCHANGED"}


class BlockedReasonService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.order_repo = OrderRepository(session)
        self.inventory_repo = InventoryRepository(session)
        self.supplier_map_repo = ProductSupplierMapRepository(session)
        self.po_item_repo = PurchaseOrderItemRepository(session)
        self.shipment_repo = ShipmentRepository(session)

    def detect_bulk(self, orders: list[Order]) -> dict[int, list[dict]]:
        """주문 목록의 막힌 사유를 한 번에 판정한다. {order_id: [사유, ...]}

        목록 화면이 매 행마다 쿼리를 내지 않도록 필요한 데이터를 먼저 벌크로 적재한다.
        """
        if not orders:
            return {}

        order_ids = [o.id for o in orders]
        items = self.order_repo.list_items_by_order_ids(order_ids)
        items_by_order: dict[int, list] = {}
        for item in items:
            items_by_order.setdefault(item.order_id, []).append(item)

        option_ids = list({item.product_option_id for item in items})

        # 옵션별 가용재고
        available: dict[int, int] = {}
        for inv in self.inventory_repo.list_by_options(option_ids):
            available[inv.product_option_id] = (
                available.get(inv.product_option_id, 0) + inv.sellable_stock - inv.reserved_stock
            )

        # 옵션별 공급처 지정 여부
        supplied_options = {m.product_option_id for m in self.supplier_map_repo.list_by_product_options(option_ids)}

        # 옵션별 미완료 발주 상태
        open_po: dict[int, str] = dict(self.po_item_repo.list_open_by_product_options(option_ids))

        # 주문별 배송(송장) 유무
        shipments_by_order: dict[int, list] = {}
        for order_id, shipment in self.shipment_repo.list_by_orders(order_ids):
            shipments_by_order.setdefault(order_id, []).append(shipment)

        cs_order_ids = self.order_repo.list_order_ids_with_cs(order_ids)

        return {
            o.id: self._detect_one(
                o,
                items_by_order.get(o.id, []),
                available,
                supplied_options,
                open_po,
                shipments_by_order.get(o.id, []),
                o.id in cs_order_ids,
            )
            for o in orders
        }

    def detect(self, order: Order) -> list[dict]:
        """단건 판정(상세 패널용)."""
        return self.detect_bulk([order]).get(order.id, [])

    def summary(self, orders: list[Order]) -> list[dict]:
        """사유별 집계 - 마감 점검("오늘 못 나간 41건의 내역")용."""
        counter: dict[str, int] = {}
        for reasons in self.detect_bulk(orders).values():
            for r in reasons:
                counter[r["code"]] = counter.get(r["code"], 0) + 1
        return sorted(
            (
                {
                    "code": code,
                    "label": REASON_REGISTRY[code].label,
                    "severity": REASON_REGISTRY[code].severity,
                    "count": count,
                }
                for code, count in counter.items()
            ),
            key=lambda x: (SEVERITY_ORDER[str(x["severity"])], -int(x["count"])),
        )

    # --- 판정 본체 -------------------------------------------------------

    def _detect_one(
        self,
        order: Order,
        items: list,
        available: dict[int, int],
        supplied_options: set[int],
        open_po: dict[int, str],
        shipments: list,
        has_cs: bool,
    ) -> list[dict]:
        reasons: list[dict] = []
        is_shipped = order.status in _SHIPPED_STATUSES
        is_closed = order.status in _CLOSED_STATUSES

        if has_cs:
            reasons.append(self._reason("CS_IN_PROGRESS"))

        # 종결된 주문에는 출고 관련 사유를 매기지 않는다.
        if is_closed:
            return self._sorted(reasons)

        if order.payment_date is None and order.status == "NEW":
            reasons.append(self._reason("PAYMENT_INCOMPLETE"))

        if not items:
            # 수집 시 SKU 매칭에 실패하면 주문품목이 아예 생성되지 않는다.
            reasons.append(self._reason("PRODUCT_UNMATCHED"))
        elif not is_shipped:
            reasons.extend(self._supply_reasons(items, available, supplied_options, open_po))

        if not is_shipped and not self._has_valid_address(order):
            reasons.append(self._reason("ADDRESS_INVALID"))

        if not is_shipped and items:
            reasons.extend(self._shipping_reasons(order, shipments))

        return self._sorted(reasons)

    def _supply_reasons(
        self, items: list, available: dict[int, int], supplied_options: set[int], open_po: dict[int, str]
    ) -> list[dict]:
        """재고·공급처·발주 관련 사유. 한 주문에 여러 품목이 있어도 사유는 유형당 1개로 합친다."""
        out: list[dict] = []
        shortage_total = 0
        missing_supplier = False
        awaiting_receipt = False

        for item in items:
            option_id = item.product_option_id
            shortage = item.quantity - available.get(option_id, 0)
            if shortage <= 0:
                continue
            shortage_total += shortage
            if option_id in open_po:
                awaiting_receipt = True  # 이미 발주가 걸려 있으면 '입고 대기'
            elif option_id not in supplied_options:
                missing_supplier = True  # 공급처가 없으면 발주 자체가 불가
        if shortage_total > 0:
            out.append(self._reason("STOCK_SHORTAGE", label=f"재고부족 {shortage_total}개"))
            if missing_supplier:
                out.append(self._reason("SUPPLIER_UNASSIGNED"))
            elif awaiting_receipt:
                out.append(self._reason("PO_PENDING_RECEIPT"))
            else:
                out.append(self._reason("PO_REQUIRED"))
        return out

    def _shipping_reasons(self, order: Order, shipments: list) -> list[dict]:
        out: list[dict] = []
        if not shipments:
            # 주문확인이 끝난(발송준비) 주문만 '송장 없음'으로 본다.
            if order.status == "PREPARING":
                out.append(self._reason("INVOICE_MISSING"))
        elif any(s.tracking_no and not s.carrier for s in shipments):
            out.append(self._reason("CARRIER_UNASSIGNED"))
        return out

    @staticmethod
    def _has_valid_address(order: Order) -> bool:
        """수취인 주소 유효성. 수취인 컬럼 도입 전에는 고객 주소로 판정한다."""
        customer = getattr(order, "customer", None)
        address = getattr(customer, "address", None) if customer else None
        return bool(address and address.strip())

    @staticmethod
    def _reason(code: str, label: Optional[str] = None) -> dict:
        d = REASON_REGISTRY[code]
        return {
            "code": d.code,
            "label": label or d.label,
            "severity": d.severity,
            "resolve_screen": d.resolve_screen,
            "resolve_action": d.resolve_action,
        }

    @staticmethod
    def _sorted(reasons: list[dict]) -> list[dict]:
        return sorted(reasons, key=lambda r: SEVERITY_ORDER[str(r["severity"])])
