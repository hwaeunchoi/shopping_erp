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

기본 차단: settings.claim_sync_enabled가 False(기본값)이면 이 잡은
아무 것도 하지 않고 즉시 반환한다(세션도 열지 않고 커넥터도 만들지 않는다 - 외부
HTTP 요청이 0건임을 보장한다). 실계정 검증 승인 후 운영자가 명시적으로 켜야 한다.

상용 ERP 확장(6단계): 운영 대시보드의 "플랫폼별 마지막 성공·실패 시각"/"클레임
수집 성공/부분성공/실패" 근거로 integration_status(integration_type="CLAIM")를
갱신한다 - sync_claims()가 이미 계산해 두던 overall_status(SUCCESS/PARTIAL/
FAILED/UNSUPPORTED)를 그대로 옮겨 적을 뿐, 이 잡의 수집 로직 자체(sync_claims/
sync_cancellations_by_candidate_orders 호출 순서·내용)는 전혀 바꾸지 않았다.
UNSUPPORTED는 기존 order_collect_job과 동일하게 기록하지 않는다(미지원과
실패를 구분 - DB에 남기지 않는다).
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
from services.claim_sync_lock import claim_sync_platform_lock
from services.claim_sync_service import ClaimSyncService, build_claim_sync_limits

INTEGRATION_TYPE = "CLAIM"

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
    if not settings.claim_sync_enabled:
        logger.debug("클레임/정산 수집 기능이 비활성화(OFF) 상태라 claim_sync_job을 건너뜁니다.")
        return {"skipped_disabled": {"skipped": "disabled"}}

    results: dict[str, dict] = {}
    with session_scope() as db:
        end_date = date.today()
        start_date = end_date - timedelta(days=max(1, settings.claim_sync_window_days))
        sync_service = ClaimSyncService(db)
        integration_status_repo = IntegrationStatusRepository(db)

        for platform in PlatformRepository(db).list_active():
            # 같은 플랫폼의 수동 sync-claims와 같은 잠금 - 겹치면 이 플랫폼은 외부 호출·DB 쓰기 없이 건너뛴다(대기·재시도 없음).
            with claim_sync_platform_lock(platform.id, db.get_bind()) as acquired:
                if not acquired:
                    results[platform.code] = {"status": "ALREADY_RUNNING", "reason_code": "ALREADY_RUNNING"}
                    logger.info("클레임 수집 건너뜀(다른 실행 중): platform=%s", platform.code)
                    continue
                try:
                    connector = get_mall_connector(platform.connector_class, session=db, platform_id=platform.id)
                    # 플랫폼 1개당 요청 예산 1개를 목록 조회(반품·교환)와 취소 단건 조회가 공유한다 -
                    # 실행 전체의 실제 HTTP 요청 수가 settings.claim_sync_max_requests_per_run을 넘지 못한다.
                    limits = build_claim_sync_limits()
                    result = sync_service.sync_claims(connector, platform.id, start_date, end_date, limits)
                    # "후보 주문 단건 조회" 방식 취소 수집(기간 대량조회가 안 되는 채널 전용,
                    # 예: 쿠팡) - sync_claims()와 별개 capability라 결과도 별도 키로 담는다.
                    result["cancellations_by_order_lookup"] = sync_service.sync_cancellations_by_candidate_orders(
                        connector, platform.id, settings.claim_cancel_detail_max_orders_per_run, limits
                    )
                    result["requests_used"] = limits.budget.used
                    logger.info(
                        "클레임 수집 요청 사용량: platform=%s, used=%s/%s",
                        platform.code,
                        limits.budget.used,
                        limits.budget.max_requests,
                    )
                    results[platform.code] = result
                    _record_integration_status(integration_status_repo, platform.code, result.get("overall_status"))
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
                    integration_status_repo.upsert_error(INTEGRATION_TYPE, platform.code, summary)
                    db.commit()
                    logger.warning("클레임 수집 실패: platform=%s, reason=%s", platform.code, summary)
                    continue
    return results


def _record_integration_status(
    integration_status_repo: IntegrationStatusRepository, platform_code: str, overall_status: object
) -> None:
    if overall_status == "SUCCESS":
        integration_status_repo.upsert_success(INTEGRATION_TYPE, platform_code)
    elif overall_status == "PARTIAL":
        integration_status_repo.upsert_partial(INTEGRATION_TYPE, platform_code, "일부 클레임 유형만 수집 성공")
    elif overall_status == "FAILED":
        integration_status_repo.upsert_error(INTEGRATION_TYPE, platform_code, "클레임 수집 전 항목 실패")
    # UNSUPPORTED는 기록하지 않는다(미지원과 실패를 구분 - order_collect_job과 동일 관례).
