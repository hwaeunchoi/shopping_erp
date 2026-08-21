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

import logging
import uuid

from core.database import session_scope
from integrations.malls import get_mall_connector
from integrations.malls.errors import (
    MarketplaceCapabilityUnsupportedError,
    MarketplaceCredentialMissingError,
    MarketplaceExternalAPIError,
)
from repositories.extra_repository import IntegrationStatusRepository
from repositories.platform_repository import PlatformRepository
from services.notification_service import NotificationService
from services.product_sync_service import ProductSyncService

logger = logging.getLogger(__name__)


def _safe_error_summary(exc: Exception) -> str:
    """개인정보·시크릿·원본 예외 문자열 없이 안전한 오류 요약을 만든다."""
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
        integration_status_repo = IntegrationStatusRepository(db)
        notification_service = NotificationService(db)
        sync_service = ProductSyncService(db)

        for platform in PlatformRepository(db).list_active():
            try:
                connector = get_mall_connector(platform.connector_class, session=db, platform_id=platform.id)
                results[platform.code] = sync_service.sync_products_from_naver(connector, platform.id)
                integration_status_repo.upsert_success("MALL_PRODUCT", platform.code)
                db.commit()
            except (MarketplaceCapabilityUnsupportedError, NotImplementedError):
                # 상품 API 미지원 채널(쿠팡 등) - 실패가 아니라 스킵(DB 저장 없음, 과다 로그 방지).
                db.rollback()
                results[platform.code] = {"skipped": "unsupported"}
                logger.debug("상품 동기화 스킵(미지원 채널): platform=%s", platform.code)
                continue
            except Exception as e:  # noqa: BLE001 - 채널별 격리(예상 밖 예외도 다음 채널 진행)
                db.rollback()
                summary = _safe_error_summary(e)
                results[platform.code] = {"error": summary}
                integration_status_repo.upsert_error("MALL_PRODUCT", platform.code, summary)
                try:
                    notification_service.notify(
                        type_="API_FAILURE",
                        severity="CRITICAL",
                        message=f"상품 동기화 실패: platform={platform.code}, reason={summary}",
                    )
                except Exception:  # noqa: BLE001 - 알림 실패가 배치를 중단시키지 않게 한다.
                    logger.warning("상품 동기화 실패 알림 생성 실패: platform=%s", platform.code)
                db.commit()
                continue
    return results
