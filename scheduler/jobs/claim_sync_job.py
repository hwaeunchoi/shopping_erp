"""
scheduler/jobs/claim_sync_job.py
---------------------------------------
전체 활성 쇼핑몰 플랫폼의 취소/반품/교환(클레임)을 자동으로 수집한다
(services.claim_sync_service.ClaimSyncService 참고 - capability 인지 + 기능별
SAVEPOINT 격리 + 클레임 ID 기반 재수집/미매칭 보존). 상용 ERP 확장(2단계).

수동 수집은 기존 POST /api/orders/sync-claims가 담당한다 - 이 잡은 그 자동(주기)
버전이다. 둘 다 같은 ClaimSyncService를 호출하므로 동작(중복방지/상태갱신/미매칭
보존)이 동일하다.

상용 ERP 확장(2단계-A 보완): 기간만으로 대량조회가 안 되는 채널(쿠팡 - 취소는
orderId 단건 조회만 공식 지원)을 위해 sync_claims() 뒤에
sync_cancellations_by_candidate_orders()도 호출한다 - 별개 capability
(supports_cancellation_lookup_by_order)이며 결과는
results[code]["cancellations_by_order_lookup"]에 따로 담긴다. 이 경로는 자체
회전식 체크포인트로 매 실행 요청 수를 제한한다(ClaimSyncService 참고).

기본 차단: settings.claims_settlement_sync_enabled가 False(기본값)이면 이 잡은
아무 것도 하지 않고 즉시 반환한다(세션도 열지 않고 커넥터도 만들지 않는다 - 외부
HTTP 요청이 0건임을 보장한다). 실계정 검증 승인 후 운영자가 명시적으로 켜야 한다.
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
from repositories.platform_repository import PlatformRepository
from services.claim_sync_service import ClaimSyncService

logger = logging.getLogger(__name__)

COLLECT_WINDOW_DAYS = 3


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
    if not settings.claims_settlement_sync_enabled:
        logger.debug("클레임/정산 수집 기능이 비활성화(OFF) 상태라 claim_sync_job을 건너뜁니다.")
        return {"skipped_disabled": {"skipped": "disabled"}}

    results: dict[str, dict] = {}
    with session_scope() as db:
        end_date = date.today()
        start_date = end_date - timedelta(days=COLLECT_WINDOW_DAYS)
        sync_service = ClaimSyncService(db)

        for platform in PlatformRepository(db).list_active():
            try:
                connector = get_mall_connector(platform.connector_class, session=db, platform_id=platform.id)
                result = sync_service.sync_claims(connector, platform.id, start_date, end_date)
                # "후보 주문 단건 조회" 방식 취소 수집(기간 대량조회가 안 되는 채널 전용,
                # 예: 쿠팡) - sync_claims()와 별개 capability라 결과도 별도 키로 담는다.
                result["cancellations_by_order_lookup"] = sync_service.sync_cancellations_by_candidate_orders(
                    connector, platform.id
                )
                results[platform.code] = result
                db.commit()
            except MarketplaceCapabilityUnsupportedError:
                db.rollback()
                results[platform.code] = {"skipped": "unsupported"}
                logger.debug("클레임 수집 스킵(미지원 채널): platform=%s", platform.code)
                continue
            except Exception as e:  # noqa: BLE001 - 채널별 격리(예상 밖 예외도 다음 채널 진행)
                db.rollback()
                summary = _safe_error_summary(e)
                results[platform.code] = {"error": summary}
                logger.warning("클레임 수집 실패: platform=%s, reason=%s", platform.code, summary)
                continue
    return results
