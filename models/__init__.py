"""
models/__init__.py
--------------------
Alembic의 autogenerate와 Base.metadata.create_all()이 50개 테이블을
전부 인식할 수 있도록, 모든 모델 모듈을 이 파일에서 import한다.

새 모델 파일을 추가했다면 반드시 이 파일에도 import 문을 추가할 것.

# isort: skip_file
(ERD 장 순서를 따르는 도메인별 그룹 + 주석 구조를 알파벳순으로 흩어놓지
않도록 이 파일은 isort 대상에서 제외한다.)
"""

from models.base import Base  # noqa: F401

# 시스템/사용자
from models.user import User, Role, Permission, RolePermission  # noqa: F401
from models.system import (  # noqa: F401
    SystemLog,
    BackupHistory,
    SystemSetting,
    ApiCredential,
    DashboardWidget,
    AlertRule,
    Notification,
)

# 플랫폼
from models.platform import Platform, PlatformFeeRule  # noqa: F401

# 고객
from models.customer import Customer  # noqa: F401

# 상품/공급처/재고
from models.product import (  # noqa: F401
    Product,
    ProductOption,
    ProductImage,
    ProductPlatformMap,
    ProductCostHistory,
    ProductPublishDraft,
)
from models.supplier import Supplier, SupplierContact, ProductSupplierMap  # noqa: F401
from models.channel_product import ChannelProduct, ChannelProductComponent  # noqa: F401
from models.purchase_order import PurchaseOrder, PurchaseOrderItem  # noqa: F401
from models.inventory import Warehouse, Inventory, InventoryTransaction  # noqa: F401

# 주문/배송/교환/반품/취소
from models.order import (  # noqa: F401
    Order,
    OrderItem,
    OrderStatusHistory,
    Shipment,
    ShipmentItem,
    Exchange,
    Return,
    Cancellation,
    ClaimUnmatched,
    ClaimCollectionCursor,
)

# 출고(피킹/검수/포장) - 상용 ERP 확장(5단계, A묶음)
from models.fulfillment import FulfillmentBatch, FulfillmentBatchItem, FulfillmentBatchItemHistory  # noqa: F401

# CS(고객문의) 케이스 - 상용 ERP 확장(5단계, B묶음)
from models.cs_case import CsCase, CsCaseHistory  # noqa: F401

# 정산
from models.settlement import Settlement, SettlementDetail, SettlementDiscrepancy  # noqa: F401

# 비용
from models.cost import Cost  # noqa: F401

# 광고
from models.ad import AdCampaign, AdPerformanceDaily  # noqa: F401

# 매출/손익/KPI
from models.analytics import ProfitLossSummary, ProductPerformanceSummary, KpiTarget  # noqa: F401

# 채널 연동 공통 기반(outbox/충돌) - 상용 ERP 확장(commercial ERP roadmap) 1단계
from models.integration_sync import (  # noqa: F401
    ExternalCommand,
    ExternalCommandLineResult,
    OrderStatusConflict,
    ProductPublishCommandDetail,
    ProductSyncCommandDetail,
)

# 부가기능
from models.extra import (  # noqa: F401
    Memo,
    Attachment,
    RecentView,
    Favorite,
    TaskExecutionHistory,
    IntegrationStatus,
    ReportSchedule,
    ImportExportJob,
    AuditLog,
    AiAnalysisResult,
)

__all__ = [
    "Base",
    "User",
    "Role",
    "Permission",
    "RolePermission",
    "SystemLog",
    "BackupHistory",
    "SystemSetting",
    "ApiCredential",
    "DashboardWidget",
    "AlertRule",
    "Notification",
    "Platform",
    "PlatformFeeRule",
    "Customer",
    "Product",
    "ProductOption",
    "ProductImage",
    "ProductPlatformMap",
    "ProductCostHistory",
    "ProductPublishDraft",
    "Supplier",
    "SupplierContact",
    "ProductSupplierMap",
    "ChannelProduct",
    "ChannelProductComponent",
    "PurchaseOrder",
    "PurchaseOrderItem",
    "Warehouse",
    "Inventory",
    "InventoryTransaction",
    "Order",
    "OrderItem",
    "OrderStatusHistory",
    "Shipment",
    "ShipmentItem",
    "Exchange",
    "Return",
    "Cancellation",
    "ClaimUnmatched",
    "ClaimCollectionCursor",
    "Settlement",
    "SettlementDetail",
    "SettlementDiscrepancy",
    "Cost",
    "AdCampaign",
    "AdPerformanceDaily",
    "ProfitLossSummary",
    "ProductPerformanceSummary",
    "KpiTarget",
    "Memo",
    "Attachment",
    "RecentView",
    "Favorite",
    "TaskExecutionHistory",
    "IntegrationStatus",
    "ReportSchedule",
    "ImportExportJob",
    "AuditLog",
    "AiAnalysisResult",
    "ExternalCommand",
    "ExternalCommandLineResult",
    "OrderStatusConflict",
    "ProductPublishCommandDetail",
    "ProductSyncCommandDetail",
]
