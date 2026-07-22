"""repositories 패키지: Repository 계층. SQLite/PostgreSQL 방언 차이를 이 계층에서만 흡수한다.

Service 계층은 이 패키지의 Repository만 사용하고 SQLAlchemy Session/쿼리를
직접 다루지 않는다 (core/database.py 설계 원칙).

새 모델 그룹을 추가했다면 이 파일에도 Repository import/`__all__`을 추가할 것.

# isort: skip_file
(models/__init__.py와 동일한 ERD 도메인 순서를 따르므로 알파벳순으로
재배열되지 않도록 isort 대상에서 제외한다.)
"""

from repositories.base_repository import BaseRepository  # noqa: F401

from repositories.user_repository import PermissionRepository, RoleRepository, UserRepository  # noqa: F401
from repositories.platform_repository import PlatformFeeRuleRepository, PlatformRepository  # noqa: F401
from repositories.customer_repository import CustomerRepository  # noqa: F401
from repositories.product_repository import (  # noqa: F401
    ProductCostHistoryRepository,
    ProductOptionRepository,
    ProductPlatformMapRepository,
    ProductRepository,
)
from repositories.supplier_repository import (  # noqa: F401
    ProductSupplierMapRepository,
    SupplierContactRepository,
    SupplierRepository,
)
from repositories.purchase_order_repository import PurchaseOrderItemRepository, PurchaseOrderRepository  # noqa: F401
from repositories.inventory_repository import InventoryRepository, WarehouseRepository  # noqa: F401
from repositories.order_repository import (  # noqa: F401
    CancellationRepository,
    ExchangeRepository,
    OrderRepository,
    ReturnRepository,
    ShipmentRepository,
)
from repositories.settlement_repository import SettlementRepository  # noqa: F401
from repositories.cost_repository import CostRepository  # noqa: F401
from repositories.ad_repository import AdCampaignRepository, AdPerformanceRepository  # noqa: F401
from repositories.analytics_repository import (  # noqa: F401
    KpiTargetRepository,
    ProductPerformanceSummaryRepository,
    ProfitLossSummaryRepository,
)
from repositories.system_repository import BackupHistoryRepository, SystemLogRepository  # noqa: F401
from repositories.extra_repository import (  # noqa: F401
    FavoriteRepository,
    IntegrationStatusRepository,
    RecentViewRepository,
    ReportScheduleRepository,
    TaskExecutionHistoryRepository,
)

__all__ = [
    "BaseRepository",
    "UserRepository",
    "RoleRepository",
    "PermissionRepository",
    "PlatformRepository",
    "PlatformFeeRuleRepository",
    "CustomerRepository",
    "ProductRepository",
    "ProductOptionRepository",
    "ProductPlatformMapRepository",
    "ProductCostHistoryRepository",
    "SupplierRepository",
    "SupplierContactRepository",
    "ProductSupplierMapRepository",
    "PurchaseOrderRepository",
    "PurchaseOrderItemRepository",
    "WarehouseRepository",
    "InventoryRepository",
    "OrderRepository",
    "ShipmentRepository",
    "ExchangeRepository",
    "ReturnRepository",
    "CancellationRepository",
    "SettlementRepository",
    "CostRepository",
    "AdCampaignRepository",
    "AdPerformanceRepository",
    "ProfitLossSummaryRepository",
    "ProductPerformanceSummaryRepository",
    "KpiTargetRepository",
    "BackupHistoryRepository",
    "SystemLogRepository",
    "ReportScheduleRepository",
    "TaskExecutionHistoryRepository",
    "RecentViewRepository",
    "FavoriteRepository",
    "IntegrationStatusRepository",
]
