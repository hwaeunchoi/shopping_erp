"""
services/order_view_service.py
----------------------------------
주문관리 화면(이지어드민 수준) 전용 조회 서비스.

목록 행과 상세 패널에 필요한 파생 상태/연관 정보를 조립한다. 기존 스키마를
그대로 활용하며 신규 컬럼 없이 계산 가능한 것만 다룬다:

- 결제상태: payment_date 유무 (PAID/UNPAID)
- 배송상태: shipments.status (없으면 UNSHIPPED)
- CS상태: 교환/반품/취소 신청 존재 여부 (EXCHANGE/RETURN/CANCEL/NONE)
- 공급처: product_supplier_map -> suppliers (대표 공급처)
- 채널상품/옵션번호: product_platform_map
- 택배사/송장번호: shipments

발주상태·순이익은 신규 집계가 필요해 이 서비스의 1차 범위에서 제외한다.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from repositories.customer_repository import CustomerRepository
from repositories.extra_repository import MemoRepository
from repositories.inventory_repository import InventoryRepository
from repositories.order_repository import (
    CancellationRepository,
    ExchangeRepository,
    OrderRepository,
    ReturnRepository,
    ShipmentRepository,
)
from repositories.platform_repository import PlatformFeeRuleRepository, PlatformRepository
from repositories.product_repository import ProductOptionRepository, ProductPlatformMapRepository, ProductRepository
from repositories.purchase_order_repository import PurchaseOrderItemRepository, PurchaseOrderRepository
from repositories.settlement_repository import SettlementRepository
from repositories.supplier_repository import ProductSupplierMapRepository, SupplierRepository
from repositories.user_repository import UserRepository

DELAYED_THRESHOLD_DAYS = 2
DEFAULT_FEE_RATE = 0.0


class OrderViewService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.order_repo = OrderRepository(session)
        self.platform_repo = PlatformRepository(session)
        self.customer_repo = CustomerRepository(session)
        self.option_repo = ProductOptionRepository(session)
        self.product_repo = ProductRepository(session)
        self.ppm_repo = ProductPlatformMapRepository(session)
        self.psm_repo = ProductSupplierMapRepository(session)
        self.supplier_repo = SupplierRepository(session)
        self.shipment_repo = ShipmentRepository(session)
        self.exchange_repo = ExchangeRepository(session)
        self.return_repo = ReturnRepository(session)
        self.cancellation_repo = CancellationRepository(session)
        self.inventory_repo = InventoryRepository(session)
        self.memo_repo = MemoRepository(session)
        self.user_repo = UserRepository(session)
        self.fee_rule_repo = PlatformFeeRuleRepository(session)
        self.po_repo = PurchaseOrderRepository(session)
        self.po_item_repo = PurchaseOrderItemRepository(session)
        self.settlement_repo = SettlementRepository(session)

    def _payment_status(self, order) -> str:
        return "PAID" if order.payment_date else "UNPAID"

    def _shipping_status(self, order_id: int) -> tuple[str, Optional[str], Optional[str]]:
        shipment = self.shipment_repo.get_by_order(order_id)
        if shipment is None:
            return "UNSHIPPED", None, None
        status = shipment.status if shipment.status != "READY" else "UNSHIPPED"
        return status, shipment.carrier, shipment.tracking_no

    def _cs_status(self, order_id: int) -> str:
        if self.exchange_repo.list_filtered(order_id=order_id):
            return "EXCHANGE"
        if self.return_repo.list_filtered(order_id=order_id):
            return "RETURN"
        if self.cancellation_repo.list_filtered(order_id=order_id):
            return "CANCEL"
        return "NONE"

    def _is_delayed(self, order) -> bool:
        if order.status not in ("NEW", "PREPARING"):
            return False
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=DELAYED_THRESHOLD_DAYS)
        order_dt = order.order_date.replace(tzinfo=None) if order.order_date.tzinfo else order.order_date
        return order_dt < cutoff

    def build_rows(self, orders: list) -> list[dict]:
        """목록 행 조립. 대표 상품(첫 품목) 기준으로 상품/공급처/채널 정보를 요약한다."""
        rows = []
        for order in orders:
            items = self.order_repo.list_items(order.id)
            total_qty = sum(i.quantity for i in items)
            first = items[0] if items else None

            product_name = option_name = sku_code = None
            channel_product_no = channel_option_no = supplier_name = None
            if first is not None:
                option = self.option_repo.get_by_id(first.product_option_id)
                if option is not None:
                    sku_code = option.sku_code
                    option_name = option.option_name
                    product = self.product_repo.get_by_id(option.product_id)
                    product_name = product.name if product else None
                    maps = [m for m in self.ppm_repo.list_by_option(option.id) if m.platform_id == order.platform_id]
                    if maps:
                        channel_product_no = maps[0].platform_product_id
                        channel_option_no = maps[0].platform_option_id
                    psm = self.psm_repo.list_by_product_option(option.id)
                    if psm:
                        supplier = self.supplier_repo.get_by_id(psm[0].supplier_id)
                        supplier_name = supplier.name if supplier else None

            platform = self.platform_repo.get_by_id(order.platform_id)
            customer = self.customer_repo.get_by_id(order.customer_id) if order.customer_id else None
            shipping_status, carrier, tracking_no = self._shipping_status(order.id)
            assignee = self.user_repo.get_by_id(order.assignee_id) if order.assignee_id else None

            rows.append(
                {
                    "id": order.id,
                    "platform_id": order.platform_id,
                    "platform_name": platform.name if platform else None,
                    "platform_order_no": order.platform_order_no,
                    "order_status": order.status,
                    "payment_status": self._payment_status(order),
                    "shipping_status": shipping_status,
                    "cs_status": self._cs_status(order.id),
                    "order_date": order.order_date,
                    "payment_date": order.payment_date,
                    "delivery_completed_date": order.delivery_completed_date,
                    "is_delayed": self._is_delayed(order),
                    "customer_name": customer.name if customer else None,
                    "customer_phone": customer.phone if customer else None,
                    "customer_address": customer.address if customer else None,
                    "product_name": product_name,
                    "option_name": option_name,
                    "sku_code": sku_code,
                    "channel_product_no": channel_product_no,
                    "channel_option_no": channel_option_no,
                    "item_count": len(items),
                    "total_quantity": total_qty,
                    "supplier_name": supplier_name,
                    "carrier": carrier,
                    "tracking_no": tracking_no,
                    "total_amount": float(order.total_amount),
                    "assignee_id": order.assignee_id,
                    "assignee_name": assignee.name if assignee else None,
                    "tags": order.tags,
                }
            )
        return rows

    def build_detail(self, order) -> dict:
        """상세 패널: 주문/품목/재고/배송/고객/메모/로그를 한 응답으로 조립한다."""
        items = self.order_repo.list_items(order.id)
        item_views = []
        inventory_views = []
        for item in items:
            option = self.option_repo.get_by_id(item.product_option_id)
            product = self.product_repo.get_by_id(option.product_id) if option else None
            maps = (
                [m for m in self.ppm_repo.list_by_option(option.id) if m.platform_id == order.platform_id]
                if option
                else []
            )
            item_views.append(
                {
                    "id": item.id,
                    "product_option_id": item.product_option_id,
                    "product_name": product.name if product else None,
                    "option_name": option.option_name if option else None,
                    "sku_code": option.sku_code if option else None,
                    "channel_product_no": maps[0].platform_product_id if maps else None,
                    "channel_option_no": maps[0].platform_option_id if maps else None,
                    "quantity": item.quantity,
                    "unit_price": float(item.unit_price),
                    "line_amount": float(item.line_amount),
                }
            )
            if option:
                for inv in self.inventory_repo.list_by_option(option.id):
                    inventory_views.append(
                        {
                            "sku_code": option.sku_code,
                            "warehouse_id": inv.warehouse_id,
                            "sellable_stock": inv.sellable_stock,
                            "reserved_stock": inv.reserved_stock,
                            "available_stock": inv.sellable_stock - inv.reserved_stock,
                            "safety_stock": inv.safety_stock,
                        }
                    )

        platform = self.platform_repo.get_by_id(order.platform_id)
        customer = self.customer_repo.get_by_id(order.customer_id) if order.customer_id else None
        shipment = self.shipment_repo.get_by_order(order.id)
        memos = self.memo_repo.list_by_target("ORDER", order.id)
        history = sorted(order.status_history, key=lambda h: h.changed_at, reverse=True)
        assignee = self.user_repo.get_by_id(order.assignee_id) if order.assignee_id else None

        return {
            "id": order.id,
            "platform_order_no": order.platform_order_no,
            "platform_name": platform.name if platform else None,
            "order_status": order.status,
            "payment_status": self._payment_status(order),
            "shipping_status": self._shipping_status(order.id)[0],
            "cs_status": self._cs_status(order.id),
            "order_date": order.order_date,
            "payment_date": order.payment_date,
            "delivery_completed_date": order.delivery_completed_date,
            "total_amount": float(order.total_amount),
            "discount_amount": float(order.discount_amount),
            "assignee_id": order.assignee_id,
            "assignee_name": assignee.name if assignee else None,
            "tags": order.tags,
            "purchase_links": self._purchase_links(items),
            "contribution": self._contribution_margin(order, items, platform),
            "settlement": self._settlement_link(order.id),
            "cs_history": self._cs_history(order.id),
            "customer": (
                None
                if customer is None
                else {
                    "id": customer.id,
                    "name": customer.name,
                    "phone": customer.phone,
                    "email": customer.email,
                    "address": customer.address,
                    "grade": customer.grade,
                    "is_vip": customer.is_vip,
                    "order_count": customer.order_count,
                }
            ),
            "items": item_views,
            "inventory": inventory_views,
            "shipment": (
                None
                if shipment is None
                else {
                    "carrier": shipment.carrier,
                    "tracking_no": shipment.tracking_no,
                    "status": shipment.status,
                    "shipped_at": shipment.shipped_at,
                    "delivered_at": shipment.delivered_at,
                }
            ),
            "memos": [
                {"id": m.id, "content": m.content, "created_by": m.created_by, "created_at": m.created_at}
                for m in memos
            ],
            "status_history": [
                {"from_status": h.from_status, "to_status": h.to_status, "changed_at": h.changed_at} for h in history
            ],
        }

    def _fee_rate(self, platform, order_date) -> float:
        if platform is None:
            return DEFAULT_FEE_RATE
        rule = self.fee_rule_repo.get_effective_rule(platform.id, order_date)
        return float(rule.fee_rate) / 100 if rule else DEFAULT_FEE_RATE

    def _contribution_margin(self, order, items, platform) -> dict:
        """주문 단위 공헌이익(광고 전) = 판매액 - 원가스냅샷 - 플랫폼수수료.

        광고비는 주문 단위로 귀속시킬 근거가 없어 제외한다(그래서 순이익이 아니라
        '공헌이익'으로 명명). 배송/포장비도 주문 단위 배분 기준이 없어 이 화면에서는
        제외하고, 총계 손익은 손익분석 화면에서 별도로 다룬다.
        """
        sale = float(order.total_amount)
        cogs = sum(float(i.cost_price_snapshot or 0) * i.quantity for i in items)
        fee = sale * self._fee_rate(platform, order.order_date.date())
        margin = round(sale - cogs - fee, 2)
        rate = round(margin / sale * 100, 2) if sale > 0 else 0.0
        return {
            "sale_amount": round(sale, 2),
            "cost_of_goods": round(cogs, 2),
            "platform_fee": round(fee, 2),
            "contribution_margin": margin,
            "contribution_rate": rate,
            "ad_cost_note": "광고비는 주문 단위 귀속 불가로 제외(공헌이익 기준)",
        }

    def _purchase_links(self, items) -> list[dict]:
        """주문 품목의 옵션이 포함된 발주(purchase_order)를 파생 조회한다.

        주문-발주 직접 링크는 없으므로, 품목 옵션을 포함하는 발주 품목을 역추적해
        연결 후보로 보여준다(발주상태 확인용). 발주 1건이 여러 주문을 채우는 구조라
        정확한 귀속이 아닌 '연결 후보'임을 화면에서 명시한다.
        """
        option_ids = {i.product_option_id for i in items}
        if not option_ids:
            return []
        links: dict[int, dict] = {}
        for po in self.po_repo.list_all(limit=1000):
            for po_item in self.po_item_repo.list_by_purchase_order(po.id):
                if po_item.product_option_id in option_ids and po.id not in links:
                    supplier = self.supplier_repo.get_by_id(po.supplier_id)
                    links[po.id] = {
                        "purchase_order_id": po.id,
                        "supplier_name": supplier.name if supplier else None,
                        "status": po.status,
                        "order_date": po.order_date,
                    }
        return list(links.values())

    def _settlement_link(self, order_id: int) -> Optional[dict]:
        rows = self._settlement_details_for_order(order_id)
        if not rows:
            return None
        settlement_id, gross, fee, net = rows
        settlement = self.settlement_repo.get_by_id(settlement_id)
        return {
            "settlement_id": settlement_id,
            "settlement_cycle": settlement.settlement_cycle if settlement else None,
            "status": settlement.status if settlement else None,
            "gross_amount": float(gross),
            "fee_amount": float(fee),
            "net_amount": float(net),
        }

    def _settlement_details_for_order(self, order_id: int):
        from sqlalchemy import func, select

        from models.settlement import SettlementDetail

        stmt = (
            select(
                SettlementDetail.settlement_id,
                func.sum(SettlementDetail.gross_amount),
                func.sum(SettlementDetail.fee_amount),
                func.sum(SettlementDetail.net_amount),
            )
            .where(SettlementDetail.order_id == order_id)
            .group_by(SettlementDetail.settlement_id)
        )
        return self.session.execute(stmt).first()

    def _cs_history(self, order_id: int) -> list[dict]:
        out: list[dict] = []
        for ex in self.exchange_repo.list_filtered(order_id=order_id):
            out.append(
                {
                    "type": "EXCHANGE",
                    "id": ex.id,
                    "reason": ex.reason,
                    "status": ex.status,
                    "requested_at": ex.requested_at,
                }
            )
        for ret in self.return_repo.list_filtered(order_id=order_id):
            out.append(
                {
                    "type": "RETURN",
                    "id": ret.id,
                    "reason": ret.reason,
                    "status": ret.status,
                    "requested_at": ret.requested_at,
                }
            )
        for c in self.cancellation_repo.list_filtered(order_id=order_id):
            out.append(
                {"type": "CANCEL", "id": c.id, "reason": c.reason, "status": c.status, "requested_at": c.requested_at}
            )
        return sorted(out, key=lambda x: x["requested_at"], reverse=True)
