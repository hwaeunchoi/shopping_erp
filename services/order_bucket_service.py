"""
services/order_bucket_service.py
------------------------------------
주문관리 버킷(운영자의 하루 작업 큐) 엔진.

이지어드민식 주문관리의 뼈대. 운영자는 필터를 조합하지 않고 버킷 탭을 왼쪽부터
0으로 비워나가면 하루 업무가 끝난다.

    신규주문 → 매칭필요 → 발주대기 → 송장대기 → 발송완료 → 배송완료 → CS

버킷은 상호배타적이며(한 주문은 정확히 한 버킷), 분류 우선순위는
OrderRepository._apply_bucket()에 SQL로 구현되어 있다. 여기서는 버킷 정의(표시명·
순서)와 건수 집계, 그리고 목록 행에 표시할 "막힌 사유" 판정을 담당한다.

'막힌 사유'는 운영자가 마감 때 묻는 질문("왜 안 나갔나")에 화면이 답하기 위한 것으로,
주문 상세를 열지 않고도 목록에서 바로 보이게 한다.
"""

from typing import Optional

from sqlalchemy.orm import Session

from models.order import Order
from repositories.inventory_repository import InventoryRepository
from repositories.order_repository import OrderRepository

# (코드, 표시명) — 화면 탭 순서와 동일하다.
BUCKET_DEFS: list[tuple[str, str]] = [
    ("NEW", "신규주문"),
    ("MATCH_REQUIRED", "매칭필요"),
    ("PO_REQUIRED", "발주대기"),
    ("AWAITING_INVOICE", "송장대기"),
    ("SHIPPED", "발송완료"),
    ("DELIVERED", "배송완료"),
    ("CS", "CS"),
]

BUCKET_CODES = [code for code, _ in BUCKET_DEFS]

# 운영자가 즉시 조치해야 하는 버킷(화면에서 빨강 강조).
URGENT_BUCKETS = {"MATCH_REQUIRED", "PO_REQUIRED"}


class OrderBucketService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.order_repo = OrderRepository(session)
        self.inventory_repo = InventoryRepository(session)

    def counts(self, **filters) -> list[dict]:
        """버킷별 건수. 검색/기간 등 현재 필터를 유지한 채 집계한다.

        운영자가 '오늘 쿠팡 주문'만 보고 있으면 버킷 건수도 그 범위로 좁혀져야
        화면이 거짓말을 하지 않는다.
        """
        return [
            {
                "code": code,
                "label": label,
                "count": self.order_repo.count_filtered(bucket=code, **filters),
                "urgent": code in URGENT_BUCKETS,
            }
            for code, label in BUCKET_DEFS
        ]

    def blocked_reasons(self, order: Order) -> list[dict]:
        """이 주문이 오늘 못 나가는 이유(목록 행에 뱃지로 표시).

        한 주문에 사유가 여러 개일 수 있으므로 버킷과 달리 목록으로 반환한다.
        """
        reasons: list[dict] = []

        items = self.order_repo.list_items(order.id)
        if not items:
            reasons.append({"code": "MATCH_REQUIRED", "label": "미매칭", "severity": "high"})

        for item in items:
            shortage = self._shortage(item.product_option_id, item.quantity)
            if shortage > 0:
                reasons.append({"code": "STOCK_SHORT", "label": f"재고부족 {shortage}개", "severity": "high"})
                break

        if self._has_cs(order.id):
            reasons.append({"code": "CS_HOLD", "label": "CS접수", "severity": "medium"})

        return reasons

    def _shortage(self, product_option_id: int, required: int) -> int:
        """부족 수량(가용재고 기준). 0 이하면 부족하지 않다."""
        available = sum(
            inv.sellable_stock - inv.reserved_stock for inv in self.inventory_repo.list_by_option(product_option_id)
        )
        return max(required - available, 0)

    def _has_cs(self, order_id: int) -> bool:
        from repositories.order_repository import CancellationRepository, ExchangeRepository, ReturnRepository

        return bool(
            ExchangeRepository(self.session).list_filtered(order_id=order_id)
            or ReturnRepository(self.session).list_filtered(order_id=order_id)
            or CancellationRepository(self.session).list_filtered(order_id=order_id)
        )

    @staticmethod
    def is_valid(bucket: Optional[str]) -> bool:
        return bucket is None or bucket in BUCKET_CODES
