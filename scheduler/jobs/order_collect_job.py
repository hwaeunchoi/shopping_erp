"""
scheduler/jobs/order_collect_job.py
---------------------------------------
전체 활성 쇼핑몰 플랫폼의 신규/변경 주문을 수집한다.

integrations.malls.get_mall_connector()로 플랫폼별 커넥터를 얻고
services.OrderSyncService에 위임한다 - 배치 작업도 Repository/커넥터를
직접 다루지 않고 Service만 호출한다. 최근 며칠을 재수집 범위로 잡아
누락되거나 상태가 바뀐 주문을 함께 보정한다.

UI v1.1 4.1절(연동상태 탭)을 위해 플랫폼별 수집 성공/실패를
integration_status에도 반영한다(integration_type="MALL"). SRS FR-MALL-07
(최종 실패 시 화면에 알림 표시)에 따라 실패 시 notifications에도 남긴다.
"""

from datetime import date, timedelta

from core.database import session_scope
from integrations.malls import get_mall_connector
from repositories.extra_repository import IntegrationStatusRepository
from repositories.inventory_repository import WarehouseRepository
from repositories.platform_repository import PlatformRepository
from services.notification_service import NotificationService
from services.order_sync_service import OrderSyncService

COLLECT_WINDOW_DAYS = 3


def run() -> dict[str, dict]:
    results: dict[str, dict] = {}
    with session_scope() as db:
        warehouses = WarehouseRepository(db).list_active()
        if not warehouses:
            raise RuntimeError("활성 창고가 없습니다. scripts/init_db.py를 먼저 실행하세요.")
        warehouse = warehouses[0]

        end_date = date.today()
        start_date = end_date - timedelta(days=COLLECT_WINDOW_DAYS)
        sync_service = OrderSyncService(db)
        integration_status_repo = IntegrationStatusRepository(db)
        notification_service = NotificationService(db)

        for platform in PlatformRepository(db).list_active():
            connector = get_mall_connector(platform.connector_class, session=db, platform_id=platform.id)
            try:
                results[platform.code] = sync_service.sync_orders(
                    connector, platform.id, warehouse.id, start_date, end_date
                )
                integration_status_repo.upsert_success("MALL", platform.code)
                db.commit()
            except Exception as e:
                integration_status_repo.upsert_error("MALL", platform.code, str(e))
                notification_service.notify(
                    type_="API_FAILURE",
                    severity="CRITICAL",
                    message=f"주문 수집 실패: platform={platform.code}, 오류={e}",
                )
                db.commit()
                raise
    return results
