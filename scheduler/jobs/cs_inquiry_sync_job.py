"""
scheduler/jobs/cs_inquiry_sync_job.py
-----------------------------------------
전체 활성 쇼핑몰 플랫폼의 채널 CS(고객문의)를 자동으로 조회 동기화한다
(services.cs_channel_sync_service.CsChannelSyncService 참고 - capability
인지 + 항목별 SAVEPOINT 격리 + 외부 문의ID 기반 멱등 저장). 상용 ERP
확장(5단계, B묶음). 공식 계약이 확인된 쿠팡 콜센터 문의·상품별 문의 모두 실제로
동기화되고(CoupangConnector.supports_inquiry_sync/supports_product_inquiry_sync=True,
sync_all_inquiries()가 두 소스를 모두 순회), 나머지 채널은 두 capability가 모두
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

상용 ERP 확장(6단계): 운영 대시보드 근거로 integration_status(integration_type=
"CS_INQUIRY")를 갱신한다 - sync_all_inquiries()가 두 소스를 합산해 반환하는
status(SUCCESS/PARTIAL_SUCCESS/FAILED/UNSUPPORTED, 이 잡 안에서는 DISABLED가 나오지 않는다 -
전역 플래그가 꺼져 있으면 위에서 이미 반환했다)를 그대로 옮겨 적을 뿐이고,
CsChannelSyncService의 동기화 로직(멱등 저장/로컬 데이터 보존/SAVEPOINT
격리)은 전혀 바꾸지 않았다.
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
from services.cs_channel_sync_service import CsChannelSyncService

logger = logging.getLogger(__name__)

INTEGRATION_TYPE = "CS_INQUIRY"


def _safe_error_summary(exc: Exception) -> str:
    """개인정보·시크릿·원본 예외 문자열 없이 안전한 오류 요약을 만든다."""
    if isinstance(exc, MarketplaceExternalAPIError):
        return f"EXTERNAL_API:{exc.reason_code}:retryable={exc.retryable}"
    if isinstance(exc, MarketplaceCredentialMissingError):
        return "CREDENTIAL_MISSING"
    if isinstance(exc, MarketplaceCapabilityUnsupportedError):
        return "CAPABILITY_UNSUPPORTED"
    return f"INTERNAL_ERROR:{type(exc).__name__}:trace={uuid.uuid4().hex[:8]}"


def _collection_window(today: date) -> tuple[date, date]:
    """settings.cs_inquiry_sync_window_days(기본 1일)로 [start_date, end_date]를
    계산하는 순수 함수 - session_scope() 없이 단위테스트할 수 있도록 run()에서
    분리했다. 기존처럼 매번 7일 전체를 다시 조회하지 않고 보수적으로 줄인 값이다
    (config/settings.py의 계산식 주석 참고). window_days=1이면 start=end=today."""
    return today - timedelta(days=settings.cs_inquiry_sync_window_days - 1), today


def run() -> dict[str, dict]:
    if not settings.cs_inquiry_sync_enabled:
        logger.debug("CS 문의 동기화 기능이 비활성화(OFF) 상태라 cs_inquiry_sync_job을 건너뜁니다.")
        return {"skipped_disabled": {"skipped": "disabled"}}

    results: dict[str, dict] = {}
    with session_scope() as db:
        start_date, end_date = _collection_window(date.today())
        sync_service = CsChannelSyncService(db)
        integration_status_repo = IntegrationStatusRepository(db)

        for platform in PlatformRepository(db).list_active():
            try:
                connector = get_mall_connector(platform.connector_class, session=db, platform_id=platform.id)
                result = sync_service.sync_all_inquiries(connector, platform.id, start_date, end_date)
                results[platform.code] = result
                _record_integration_status(integration_status_repo, platform.code, result)
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
                integration_status_repo.upsert_error(INTEGRATION_TYPE, platform.code, summary)
                db.commit()
                logger.warning("CS 문의 동기화 실패: platform=%s, reason=%s", platform.code, summary)
                continue
    return results


def _record_integration_status(
    integration_status_repo: IntegrationStatusRepository, platform_code: str, result: dict
) -> None:
    """result는 CsChannelSyncService.sync_all_inquiries()의 반환값 전체다(status 하나만이
    아니라) - by_source에 담긴 안전한 reason_code(예: PAGE_LIMIT_EXCEEDED/
    REQUEST_BUDGET_EXCEEDED/CREDENTIAL_MISSING)를 메시지에 포함해, 운영자가
    integration_status 조회만으로 "무엇 때문에" 실패/부분성공했는지 확인할 수 있게
    한다(요구사항: 실패 사실과 안전한 요약을 기존 운영 관찰 경로에서 확인 가능하게
    할 것). reason_code는 이 코드베이스가 직접 정의한 짧은 안전 문자열뿐이라(모듈
    상단 _safe_error_summary와 동일 원칙) 문의 본문/주문번호/Secret이 섞여 들어올
    수 없다."""
    sync_status = result.get("status")
    by_source = result.get("by_source") or {}
    reason_codes: set[str] = {
        str(r["reason_code"]) for r in by_source.values() if isinstance(r, dict) and r.get("reason_code")
    }
    reason_suffix = f" (reason={','.join(sorted(reason_codes))})" if reason_codes else ""

    if sync_status == "SUCCESS":
        integration_status_repo.upsert_success(INTEGRATION_TYPE, platform_code)
    elif sync_status == "PARTIAL_SUCCESS":
        integration_status_repo.upsert_partial(INTEGRATION_TYPE, platform_code, f"일부 문의만 수집 성공{reason_suffix}")
    elif sync_status == "FAILED":
        integration_status_repo.upsert_error(INTEGRATION_TYPE, platform_code, f"CS 문의 수집 전체 실패{reason_suffix}")
    # UNSUPPORTED(capability 없음)는 기록하지 않는다(미지원과 실패를 구분).
