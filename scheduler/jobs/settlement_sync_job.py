"""
scheduler/jobs/settlement_sync_job.py
------------------------------------------
쇼핑몰 커넥터의 fetch_settlements()로 정산 확인 배치를 수행한다.
(platform_id, settlement_cycle) 기준으로 있으면 갱신, 없으면 생성한다.

커넥터가 반환하는 정산 정보는 회차 단위 요약이라 settlement_details(주문
단위 상세)는 이 배치의 대상이 아니다 - 상세 매칭은 이후 정산 상세 대사
기능에서 다룬다.
"""

import logging
import uuid
from datetime import date, datetime, timedelta, timezone

from core.database import session_scope
from integrations.malls import get_mall_connector
from integrations.malls.errors import (
    MarketplaceCapabilityUnsupportedError,
    MarketplaceCredentialMissingError,
    MarketplaceExternalAPIError,
)
from models.settlement import Settlement
from repositories.platform_repository import PlatformRepository
from repositories.settlement_repository import SettlementRepository

logger = logging.getLogger(__name__)

CHECK_WINDOW_DAYS = 60


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
        settlement_repo = SettlementRepository(db)
        end_date = date.today()
        start_date = end_date - timedelta(days=CHECK_WINDOW_DAYS)

        for platform in PlatformRepository(db).list_active():
            try:
                connector = get_mall_connector(platform.connector_class, session=db, platform_id=platform.id)
                created = updated = 0
                for raw in connector.fetch_settlements(start_date, end_date):
                    existing = settlement_repo.get_by_platform_and_cycle(platform.id, raw["settlement_cycle"])
                    if existing:
                        existing.scheduled_date = raw["scheduled_date"]
                        existing.settled_date = raw["settled_date"]
                        existing.expected_amount = raw["expected_amount"]
                        existing.settled_amount = raw["settled_amount"]
                        existing.unsettled_amount = raw["expected_amount"] - raw["settled_amount"]
                        existing.status = raw["status"]
                        updated += 1
                    else:
                        settlement_repo.add(
                            Settlement(
                                platform_id=platform.id,
                                settlement_cycle=raw["settlement_cycle"],
                                scheduled_date=raw["scheduled_date"],
                                settled_date=raw["settled_date"],
                                expected_amount=raw["expected_amount"],
                                settled_amount=raw["settled_amount"],
                                unsettled_amount=raw["expected_amount"] - raw["settled_amount"],
                                discrepancy_amount=0.0,
                                status=raw["status"],
                                created_at=datetime.now(timezone.utc),
                            )
                        )
                        created += 1
                results[platform.code] = {"created": created, "updated": updated}
                db.commit()
            except (MarketplaceCapabilityUnsupportedError, NotImplementedError):
                # 정산 미지원 채널 - 실패가 아니라 스킵(DB 저장 없음, 과다 로그 방지).
                db.rollback()
                results[platform.code] = {"skipped": "unsupported"}
                logger.debug("정산 동기화 스킵(미지원 채널): platform=%s", platform.code)
                continue
            except Exception as e:  # noqa: BLE001 - 채널별 격리(예상 밖 예외도 다음 채널 진행)
                db.rollback()
                summary = _safe_error_summary(e)
                results[platform.code] = {"error": summary}
                logger.warning("정산 동기화 실패: platform=%s, reason=%s", platform.code, summary)
                continue
    return results
