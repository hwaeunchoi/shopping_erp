"""
services/claim_sync_service.py
------------------------------------
교환/반품/취소(클레임) 수집 - capability 인지 + 기능별 격리.

커넥터의 supports_cancellation_sync/supports_return_sync/supports_exchange_sync가
True인 기능만 fetch_*를 호출해, 이미 수집된 주문(platform_order_no로 매칭)에 연결해
ERP의 Cancellation/Return/Exchange 레코드로 미러링한다.

설계 원칙
  - **capability 인지**: 지원(True)하는 기능만 호출한다. 미지원(False)은 호출하지 않고
    결과에서 UNSUPPORTED로 구분한다("결과 0건"과 혼동하지 않는다). 지원+0건은 SUCCESS.
  - **기능별 격리**: 취소/반품/교환을 각각 SAVEPOINT(begin_nested) 안에서 처리하고
    기능 범위에서 flush()한다. 한 기능의 fetch 실패·DB 오류는 그 기능만 롤백하고
    앞서 성공한 기능 데이터는 유지한다. 다음 기능은 계속 진행한다.
  - **수집(미러링) 전용**: 플랫폼 클레임 상태를 그대로 레코드로 만든다. 재고/주문상태
    변경 같은 부수효과는 일으키지 않는다.
  - **안전 로그**: Secret·개인정보·원본 응답·내부 예외 전문을 로그/응답에 남기지 않는다.

⚠️ 현재 모델 한계(실제 채널 capability를 True로 켜기 전에 모델 확장 TODO 필요):
  - 플랫폼 claim 고유 ID 미저장 → 중복방지는 "주문+유형" 단위(동일 주문의 동일 유형
    복수 클레임 구분 불가, 재수집 시 상태 업데이트 불가 - 존재하면 skip).
  - 원본 상태 코드·원본 응답·귀책 주체·배송비·수거정보 미저장.
  현재 네이버·쿠팡은 세 capability 모두 False라 실 클레임이 이 구조로 자동 수집되지 않는다.
"""

import logging
import uuid
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from integrations.malls.errors import (
    MarketplaceCapabilityUnsupportedError,
    MarketplaceCredentialMissingError,
    MarketplaceExternalAPIError,
)
from models.order import Cancellation, Exchange, Return
from repositories.order_repository import CancellationRepository, ExchangeRepository, OrderRepository, ReturnRepository

logger = logging.getLogger(__name__)

# 각 클레임 유형에서 "완료(종결)"로 보는 상태 - completed_at을 채운다.
_TERMINAL_STATUSES = {"COMPLETED", "REFUNDED", "REJECTED"}


