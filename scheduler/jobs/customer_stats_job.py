"""
scheduler/jobs/customer_stats_job.py
------------------------------------------
services.CustomerStatsService로 전체 고객의 캐시 통계(집계 배치)를
다시 계산한다.
"""

from core.database import session_scope
from repositories.customer_repository import CustomerRepository
from services.customer_stats_service import CustomerStatsService


def run() -> int:
    with session_scope() as db:
        service = CustomerStatsService(db)
        customer_ids = [c.id for c in CustomerRepository(db).list_all()]
        for customer_id in customer_ids:
            service.refresh(customer_id)
        return len(customer_ids)
