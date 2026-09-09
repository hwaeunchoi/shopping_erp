"""
scheduler/jobs/channel_status_sync_job.py
---------------------------------------------
채널의 최신 주문상태를 읽기 전용으로 재조회해 내부 Order.status와 비교/동기화한다.

기존 scheduler.jobs.order_collect_job(운영 중인 주문 수집 흐름)은 그대로 두고
(변경하지 않기 위한 의도적 범위 제한 - services.order_channel_sync_service 모듈
docstring 참고), 이 잡은 완전히 별도로 동작한다:
- order_collect_job처럼 신규 주문을 만들거나 order_items/재고/고객을 갱신하지
  않는다 - 이미 수집된 주문의 상태값만 OrderChannelSyncService.sync_channel_status()
  를 거쳐 반영한다(허용된 전이만 자동 반영, 아니면 OrderStatusConflict로 남김).
- 채널 호출은 fetch_orders()(이미 order_collect_job이 쓰는 것과 같은 읽기 전용
  조회)를 그대로 재사용한다 - 새로 쓰기 API를 호출하지 않는다.

order_collect_job과 조회 범위가 겹쳐 채널 호출이 다소 중복되지만(운영 중인
잡을 건드리지 않기 위한 의도적 트레이드오프), 두 잡의 실행 주기를 다르게 두어
과도한 중복 호출은 피한다(scheduler.py 참고).

기본 차단: settings.channel_status_sync_enabled가 False(기본값)이면 이 잡은 아무
것도 하지 않고 즉시 반환한다(세션도 열지 않고 커넥터도 만들지 않는다 - 외부 HTTP
요청이 0건임을 보장한다). shipment_channel_submit_enabled와는 별개의 스위치다 -
실계정 검증 승인 후 운영자가 명시적으로 켜야 한다.

상용 ERP 확장(6단계): 운영 대시보드 근거로 integration_status(integration_type=
"ORDER_STATUS_SYNC")를 갱신한다 - order_collect_job과 동일한 이분법(NORMAL/
ERROR)이다(이 잡은 한 플랫폼 처리 중 예외가 나면 그 플랫폼의 나머지 주문
전체를 건너뛰므로 CS/클레임처럼 "일부만 성공"을 세분화할 근거가 없다).
"""

import logging
import uuid
from datetime import date, timedelta

from config.settings import settings
from core.database import session_scope
from integrations.malls import get_mall_connector
from integrations.malls.errors import (
    MarketplaceCapabilityUnsupportedError,
    MarketplaceCredentialMissingError,
    MarketplaceExternalAPIError,
)
from repositories.extra_repository import IntegrationStatusRepository
from repositories.order_repository import OrderRepository
from repositories.platform_repository import PlatformRepository
from services.order_channel_sync_service import OrderChannelSyncService

logger = logging.getLogger(__name__)

SYNC_WINDOW_DAYS = 3
INTEGRATION_TYPE = "ORDER_STATUS_SYNC"


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
    if not settings.channel_status_sync_enabled:
        logger.debug("채널 상태 재조회 기능이 비활성화(OFF) 상태라 channel_status_sync_job을 건너뜁니다.")
        return {"skipped_disabled": {"skipped": "disabled"}}

    results: dict[str, dict] = {}
    with session_scope() as db:
        end_date = date.today()
        start_date = end_date - timedelta(days=SYNC_WINDOW_DAYS)
        order_repo = OrderRepository(db)
        channel_sync_service = OrderChannelSyncService(db)
        integration_status_repo = IntegrationStatusRepository(db)

        for platform in PlatformRepository(db).list_active():
            applied = conflicts = matched = 0
            try:
                connector = get_mall_connector(platform.connector_class, session=db, platform_id=platform.id)
                raw_orders = connector.fetch_orders(start_date, end_date)
                for raw in raw_orders:
                    existing = order_repo.get_by_platform_order_no(platform.id, raw["platform_order_no"])
                    if existing is None:
                        continue  # 신규 주문 생성은 order_collect_job의 책임 - 여기서는 만들지 않는다.
                    matched += 1
                    result = channel_sync_service.sync_channel_status(existing, raw["status"])
                    if result.applied:
                        applied += 1
                    if result.conflict:
                        conflicts += 1
                results[platform.code] = {"matched": matched, "applied": applied, "conflicts": conflicts}
                integration_status_repo.upsert_success(INTEGRATION_TYPE, platform.code)
                db.commit()
            except MarketplaceCapabilityUnsupportedError:
                db.rollback()
                results[platform.code] = {"skipped": "unsupported"}
                logger.debug("채널 상태 재조회 스킵(미지원 채널): platform=%s", platform.code)
                continue
            except Exception as e:  # noqa: BLE001 - 채널별 격리(예상 밖 예외도 다음 채널 진행)
                db.rollback()
                summary = _safe_error_summary(e)
                results[platform.code] = {"error": summary}
                integration_status_repo.upsert_error(INTEGRATION_TYPE, platform.code, summary)
                logger.warning("채널 상태 재조회 실패: platform=%s, reason=%s", platform.code, summary)
                db.commit()
                continue
    return results
