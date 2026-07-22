"""
services/customer_stats_service.py
--------------------------------------
customers 캐시 컬럼(total_purchase_amount/order_count/first_order_at/
last_order_at/grade/is_vip/is_dormant) 갱신 업무로직.

models/customer.py 설계 주석: "CRM 캐시/통계 컬럼(배치가 주기적으로 갱신)",
"주문 확정/취소 시 서비스 레이어가 갱신한다"에 대응한다.
"""

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config.settings import settings
from models.customer import Customer
from models.order import Order
from repositories.customer_repository import CustomerRepository

VIP_THRESHOLD = 500000
PREMIUM_THRESHOLD = 200000


class CustomerStatsService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.customer_repo = CustomerRepository(session)

    def refresh(self, customer_id: int) -> Customer:
        """취소되지 않은 주문을 기준으로 고객 캐시 통계를 다시 계산한다."""
        customer = self.customer_repo.get_by_id(customer_id)
        if customer is None:
            raise ValueError(f"고객을 찾을 수 없습니다: {customer_id}")

        stmt = select(
            func.coalesce(func.sum(Order.total_amount), 0),
            func.count(Order.id),
            func.min(Order.order_date),
            func.max(Order.order_date),
        ).where(Order.customer_id == customer_id, Order.status != "CANCELED", Order.is_deleted.is_(False))
        total_amount, order_count, first_order_at, last_order_at = self.session.execute(stmt).one()

        customer.total_purchase_amount = round(float(total_amount), 2)
        customer.order_count = order_count
        customer.first_order_at = first_order_at
        customer.last_order_at = last_order_at
        customer.grade = self._grade_for(customer.total_purchase_amount)
        customer.is_vip = customer.grade == "VIP"
        # DB에서 읽어온 datetime은 naive이므로 naive 기준 "현재 UTC"와 비교한다.
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        customer.is_dormant = bool(last_order_at and (now_naive - last_order_at).days >= settings.dormant_customer_days)

        self.session.flush()
        return customer

    @staticmethod
    def _grade_for(total_purchase_amount: float) -> str:
        if total_purchase_amount >= VIP_THRESHOLD:
            return "VIP"
        if total_purchase_amount >= PREMIUM_THRESHOLD:
            return "우수"
        return "일반"
