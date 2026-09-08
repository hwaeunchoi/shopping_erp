"""
scheduler/jobs/cs_inquiry_sync_job.py
-----------------------------------------
전체 활성 쇼핑몰 플랫폼의 채널 CS(고객문의)를 자동으로 조회 동기화한다
(services.cs_channel_sync_service.CsChannelSyncService 참고 - capability
인지 + 항목별 SAVEPOINT 격리 + 외부 문의ID 기반 멱등 저장). 상용 ERP
확장(5단계, B묶음). 공식 계약이 확인된 쿠팡 콜센터 문의만 실제로 동기화되고
(CoupangConnector.supports_inquiry_sync=True), 나머지 채널은 capability가
False라 자동으로 건너뛴다(미지원과 "결과 0건"을 구분 - results에
skipped: unsupported로 남는다).

수동 수집은 POST /api/cs-cases/sync가 담당한다 - 이 잡은 그 자동(주기) 버전이다.
둘 다 같은 CsChannelSyncService를 호출하므로 동작(멱등 저장/로컬 데이터
보존)이 동일하다.

기본 차단: settings.cs_inquiry_sync_enabled가 False(기본값)이면 이 잡은
아무 것도 하지 않고 즉시 반환한다(세션도 열지 않고 커넥터도 만들지 않는다 -
외부 HTTP 요청이 0건임을 보장한다). 실계정 검증 승인 후 운영자가 명시적으로
켜야 한다. 실제 채널 답변 전송은 이 잡에도, 어떤 코드 경로에도 없다(조회
전용 - services/cs_channel_sync_service.py 모듈 docstring 참고).
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
from services.cs_channel_sync_service import CsChannelSyncService

logger = logging.getLogger(__name__)

COLLECT_WINDOW_DAYS = 7


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
    if not settings.cs_inquiry_sync_enabled:
        logger.debug("CS 문의 동기화 기능이 비활성화(OFF) 상태라 cs_inquiry_sync_job을 건너뜁니다.")
        return {"skipped_disabled": {"skipped": "disabled"}}

    results: dict[str, dict] = {}
    with session_scope() as db:
        end_date = date.today()
        start_date = end_date - timedelta(days=COLLECT_WINDOW_DAYS)
        sync_service = CsChannelSyncService(db)

        for platform in PlatformRepository(db).list_active():
            try:
                connector = get_mall_connector(platform.connector_class, session=db, platform_id=platform.id)
                result = sync_service.sync_inquiries(connector, platform.id, start_date, end_date)
                results[platform.code] = result
                db.commit()
            except MarketplaceCapabilityUnsupportedError:
                db.rollback()
                results[platform.code] = {"skipped": "unsupported"}
                logger.debug("CS 문의 동기화 스킵(미지원 채널): platform=%s", platform.code)
                continue
            except Exception as e:  # noqa: BLE001 - 채널별 격리(예상 밖 예외도 다음 채널 진행)
                db.rollback()
                summary = _safe_error_summary(e)
                results[platform.code] = {"error": summary}
                logger.warning("CS 문의 동기화 실패: platform=%s, reason=%s", platform.code, summary)
                continue
    return results
