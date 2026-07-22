"""
scheduler/jobs/profit_calculation_job.py
----------------------------------------------
services.ProfitCalculationService로 최근 며칠치 일별 매출/손익 요약을
다시 계산한다(집계 배치). 최근 N일을 매번 재계산하여, 뒤늦게 반영된
주문/비용/광고 데이터가 있어도 요약이 최신 상태로 보정되게 한다.
"""

from datetime import date, timedelta

from core.database import session_scope
from services.profit_calculation_service import ProfitCalculationService

RECALCULATE_WINDOW_DAYS = 3


def run() -> list[str]:
    with session_scope() as db:
        service = ProfitCalculationService(db)
        # calculate_daily_range는 [start_date, end_date) 반개구간이므로 오늘도 포함되게 +1일.
        end_date = date.today() + timedelta(days=1)
        start_date = date.today() - timedelta(days=RECALCULATE_WINDOW_DAYS)
        summaries = service.calculate_daily_range(start_date, end_date)
        return [s.period_key for s in summaries]
