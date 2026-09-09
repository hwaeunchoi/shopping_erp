"""
scheduler/jobs/settlement_sync_job.py
------------------------------------------
쇼핑몰 커넥터의 fetch_settlements()/fetch_settlement_details()로 정산 회차 요약과
주문단위 상세를 수집하고 대사한다(services.settlement_sync_service.SettlementSyncService
참고 - capability 인지 + 기능별 SAVEPOINT 격리).

기본 차단: settings.claims_settlement_sync_enabled가 False(기본값)이면 이 잡은
아무 것도 하지 않고 즉시 반환한다(세션도 열지 않고 커넥터도 만들지 않는다 - 외부
HTTP 요청이 0건임을 보장한다). 실계정 검증 승인 후 운영자가 명시적으로 켜야 한다.

상용 ERP 확장(6단계): 운영 대시보드 근거로 integration_status(integration_type=
"SETTLEMENT")를 갱신한다. sync_settlements()/sync_settlement_details() 두
호출은 각각 독립된 status를 반환하므로(SettlementSyncService에 이 둘을 합친
"overall_status"가 없다), 이 잡에서만 services.claim_sync_service.
ClaimSyncService._overall_status와 동일한 규칙(하나라도 SUCCESS+하나라도
FAILED면 PARTIAL, 전부 FAILED면 FAILED, 전부 UNSUPPORTED면 UNSUPPORTED)으로
두 status를 합친다 - SettlementSyncService 자체는 건드리지 않았다.
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
from repositories.platform_repository import PlatformRepository
from services.settlement_sync_service import SettlementSyncService

logger = logging.getLogger(__name__)

CHECK_WINDOW_DAYS = 60
INTEGRATION_TYPE = "SETTLEMENT"


def _combine_statuses(statuses: list[str]) -> str:
    if all(s == "UNSUPPORTED" for s in statuses):
        return "UNSUPPORTED"
    has_success = any(s == "SUCCESS" for s in statuses)
    has_failed = any(s == "FAILED" for s in statuses)
    if has_success and has_failed:
        return "PARTIAL"
    if has_failed:
        return "FAILED"
    return "SUCCESS"


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
        logger.debug("클레임/정산 수집 기능이 비활성화(OFF) 상태라 settlement_sync_job을 건너뜁니다.")
        return {"skipped_disabled": {"skipped": "disabled"}}

    results: dict[str, dict] = {}
    with session_scope() as db:
        end_date = date.today()
        start_date = end_date - timedelta(days=CHECK_WINDOW_DAYS)
        sync_service = SettlementSyncService(db)
        integration_status_repo = IntegrationStatusRepository(db)

        for platform in PlatformRepository(db).list_active():
            try:
                connector = get_mall_connector(platform.connector_class, session=db, platform_id=platform.id)
                settlements = sync_service.sync_settlements(connector, platform.id, start_date, end_date)
                details = sync_service.sync_settlement_details(connector, platform.id, start_date, end_date)
                results[platform.code] = {"settlements": settlements, "settlement_details": details}
                overall = _combine_statuses([settlements["status"], details["status"]])
                if overall == "SUCCESS":
                    integration_status_repo.upsert_success(INTEGRATION_TYPE, platform.code)
                elif overall == "PARTIAL":
                    integration_status_repo.upsert_partial(
                        INTEGRATION_TYPE, platform.code, "정산 회차/상세 중 일부만 수집 성공"
                    )
                elif overall == "FAILED":
                    integration_status_repo.upsert_error(INTEGRATION_TYPE, platform.code, "정산 수집 전 항목 실패")
                # UNSUPPORTED는 기록하지 않는다(미지원과 실패를 구분).
                db.commit()
            except MarketplaceCapabilityUnsupportedError:
                # get_mall_connector() 자체가 미검증 커넥터 클래스를 거부한 경우 -
                # SettlementSyncService 내부의 UNSUPPORTED 결과와 달리 커넥터 생성
                # 단계라 별도로 잡는다(product_sync_job/order_collect_job과 동일 패턴).
                db.rollback()
                results[platform.code] = {"skipped": "unsupported"}
                logger.debug("정산 동기화 스킵(미지원 채널): platform=%s", platform.code)
                continue
            except Exception as e:  # noqa: BLE001 - 채널별 격리(예상 밖 예외도 다음 채널 진행)
                db.rollback()
                summary = _safe_error_summary(e)
                results[platform.code] = {"error": summary}
                integration_status_repo.upsert_error(INTEGRATION_TYPE, platform.code, summary)
                db.commit()
                logger.warning("정산 동기화 실패: platform=%s, reason=%s", platform.code, summary)
                continue
    return results
