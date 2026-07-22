"""
scheduler/jobs/product_sync_job.py
---------------------------------------
활성 쇼핑몰 플랫폼의 상품을 상품 API로 동기화한다(네이버 스마트스토어만 실제로
지원 - 나머지 플랫폼은 fetch_products()가 NotImplementedError를 던지므로
건너뛴다). 주문 수집(order_collect_job)보다 먼저 실행되는 것이 바람직하다 -
상품이 먼저 등록/매핑돼 있어야 주문 수집 시 자동매칭이 성공할 확률이 높다.

integration_status에는 "MALL"이 아니라 "MALL_PRODUCT" 타입으로 남겨
order_collect_job이 쓰는 주문 수집 상태와 섞이지 않게 한다.
"""

from core.database import session_scope
from integrations.malls import get_mall_connector
from repositories.extra_repository import IntegrationStatusRepository
from repositories.platform_repository import PlatformRepository
from services.notification_service import NotificationService
from services.product_sync_service import ProductSyncService


def run() -> dict[str, dict]:
    results: dict[str, dict] = {}
    with session_scope() as db:
        integration_status_repo = IntegrationStatusRepository(db)
        notification_service = NotificationService(db)
        sync_service = ProductSyncService(db)

        for platform in PlatformRepository(db).list_active():
            connector = get_mall_connector(platform.connector_class, session=db, platform_id=platform.id)
            try:
                results[platform.code] = sync_service.sync_products_from_naver(connector, platform.id)
                integration_status_repo.upsert_success("MALL_PRODUCT", platform.code)
                db.commit()
            except NotImplementedError:
                # 아직 상품 API 연동을 지원하지 않는 플랫폼(더미 커넥터) - 실패로 취급하지 않는다.
                db.rollback()
                continue
            except Exception as e:
                integration_status_repo.upsert_error("MALL_PRODUCT", platform.code, str(e))
                notification_service.notify(
                    type_="API_FAILURE",
                    severity="CRITICAL",
                    message=f"상품 동기화 실패: platform={platform.code}, 오류={e}",
                )
                db.commit()
                raise
    return results
