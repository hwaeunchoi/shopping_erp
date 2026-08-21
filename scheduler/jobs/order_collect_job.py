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

import logging
import uuid
from datetime import date, timedelta

from core.database import session_scope
from integrations.malls import get_mall_connector
from integrations.malls.errors import (
    MarketplaceCapabilityUnsupportedError,
    MarketplaceCredentialMissingError,
    MarketplaceExternalAPIError,
)
from repositories.extra_repository import IntegrationStatusRepository
from repositories.inventory_repository import WarehouseRepository
from repositories.platform_repository import PlatformRepository
from services.notification_service import NotificationService
from services.order_sync_service import OrderSyncService

logger = logging.getLogger(__name__)

COLLECT_WINDOW_DAYS = 3


def _safe_error_summary(exc: Exception) -> str:
    """개인정보·시크릿·원본 예외 문자열을 노출하지 않는 안전한 오류 요약을 만든다.

    - 마켓 전용 오류: 분류/재시도 여부만.
    - 예상 밖 예외: 예외 클래스명 + 추적 ID(로그 상관용). str(exc)는 담지 않는다.
    """
    if isinstance(exc, MarketplaceExternalAPIError):
        return f"EXTERNAL_API:{exc.reason_code}:retryable={exc.retryable}"
    if isinstance(exc, MarketplaceCredentialMissingError):
        return "CREDENTIAL_MISSING"
    if isinstance(exc, MarketplaceCapabilityUnsupportedError):
        return "CAPABILITY_UNSUPPORTED"
    return f"INTERNAL_ERROR:{type(exc).__name__}:trace={uuid.uuid4().hex[:8]}"


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
            try:
                # 커넥터 생성도 try 안에서 - 미검증 채널은 팩토리가 미지원 오류를 던진다.
                connector = get_mall_connector(platform.connector_class, session=db, platform_id=platform.id)
                results[platform.code] = sync_service.sync_orders(
                    connector, platform.id, warehouse.id, start_date, end_date
                )
                integration_status_repo.upsert_success("MALL", platform.code)
                db.commit()
            except MarketplaceCapabilityUnsupportedError:
                # 미지원 채널 - 실패가 아니라 스킵(과다 로그 방지: debug만, DB 저장 없음).
                db.rollback()
                results[platform.code] = {"skipped": "unsupported", "created": 0, "updated": 0}
                logger.debug("주문 수집 스킵(미지원 채널): platform=%s", platform.code)
                continue
            except Exception as e:  # noqa: BLE001 - 채널별 격리(예상 밖 예외도 다음 채널 진행)
                # KeyboardInterrupt/SystemExit 등 BaseException은 잡지 않는다(정상 종료 보장).
                db.rollback()
                summary = _safe_error_summary(e)
                results[platform.code] = {"error": summary, "created": 0, "updated": 0}
                _record_channel_failure(integration_status_repo, notification_service, platform.code, summary)
                db.commit()
                continue
    return results


def _record_channel_failure(
    integration_status_repo: IntegrationStatusRepository,
    notification_service: NotificationService,
    platform_code: str,
    summary: str,
) -> None:
    """실패 채널을 구조화 기록한다. 알림 실패가 본 작업을 실패시키지 않게 감싼다.

    Secret·PII·원본 예외 문자열은 남기지 않고 안전한 요약(summary)만 사용한다."""
    integration_status_repo.upsert_error("MALL", platform_code, summary)
    try:
        notification_service.notify(
            type_="API_FAILURE",
            severity="CRITICAL",
            message=f"주문 수집 실패: platform={platform_code}, reason={summary}",
        )
    except Exception:  # noqa: BLE001 - 알림 실패가 수집 배치를 중단시키지 않게 한다.
        logger.warning("주문 수집 실패 알림 생성 실패: platform=%s", platform_code)
