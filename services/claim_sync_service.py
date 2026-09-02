"""
services/claim_sync_service.py
------------------------------------
교환/반품/취소(클레임) 수집 - capability 인지 + 기능별 격리 + 클레임 ID 기반
재수집(2단계 확장).

커넥터의 supports_cancellation_sync/supports_return_sync/supports_exchange_sync가
True인 기능만 fetch_*를 호출해, 이미 수집된 주문(platform_order_no로 매칭)에 연결해
ERP의 Cancellation/Return/Exchange 레코드로 미러링한다.

설계 원칙
  - **capability 인지**: 지원(True)하는 기능만 호출한다. 미지원(False)은 호출하지 않고
    결과에서 UNSUPPORTED로 구분한다("결과 0건"과 혼동하지 않는다). 지원+0건은 SUCCESS.
  - **기능별 격리**: 취소/반품/교환을 각각 SAVEPOINT(begin_nested) 안에서 처리하고
    기능 범위에서 flush()한다. 한 기능의 fetch 실패·DB 오류는 그 기능만 롤백하고
    앞서 성공한 기능 데이터는 유지한다. 다음 기능은 계속 진행한다.
  - **클레임 ID 기반 중복방지/갱신(2단계)**: raw["platform_claim_id"]가 있으면 (order_id,
    platform_claim_id) 단위로 식별한다 - 같은 주문에 같은 유형의 클레임이 여러 건이어도
    (부분 클레임) 각각 별도 행으로 만들고, 재수집 시에는 새로 만들지 않고 기존 행을
    갱신한다. 커넥터가 platform_claim_id를 주지 않는 채널(아직 계약을 확인하지 못한
    경우)은 임의로 키를 지어내지 않고, 기존의 보수적 방식("주문+유형" 단위, 이미
    있으면 skip)으로 폴백한다.
  - **상태 후퇴 방지(2단계)**: 갱신 시 services.claim_state_machine.should_apply_claim_status()
    로 오래된 응답이 이미 반영된 최신 상태를 되돌리지 못하게 막는다. 원본 상태가
    알려진 매핑에 없으면(커넥터가 이미 raw_status를 "REVIEW"로 정규화해 보냄) 완료 등으로
    추정하지 않는다.
  - **미매칭 주문 보존(2단계)**: 아직 수집되지 않은 주문(platform_order_no 매칭 실패)의
    클레임은 조용히 버리지 않고 ClaimUnmatched에 보존한다. 매 sync_claims() 호출 시작
    시, 이전에 보존해 둔 미매칭 클레임 중 이제 주문이 수집된 것이 있으면 먼저 실제
    클레임 행으로 승격한다(_resolve_pending_unmatched).
  - **수집(미러링) 전용**: 플랫폼 클레임 상태를 그대로 레코드로 만든다. 재고/주문상태
    변경 같은 부수효과는 일으키지 않는다.
  - **안전 로그**: Secret·개인정보·원본 응답·내부 예외 전문을 로그/응답에 남기지 않는다.
    원본 응답 전체는 저장하지 않는다(보존정책 승인 전) - 필요한 필드만 모델 컬럼에 담는다.
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
from models.order import Cancellation, ClaimUnmatched, Exchange, Return
from repositories.order_repository import (
    CancellationRepository,
    ClaimCollectionCursorRepository,
    ClaimUnmatchedRepository,
    ExchangeRepository,
    OrderRepository,
    ReturnRepository,
)
from services.claim_state_machine import (
    CANCELLATION_TRANSITIONS,
    EXCHANGE_TRANSITIONS,
    RETURN_TRANSITIONS,
    should_apply_claim_status,
)

logger = logging.getLogger(__name__)

# 각 클레임 유형에서 "완료(종결)"로 보는 상태 - completed_at을 채운다.
_TERMINAL_STATUSES = {"COMPLETED", "REFUNDED", "REJECTED"}

# "후보 주문 단건 조회" 방식 취소 수집(예: 쿠팡)의 회전식 체크포인트 claim_type 값
# (models.order.ClaimCollectionCursor - 실제 Cancellation.claim_type이 아니라
# 진행 위치 구분용 키다).
CANCELLATION_ORDER_LOOKUP_CURSOR_TYPE = "CANCELLATION_ORDER_LOOKUP"
# 실행 1회당 최대 조회 요청 수 - 전체 주문을 무제한 순회하지 않기 위한 예산.
DEFAULT_CANCELLATION_LOOKUP_BATCH = 50


class ClaimSyncService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.order_repo = OrderRepository(session)
        self.exchange_repo = ExchangeRepository(session)
        self.return_repo = ReturnRepository(session)
        self.cancellation_repo = CancellationRepository(session)
        self.unmatched_repo = ClaimUnmatchedRepository(session)
        self.cursor_repo = ClaimCollectionCursorRepository(session)

    def sync_claims(self, connector: Any, platform_id: int, start_date: date, end_date: date) -> dict[str, Any]:
        """취소/반품/교환을 capability 기준으로 미러링하고 구조화된 기능별 결과를 반환한다.

        반환:
            {"overall_status": str,
             "cancellations": {...}, "returns": {...}, "exchanges": {...},
             "skipped_no_order": int, "resolved_unmatched": int}
        각 기능 결과: {"status", "count", "reason_code", "retryable"}.
        """
        resolved_unmatched = self._resolve_pending_unmatched(platform_id)
        skipped = {"count": 0}

        cancellations = self._sync_feature(
            supported=getattr(connector, "supports_cancellation_sync", False),
            fetch=lambda: connector.fetch_cancellations(start_date, end_date),
            persist=lambda raw: self._persist_claim(
                "CANCELLATION", platform_id, raw, skipped, self.cancellation_repo, CANCELLATION_TRANSITIONS
            ),
            feature="cancellations",
        )
        returns = self._sync_feature(
            supported=getattr(connector, "supports_return_sync", False),
            fetch=lambda: connector.fetch_returns(start_date, end_date),
            persist=lambda raw: self._persist_claim(
                "RETURN", platform_id, raw, skipped, self.return_repo, RETURN_TRANSITIONS
            ),
            feature="returns",
        )
        exchanges = self._sync_feature(
            supported=getattr(connector, "supports_exchange_sync", False),
            fetch=lambda: connector.fetch_exchanges(start_date, end_date),
            persist=lambda raw: self._persist_claim(
                "EXCHANGE", platform_id, raw, skipped, self.exchange_repo, EXCHANGE_TRANSITIONS
            ),
            feature="exchanges",
        )

        features = {"cancellations": cancellations, "returns": returns, "exchanges": exchanges}
        return {
            "overall_status": self._overall_status(features),
            **features,
            "skipped_no_order": skipped["count"],
            "resolved_unmatched": resolved_unmatched,
        }

    def sync_cancellations_by_candidate_orders(
        self, connector: Any, platform_id: int, max_requests: int = DEFAULT_CANCELLATION_LOOKUP_BATCH
    ) -> dict[str, Any]:
        """ "후보 주문 단건 조회" 방식의 취소 수집(상용 ERP 확장 2단계-A 보완) -
        기간만으로 대량조회가 안 되는 채널(쿠팡 등, supports_cancellation_lookup_by_order
        참고) 전용. sync_claims()의 기간 기반 대량조회(supports_cancellation_sync)와는
        별개 경로이며, 같은 sync_claims() 호출 안에 넣지 않고 별도로 호출한다(스케줄러가
        둘 다 호출 - scheduler/jobs/claim_sync_job.py 참고).

        회전식 체크포인트(ClaimCollectionCursor)로 Order.id 오름차순 최대 max_requests건만
        조회해(전체 무제한 순회 금지) 매 실행 요청 수를 예산 내로 제한한다. 배송 전
        상태(NEW/PREPARING/SHIPPING)의 주문만 후보로 삼는다(repositories.order_repository.
        CANCELLATION_LOOKUP_CANDIDATE_STATUSES 참고).

        ⚠️ 알려진 한계: ERP에 아직 수집되지 않은 주문(order_collect_job이 아직 못 가져온
        주문)의 취소는 이 방식으로 알 수 없다 - 후보 목록 자체가 이미 수집된 Order
        테이블에서 나오기 때문이다. 그 주문이 나중에 수집되면 다음 회전에서부터 후보에
        포함된다(늦게라도 감지는 되지만 즉시는 아니다).

        한 후보 주문의 외부 API 호출 실패(MarketplaceExternalAPIError)는 그 주문만
        건너뛰고 다음 후보로 계속 진행한다(요청 자체가 주문마다 독립적인 개별 호출이라
        전체 기능 롤백과는 격리 원칙을 다르게 적용 - 인증정보 누락처럼 이후 모든 호출이
        똑같이 실패할 오류는 예외로 전파해 배치를 즉시 중단한다)."""
        if not getattr(connector, "supports_cancellation_lookup_by_order", False):
            return _feature_result("UNSUPPORTED", 0, "CAPABILITY_UNSUPPORTED", False)

        savepoint = self.session.begin_nested()
        try:
            cursor = self.cursor_repo.get_or_create(platform_id, CANCELLATION_ORDER_LOOKUP_CURSOR_TYPE)
            candidates = self.order_repo.list_cancellation_lookup_candidates(
                platform_id, cursor.last_order_id, max_requests
            )
            if len(candidates) < max_requests:
                seen_ids = {o.id for o in candidates}
                wrapped = self.order_repo.list_cancellation_lookup_candidates(
                    platform_id, 0, max_requests - len(candidates)
                )
                candidates += [o for o in wrapped if o.id not in seen_ids]

            skipped = {"count": 0}
            count = 0
            last_id = cursor.last_order_id
            for order in candidates:
                last_id = order.id
                try:
                    raw = connector.fetch_cancellation_status(order.platform_order_no, order.order_date.date())
                except MarketplaceExternalAPIError as e:
                    logger.warning(
                        "취소 후보 주문 조회 실패(다음 주문 계속): platform_id=%s, reason=%s",
                        platform_id,
                        e.reason_code,
                    )
                    continue
                if raw is None:
                    continue
                if self._persist_claim(
                    "CANCELLATION", platform_id, raw, skipped, self.cancellation_repo, CANCELLATION_TRANSITIONS
                ):
                    count += 1

            if candidates:
                cursor.last_order_id = last_id
            cursor.updated_at = datetime.now(timezone.utc)
            self.session.flush()
            savepoint.commit()
            return _feature_result("SUCCESS", count, None, None)
        except MarketplaceCredentialMissingError:
            savepoint.rollback()
            return _feature_result("FAILED", 0, "CREDENTIAL_MISSING", False)
        except MarketplaceCapabilityUnsupportedError:
            savepoint.rollback()
            return _feature_result("UNSUPPORTED", 0, "CAPABILITY_UNSUPPORTED", False)
        except SQLAlchemyError:
            savepoint.rollback()
            logger.warning("취소 후보조회 DB 반영 실패: trace=%s", uuid.uuid4().hex[:8])
            return _feature_result("FAILED", 0, "DB_WRITE_FAILED", False)
        except Exception:  # noqa: BLE001 - 예상 밖 예외도 이 기능만 격리(내부 전문 미노출)
            savepoint.rollback()
            logger.warning("취소 후보조회 예상 밖 오류: trace=%s", uuid.uuid4().hex[:8])
            return _feature_result("FAILED", 0, "INTERNAL_ERROR", False)

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

    # --- 클레임 ID 기반 저장(생성/갱신) ---

    def _persist_claim(
        self,
        claim_type: str,
        platform_id: int,
        raw: dict[str, Any],
        skipped: dict,
        repo: Any,
        transitions: dict[str, frozenset[str]],
    ) -> bool:
        """생성/갱신했으면 True, 건너뛰었으면(주문 미매칭 - ClaimUnmatched에 보존,
        또는 오래된 응답이라 무시) False."""
        order_no = raw.get("platform_order_no")
        if not order_no:
            skipped["count"] += 1
            return False
        order = self.order_repo.get_by_platform_order_no(platform_id, order_no)
        if order is None:
            self._hold_unmatched(claim_type, platform_id, raw, order_no)
            skipped["count"] += 1
            return False

        platform_claim_id = raw.get("platform_claim_id")
        status = raw.get("status") or "REVIEW"
        raw_status = raw.get("raw_status")
        requested_at = _as_dt(raw.get("requested_at"))
        order_item_id = self._resolve_order_item_id(order.id, raw.get("platform_order_item_no"))

        existing = None
        if platform_claim_id:
            existing = repo.get_by_order_and_claim_id(order.id, platform_claim_id)
        else:
            # 공식 claim ID를 확인하지 못한 채널 - 임의 키를 지어내지 않고, "주문+유형"
            # 단위의 보수적 중복방지로 폴백한다(이미 있으면 새로 만들지 않는다).
            if repo.count_filtered(order_id=order.id) > 0:
                return False

        if existing is not None:
            if not should_apply_claim_status(existing.status, status, transitions):
                # 오래된 응답이거나 이미 같은 상태 - 무시한다(되돌리지 않음).
                return False
            existing.status = status
            existing.raw_status = raw_status
            if status in _TERMINAL_STATUSES and existing.completed_at is None:
                existing.completed_at = requested_at
            if raw.get("quantity") is not None:
                existing.quantity = raw.get("quantity")
            if raw.get("shipping_fee") is not None:
                existing.shipping_fee = raw.get("shipping_fee")
            if hasattr(existing, "fault_type") and raw.get("fault_type") is not None:
                existing.fault_type = raw.get("fault_type")
            if raw.get("refund_amount") is not None and hasattr(existing, "refund_amount"):
                existing.refund_amount = raw.get("refund_amount")
            if order_item_id is not None and existing.order_item_id is None:
                existing.order_item_id = order_item_id
            return True

        kwargs: dict[str, Any] = {
            "order_id": order.id,
            "order_item_id": order_item_id,
            "reason": _clip(raw.get("reason"), 200),
            "status": status,
            "requested_at": requested_at,
            "completed_at": requested_at if status in _TERMINAL_STATUSES else None,
            "platform_claim_id": platform_claim_id,
            "raw_status": raw_status,
            "quantity": raw.get("quantity"),
            "shipping_fee": raw.get("shipping_fee"),
            "fault_type": raw.get("fault_type"),
        }
        if claim_type in ("RETURN", "CANCELLATION"):
            kwargs["refund_amount"] = raw.get("refund_amount")
        model_cls = {"CANCELLATION": Cancellation, "RETURN": Return, "EXCHANGE": Exchange}[claim_type]
        repo.add(model_cls(**kwargs))
        return True

    def _resolve_order_item_id(self, order_id: int, platform_order_item_no: Optional[str]) -> Optional[int]:
        if not platform_order_item_no:
            return None
        item = self.order_repo.get_item_by_platform_order_item_no(order_id, platform_order_item_no)
        return item.id if item is not None else None

    def _hold_unmatched(self, claim_type: str, platform_id: int, raw: dict[str, Any], order_no: str) -> None:
        """주문이 아직 수집되지 않은 클레임을 조용히 버리지 않고 보존한다."""
        platform_claim_id = raw.get("platform_claim_id")
        existing = self.unmatched_repo.get_by_key(platform_id, claim_type, platform_claim_id, order_no)
        if existing is not None:
            existing.raw_status = raw.get("raw_status")
            return
        self.unmatched_repo.add(
            ClaimUnmatched(
                platform_id=platform_id,
                claim_type=claim_type,
                platform_claim_id=platform_claim_id,
                platform_order_no=order_no,
                raw_status=raw.get("raw_status"),
                reason=_clip(raw.get("reason"), 200),
                detected_at=datetime.now(timezone.utc),
            )
        )

    def _resolve_pending_unmatched(self, platform_id: int) -> int:
        """이전에 보존해 둔 미매칭 클레임 중 이제 주문이 수집된 것을 실제 클레임
        행으로 승격한다. 각 항목은 독립적으로 처리한다(하나가 실패해도 나머지는 계속)."""
        resolved = 0
        for pending in self.unmatched_repo.list_unresolved(platform_id):
            order = self.order_repo.get_by_platform_order_no(platform_id, pending.platform_order_no)
            if order is None:
                continue
            entity_id = self._promote_unmatched(pending, order.id)
            if entity_id is None:
                continue
            pending.resolved_entity_id = entity_id
            pending.resolved_at = datetime.now(timezone.utc)
            resolved += 1
        if resolved:
            self.session.flush()
        return resolved

    def _promote_unmatched(self, pending: ClaimUnmatched, order_id: int) -> Optional[int]:
        """보존 당시 저장해 둔 최소 필드만으로 승격한다(수량/환불액/배송비/라인 연결
        등은 여기 없다 - 다음 정기 재수집에서 같은 platform_claim_id로 _persist_claim()
        이 이 행을 정상적으로 갱신하며 채운다)."""
        if pending.claim_type == "CANCELLATION":
            existing_c = self.cancellation_repo.get_by_order_and_claim_id(order_id, pending.platform_claim_id or "")
            if existing_c is not None:
                return existing_c.id
            created_c = self.cancellation_repo.add(
                Cancellation(
                    order_id=order_id,
                    reason=pending.reason,
                    status="REVIEW",
                    requested_at=pending.detected_at,
                    platform_claim_id=pending.platform_claim_id,
                    raw_status=pending.raw_status,
                )
            )
            return created_c.id
        if pending.claim_type == "RETURN":
            existing_r = self.return_repo.get_by_order_and_claim_id(order_id, pending.platform_claim_id or "")
            if existing_r is not None:
                return existing_r.id
            created_r = self.return_repo.add(
                Return(
                    order_id=order_id,
                    reason=pending.reason,
                    status="REVIEW",
                    requested_at=pending.detected_at,
                    platform_claim_id=pending.platform_claim_id,
                    raw_status=pending.raw_status,
                )
            )
            return created_r.id
        if pending.claim_type == "EXCHANGE":
            existing_e = self.exchange_repo.get_by_order_and_claim_id(order_id, pending.platform_claim_id or "")
            if existing_e is not None:
                return existing_e.id
            created_e = self.exchange_repo.add(
                Exchange(
                    order_id=order_id,
                    reason=pending.reason,
                    status="REVIEW",
                    requested_at=pending.detected_at,
                    platform_claim_id=pending.platform_claim_id,
                    raw_status=pending.raw_status,
                )
            )
            return created_e.id
        logger.warning("알 수 없는 클레임 유형이라 미매칭 항목을 승격하지 못했습니다: %s", pending.claim_type)
        return None

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