class ClaimSyncService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.order_repo = OrderRepository(session)
        self.exchange_repo = ExchangeRepository(session)
        self.return_repo = ReturnRepository(session)
        self.cancellation_repo = CancellationRepository(session)

    def sync_claims(self, connector: Any, platform_id: int, start_date: date, end_date: date) -> dict[str, Any]:
        """취소/반품/교환을 capability 기준으로 미러링하고 구조화된 기능별 결과를 반환한다.

        반환:
            {"overall_status": str,
             "cancellations": {...}, "returns": {...}, "exchanges": {...},
             "skipped_no_order": int}
        각 기능 결과: {"status", "count", "reason_code", "retryable"}.
        """
        skipped = {"count": 0}

        cancellations = self._sync_feature(
            supported=getattr(connector, "supports_cancellation_sync", False),
            fetch=lambda: connector.fetch_cancellations(start_date, end_date),
            persist=lambda raw: self._persist_cancellation(platform_id, raw, skipped),
            feature="cancellations",
        )
        returns = self._sync_feature(
            supported=getattr(connector, "supports_return_sync", False),
            fetch=lambda: connector.fetch_returns(start_date, end_date),
            persist=lambda raw: self._persist_return(platform_id, raw, skipped),
            feature="returns",
        )
        exchanges = self._sync_feature(
            supported=getattr(connector, "supports_exchange_sync", False),
            fetch=lambda: connector.fetch_exchanges(start_date, end_date),
            persist=lambda raw: self._persist_exchange(platform_id, raw, skipped),
            feature="exchanges",
        )

        features = {"cancellations": cancellations, "returns": returns, "exchanges": exchanges}
        return {"overall_status": self._overall_status(features), **features, "skipped_no_order": skipped["count"]}

    def _sync_feature(
        self, supported: bool, fetch: Callable[[], list], persist: Callable[[dict], bool], feature: str
    ) -> dict[str, Any]:
        """한 클레임 기능을 SAVEPOINT 안에서 격리 처리한다. 실패는 그 기능만 롤백한다."""
        if not supported:
            # capability False - fetch 호출 자체를 하지 않는다(미지원과 0건 구분).
            return _feature_result("UNSUPPORTED", 0, "CAPABILITY_UNSUPPORTED", False)

        savepoint = self.session.begin_nested()
        try:
            count = 0
            for raw in fetch():
                if persist(raw):
                    count += 1
            # DB 제약 오류를 이 기능 범위(SAVEPOINT)에서 잡기 위해 여기서 flush 한다.
            self.session.flush()
            savepoint.commit()
            return _feature_result("SUCCESS", count, None, None)
        except MarketplaceCredentialMissingError:
            savepoint.rollback()
            return _feature_result("FAILED", 0, "CREDENTIAL_MISSING", False)
        except MarketplaceExternalAPIError as e:
            savepoint.rollback()
            return _feature_result("FAILED", 0, e.reason_code, bool(e.retryable))
        except MarketplaceCapabilityUnsupportedError:
            # capability=True인데 커넥터가 미지원 오류를 던지는 비정상 상태 - 미지원으로 본다.
            savepoint.rollback()
            return _feature_result("UNSUPPORTED", 0, "CAPABILITY_UNSUPPORTED", False)
        except SQLAlchemyError:
            savepoint.rollback()
            logger.warning("클레임 DB 반영 실패: feature=%s, trace=%s", feature, uuid.uuid4().hex[:8])
            return _feature_result("FAILED", 0, "DB_WRITE_FAILED", False)
        except Exception:  # noqa: BLE001 - 예상 밖 예외도 기능별 격리(내부 전문 미노출)
            savepoint.rollback()
            logger.warning("클레임 처리 예상 밖 오류: feature=%s, trace=%s", feature, uuid.uuid4().hex[:8])
            return _feature_result("FAILED", 0, "INTERNAL_ERROR", False)

    # --- 기능별 저장(중복방지 포함). 생성하면 True, skip이면 False ---

    def _persist_cancellation(self, platform_id: int, raw: dict, skipped: dict) -> bool:
        order = self._find_order(platform_id, raw, skipped)
        if order is None:
            return False
        if self.cancellation_repo.count_filtered(order_id=order.id) > 0:
            return False  # 중복방지(주문+유형)
        status = raw.get("status") or "REQUESTED"
        requested_at = _as_dt(raw.get("requested_at"))
        self.cancellation_repo.add(
            Cancellation(
                order_id=order.id,
                reason=_clip(raw.get("reason"), 200),
                refund_amount=raw.get("refund_amount"),
                status=status,
                requested_at=requested_at,
                completed_at=requested_at if status in _TERMINAL_STATUSES else None,
            )
        )
        return True

    def _persist_return(self, platform_id: int, raw: dict, skipped: dict) -> bool:
        order = self._find_order(platform_id, raw, skipped)
        if order is None:
            return False
        if self.return_repo.count_filtered(order_id=order.id) > 0:
            return False
        status = raw.get("status") or "REQUESTED"
        requested_at = _as_dt(raw.get("requested_at"))
        self.return_repo.add(
            Return(
                order_id=order.id,
                order_item_id=None,
                reason=_clip(raw.get("reason"), 200),
                refund_amount=raw.get("refund_amount"),
                status=status,
                requested_at=requested_at,
                completed_at=requested_at if status in _TERMINAL_STATUSES else None,
            )
        )
        return True

    def _persist_exchange(self, platform_id: int, raw: dict, skipped: dict) -> bool:
        order = self._find_order(platform_id, raw, skipped)
        if order is None:
            return False
        if self.exchange_repo.count_filtered(order_id=order.id) > 0:
            return False
        status = raw.get("status") or "REQUESTED"
        requested_at = _as_dt(raw.get("requested_at"))
        self.exchange_repo.add(
            Exchange(
                order_id=order.id,
                order_item_id=None,
                reason=_clip(raw.get("reason"), 200),
                status=status,
                requested_at=requested_at,
                completed_at=requested_at if status in _TERMINAL_STATUSES else None,
            )
        )
        return True

    def _find_order(self, platform_id: int, raw: dict[str, Any], skipped: dict):
        order_no = raw.get("platform_order_no")
        if not order_no:
            skipped["count"] += 1
            return None
        order = self.order_repo.get_by_platform_order_no(platform_id, order_no)
        if order is None:
            skipped["count"] += 1
        return order

    @staticmethod
    def _overall_status(features: dict[str, dict]) -> str:
        statuses = [f["status"] for f in features.values()]
        if all(s == "UNSUPPORTED" for s in statuses):
            return "UNSUPPORTED"
        has_success = any(s == "SUCCESS" for s in statuses)
        has_failed = any(s == "FAILED" for s in statuses)
        if has_success and has_failed:
            return "PARTIAL"
        if has_success:  # SUCCESS (+ UNSUPPORTED) 만 존재
            return "SUCCESS"
        return "FAILED"  # SUCCESS 없음 + FAILED 존재(UNSUPPORTED 동반 가능)


def _feature_result(status: str, count: int, reason_code: Optional[str], retryable: Optional[bool]) -> dict[str, Any]:
    return {"status": status, "count": count, "reason_code": reason_code, "retryable": retryable}


def _as_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _clip(value: Any, length: int):
    if value is None:
        return None
    text = str(value)
    return text[:length] if len(text) > length else text
