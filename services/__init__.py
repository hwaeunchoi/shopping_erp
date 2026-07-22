"""services 패키지: Service Layer (계산엔진/업무로직).

Repository만 사용하고, API 계층은 이 패키지의 Service만 사용한다
(SQLAlchemy Session은 API의 core.database.get_db 등에서 주입받아 Service
생성자에 전달한다).

시스템/부가기능 그룹(알림/감사로그/보고서예약 등)과 services/ai/(AI 분석
9종, v1.2 13장)는 아직 이 패키지의 대상이 아니다 - 실제로 이를 호출하는
API/스케줄러 작업이 생길 때 함께 구현한다.
"""

from services.customer_stats_service import CustomerStatsService  # noqa: F401
from services.inventory_service import InsufficientStockError, InventoryService  # noqa: F401
from services.order_sync_service import OrderSyncService  # noqa: F401
from services.profit_calculation_service import ProfitCalculationService  # noqa: F401

__all__ = [
    "CustomerStatsService",
    "InventoryService",
    "InsufficientStockError",
    "OrderSyncService",
    "ProfitCalculationService",
]
