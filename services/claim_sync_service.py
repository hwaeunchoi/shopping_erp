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
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from config.settings import settings
from integrations.malls.errors import (
    MarketplaceCapabilityUnsupportedError,
    MarketplaceCredentialMissingError,
    MarketplaceExternalAPIError,
    RequestBudget,
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
# 취소 단건 조회 후보 주문의 실행당 기본 상한(settings.claim_cancel_detail_max_orders_per_run이 우선한다).
DEFAULT_CANCELLATION_LOOKUP_BATCH = 20

# 취소 단건 조회 중 이 reason을 만나면 같은 일이 다음 주문에도 반복될 가능성이 높아(인증/레이트리밋/서버·네트워크
# 장애/예산 소진) 이번 배치를 즉시 멈춘다. 그 밖의 오류(페이지 상한·nextToken 이상·응답 형식 등)는 그 주문만
# 건너뛰고(cursor는 전진) 다음 주문을 계속 확인한다 - 한 주문의 영구 오류가 회전을 막아 다른 후보를 굶기지 않게 한다.
_BATCH_STOP_REASONS = frozenset(
    {
        "AUTH_FAILED",
        "RATE_LIMITED",
        "SERVER_ERROR",
        "TIMEOUT",
        "CONNECT_FAILED",
        "TRANSPORT_ERROR",
        "REQUEST_BUDGET_EXCEEDED",
    }
)
# 건너뛴 주문이 연속으로 이 횟수에 이르면 개별 주문 문제가 아니라 공통 문제로 보고 배치를 멈춘다.
_MAX_CONSECUTIVE_ORDER_SKIPS = 3


@dataclass
class ClaimSyncLimits:
    """클레임 수집 1회(플랫폼 1개)의 호출량 제한. budget은 목록 조회(반품·교환)와 취소 단건 조회가 공유한다."""

    max_pages: int
    max_retries: int
    budget: RequestBudget


def build_claim_sync_limits() -> ClaimSyncLimits:
    return ClaimSyncLimits(
        max_pages=settings.claim_sync_max_pages_per_query,
        max_retries=settings.claim_sync_max_retries_per_page,
        budget=RequestBudget(max_requests=settings.claim_sync_max_requests_per_run),
    )


def _estimate(connector: Any, feature: str, start: date, end: date, limits: ClaimSyncLimits) -> Optional[int]:
    """커넥터가 알려주는 해당 기능 1회의 최악 요청 수(없으면 None - 사전 예산 검사를 건너뛴다)."""
    estimator = getattr(connector, "estimate_claim_requests", None)
    if estimator is None:
        return None
    result = estimator(feature, start, end, limits.max_pages, limits.max_retries)
    return int(result) if isinstance(result, int) and not isinstance(result, bool) else None


class ClaimSyncService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.order_repo = OrderRepository(session)
        self.exchange_repo = ExchangeRepository(session)
        self.return_repo = ReturnRepository(session)
        self.cancellation_repo = CancellationRepository(session)
        self.unmatched_repo = ClaimUnmatchedRepository(session)
        self.cursor_repo = ClaimCollectionCursorRepository(session)

    def sync_claims(
        self,
        connector: Any,
        platform_id: int,
        start_date: date,
        end_date: date,
        limits: Optional[ClaimSyncLimits] = None,
        features: Optional[frozenset[str]] = None,
    ) -> dict[str, Any]:
        """취소/반품/교환을 capability 기준으로 미러링하고 구조화된 기능별 결과를 반환한다.

        호출량 제한(limits, 생략하면 settings 기본값으로 만든다 - 무제한 경로는 없다): 반품·교환은 각각 쿼리당
        max_pages/retry/공유 요청 예산이 적용되고, 기능을 시작하기 전에 커넥터가 추정한 "최악 요청 수"가 예산의 남은
        양보다 크면 네트워크 요청 없이 그 기능만 실패(BUDGET_TOO_SMALL: 예산 자체가 모자람 / REQUEST_BUDGET_EXCEEDED:
        이번 실행의 남은 예산이 모자람)로 처리한다.

        DB 쓰기 정책: 각 기능은 SAVEPOINT 안에서 "전부 조회한 뒤 저장"한다 - 조회 중 PAGE_LIMIT_EXCEEDED 등으로
        실패하면 그 기능은 아무 것도 저장하지 않고(부분 저장 없음) 다른 기능은 영향 없이 계속된다. 실패한 기능은 다음
        실행(기본 20분 뒤)에서 같은 기간을 다시 조회한다.

        반환:
            {"overall_status": str, "requests_used": int,
             "cancellations": {...}, "returns": {...}, "exchanges": {...},
             "skipped_no_order": int, "resolved_unmatched": int}
        각 기능 결과: {"status", "count", "reason_code", "retryable"}.

        features("cancellations"/"returns"/"exchanges"의 부분집합, 생략하면 전부): 지정하면 그 밖의 기능은 호출하지 않고
        UNSUPPORTED/NOT_REQUESTED로 표시한다(실계정 검증처럼 범위를 코드로 좁혀야 할 때 쓴다).
        """
        limits = limits or build_claim_sync_limits()
        resolved_unmatched = self._resolve_pending_unmatched(platform_id)
        skipped = {"count": 0}
        wanted = features if features is not None else frozenset({"cancellations", "returns", "exchanges"})

        cancellations = self._sync_feature(
            supported=getattr(connector, "supports_cancellation_sync", False) and "cancellations" in wanted,
            fetch=lambda: connector.fetch_cancellations(start_date, end_date),
            persist=lambda raw: self._persist_claim(
                "CANCELLATION", platform_id, raw, skipped, self.cancellation_repo, CANCELLATION_TRANSITIONS
            ),
            feature="cancellations",
        )
        returns = self._sync_feature(
            supported=getattr(connector, "supports_return_sync", False) and "returns" in wanted,
            fetch=lambda: connector.fetch_returns(
                start_date,
                end_date,
                max_pages=limits.max_pages,
                max_retries=limits.max_retries,
                request_budget=limits.budget,
            ),
            persist=lambda raw: self._persist_claim(
                "RETURN", platform_id, raw, skipped, self.return_repo, RETURN_TRANSITIONS
            ),
            feature="returns",
            worst_case=lambda: _estimate(connector, "returns", start_date, end_date, limits),
            budget=limits.budget,
        )
        exchanges = self._sync_feature(
            supported=getattr(connector, "supports_exchange_sync", False) and "exchanges" in wanted,
            fetch=lambda: connector.fetch_exchanges(
                start_date,
                end_date,
                max_pages=limits.max_pages,
                max_retries=limits.max_retries,
                request_budget=limits.budget,
            ),
            persist=lambda raw: self._persist_claim(
                "EXCHANGE", platform_id, raw, skipped, self.exchange_repo, EXCHANGE_TRANSITIONS
            ),
            feature="exchanges",
            worst_case=lambda: _estimate(connector, "exchanges", start_date, end_date, limits),
            budget=limits.budget,
        )

        results = {"cancellations": cancellations, "returns": returns, "exchanges": exchanges}
        for name in results:
            if name not in wanted:
                results[name] = _feature_result("UNSUPPORTED", 0, "NOT_REQUESTED", False)
        return {
            "overall_status": self._overall_status(results),
            "requests_used": limits.budget.used,
            **results,
            "skipped_no_order": skipped["count"],
            "resolved_unmatched": resolved_unmatched,
        }

    def sync_cancellations_by_candidate_orders(
        self,
        connector: Any,
        platform_id: int,
        max_orders: Optional[int] = None,
        limits: Optional[ClaimSyncLimits] = None,
    ) -> dict[str, Any]:
        """ "후보 주문 단건 조회" 방식의 취소 수집(상용 ERP 확장 2단계-A 보완) -
        기간만으로 대량조회가 안 되는 채널(쿠팡 등, supports_cancellation_lookup_by_order
        참고) 전용. sync_claims()의 기간 기반 대량조회(supports_cancellation_sync)와는
        별개 경로이며, 같은 sync_claims() 호출 안에 넣지 않고 별도로 호출한다(스케줄러가
        둘 다 호출 - scheduler/jobs/claim_sync_job.py 참고).

        회전식 체크포인트(ClaimCollectionCursor)로 Order.id 오름차순 최대 max_orders건만(기본 settings.claim_cancel_detail_max_orders_per_run)
        조회해(전체 무제한 순회 금지) 매 실행 요청 수를 예산 내로 제한한다. 배송 전
        상태(NEW/PREPARING/SHIPPING)의 주문만 후보로 삼는다(repositories.order_repository.
        CANCELLATION_LOOKUP_CANDIDATE_STATUSES 참고).

        ⚠️ 알려진 한계 1(주문 자체를 모름): ERP에 아직 수집되지 않은 주문
        (order_collect_job이 아직 못 가져온 주문)의 취소는 이 방식으로 알 수 없다 -
        후보 목록 자체가 이미 수집된 Order 테이블에서 나오기 때문이다. 그 주문이
        나중에 수집되면 다음 회전에서부터 후보에 포함된다(늦게라도 감지는 되지만
        즉시는 아니다).

        ⚠️ 알려진 한계 2(재조회 창 정책 - 후발 취소 커버리지): 커넥터의 조회기간은
        "오늘 기준 최근 CANCEL_LOOKUP_WINDOW_DAYS일"(쿠팡은 31일, 채널이 허용하는
        최대 range)이지 "주문일 이후 전체 기간"이 아니다(coupang_connector.
        fetch_cancellation_status 참고). 그래서 한 주문이 다시 점검되는 "간격"이
        CANCEL_LOOKUP_WINDOW_DAYS를 넘지 않는 한 그 사이에 발생한 취소는 반드시
        어느 한 번의 점검 창에는 걸린다(오래된 주문이라서가 아니라 "점검 간격"이
        관건). 반대로 후보 전체를 한 바퀴 도는 데(회전 주기) max_requests와 실행
        빈도 대비 후보 수가 너무 많아 CANCEL_LOOKUP_WINDOW_DAYS일을 초과하면, 그
        간격 동안 발생한 취소가 어느 창에도 걸리지 않아 놓칠 수 있다 - 운영자는
        (비활성 상태로 오래 머무는 후보 수) / (실행당 max_orders * 실행 빈도)가
        CANCEL_LOOKUP_WINDOW_DAYS일보다 작게 유지되도록 스케줄/배치 크기를
        조정해야 한다.

        호출량: 이 조회는 목록 조회(반품·교환)와 같은 요청 예산(limits.budget)을 공유한다. 각 주문을 확인하기
        전에 "최초 1회 + retry" 최악치가 남은 예산 안에 드는지 확인하고, 아니면 네트워크 요청 없이 배치를 멈춘다.

        한 후보 주문의 외부 API 호출 실패 중 공통 원인일 가능성이 높은 것(인증 거부/429/5xx/timeout/연결 실패/
        예산 소진 - _BATCH_STOP_REASONS)을 만나면 그 지점에서 이번 배치를 멈춘다(이후 후보는 이번 실행에서
        시도하지 않음 - 레이트리밋 등 채널 쪽 문제일 가능성이 높은데 계속 밀어붙이지 않기 위함). 그 밖의
        개별 주문 오류(PAGE_LIMIT_EXCEEDED/PAGINATION_*/BAD_RESPONSE/PARSE_FAILED 등)는 그 주문만 건너뛰고
        cursor를 그 주문 뒤로 전진시킨다 - 한 주문의 영구 오류가 같은 자리에서 매번 배치를 멈춰 뒤 후보를 굶기지
        않는다(건너뛴 주문은 다음 회전에서 다시 확인한다). 건너뜀이 연속 _MAX_CONSECUTIVE_ORDER_SKIPS회에
        이르면 공통 문제로 보고 멈춘다. 커서는 실패한 주문 **이전까지** 성공적으로 확인된 위치에만
        전진한다 - 실패한 주문은 다음 실행에서 맨 먼저 다시 시도되어 영구적으로
        누락되지 않는다(재조회는 claim_id 기반 dedup이라 중복 생성도 없다).
        인증정보 누락처럼 이후 모든 호출이 똑같이 실패할 오류는 예외로 전파해
        커서를 전혀 전진시키지 않고(이번 실행에서 성공한 것도 포함해 전부 롤백)
        배치를 즉시 중단한다 - 다음 실행이 이번 실행과 동일한 지점부터 다시
        시도한다."""
        if not getattr(connector, "supports_cancellation_lookup_by_order", False):
            return _feature_result("UNSUPPORTED", 0, "CAPABILITY_UNSUPPORTED", False)

        limits = limits or build_claim_sync_limits()
        batch = max_orders if max_orders is not None else settings.claim_cancel_detail_max_orders_per_run
        batch = max(0, min(batch, DEFAULT_CANCELLATION_LOOKUP_BATCH * 5))
        if batch == 0:
            return _feature_result("SUCCESS", 0, None, None, checked=0, skipped_failed=0)
        per_order_worst = _estimate(connector, "cancellation_lookup", date.today(), date.today(), limits)
        if per_order_worst is None:
            per_order_worst = 1 + limits.max_retries

        savepoint = self.session.begin_nested()
        try:
            cursor = self.cursor_repo.get_or_create(platform_id, CANCELLATION_ORDER_LOOKUP_CURSOR_TYPE)
            candidates = self.order_repo.list_cancellation_lookup_candidates(platform_id, cursor.last_order_id, batch)
            if len(candidates) < batch:
                seen_ids = {o.id for o in candidates}
                wrapped = self.order_repo.list_cancellation_lookup_candidates(platform_id, 0, batch - len(candidates))
                candidates += [o for o in wrapped if o.id not in seen_ids]

            skipped = {"count": 0}
            count = 0
            checked = 0
            skipped_failed = 0
            consecutive_skips = 0
            last_safe_id = cursor.last_order_id
            failure: Optional[MarketplaceExternalAPIError] = None
            for order in candidates:
                if limits.budget.max_requests - limits.budget.used < per_order_worst:
                    break  # 남은 예산이 이 주문의 최악치보다 작다 - 네트워크 요청 없이 멈춘다(다음 실행이 이어간다).
                try:
                    raw = connector.fetch_cancellation_status(
                        order.platform_order_no,
                        order.order_date.date(),
                        max_retries=limits.max_retries,
                        request_budget=limits.budget,
                    )
                except MarketplaceExternalAPIError as e:
                    logger.warning(
                        "취소 후보 주문 조회 실패: platform_id=%s, reason=%s, batch_stop=%s",
                        platform_id,
                        e.reason_code,
                        e.reason_code in _BATCH_STOP_REASONS,
                    )
                    if e.reason_code in _BATCH_STOP_REASONS:
                        failure = e
                        break
                    # 개별 주문 오류 - 이 주문만 건너뛰고 cursor를 전진시켜 뒤 후보를 계속 확인한다.
                    skipped_failed += 1
                    consecutive_skips += 1
                    last_safe_id = order.id
                    if consecutive_skips >= _MAX_CONSECUTIVE_ORDER_SKIPS:
                        failure = e
                        break
                    continue
                consecutive_skips = 0
                checked += 1
                # 조회 자체는 성공했다(취소 없음/있음 둘 다 "확인 완료") - 여기까지 온
                # 주문만 커서 전진 대상이다.
                last_safe_id = order.id
                if raw is None:
                    continue
                if self._persist_claim(
                    "CANCELLATION", platform_id, raw, skipped, self.cancellation_repo, CANCELLATION_TRANSITIONS
                ):
                    count += 1

            if candidates:
                cursor.last_order_id = last_safe_id
            cursor.updated_at = datetime.now(timezone.utc)
            self.session.flush()
            savepoint.commit()
            extra = {"checked": checked, "skipped_failed": skipped_failed}
            if failure is not None:
                status = "PARTIAL" if (count > 0 or checked > 0) else "FAILED"
                return _feature_result(status, count, failure.reason_code, bool(failure.retryable), **extra)
            return _feature_result("SUCCESS", count, None, None, **extra)
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
        self,
        supported: bool,
        fetch: Callable[[], list],
        persist: Callable[[dict], bool],
        feature: str,
        worst_case: Optional[Callable[[], Optional[int]]] = None,
        budget: Optional[RequestBudget] = None,
    ) -> dict[str, Any]:
        """한 클레임 기능을 SAVEPOINT 안에서 격리 처리한다. 실패는 그 기능만 롤백한다.

        worst_case/budget이 주어지면 시작 전에 "이 기능의 최악 요청 수"가 예산에 들어가는지 확인한다 - 안 들어가면
        네트워크 요청 없이 이 기능만 실패시킨다(BUDGET_TOO_SMALL: 실행당 예산 자체가 모자란 설정 오류,
        REQUEST_BUDGET_EXCEEDED: 이번 실행에서 앞선 사용량 때문에 남은 예산이 모자람)."""
        if not supported:
            # capability False - fetch 호출 자체를 하지 않는다(미지원과 0건 구분).
            return _feature_result("UNSUPPORTED", 0, "CAPABILITY_UNSUPPORTED", False)

        if worst_case is not None and budget is not None:
            worst = worst_case()
            if worst is not None:
                if worst > budget.max_requests:
                    return _feature_result("FAILED", 0, "BUDGET_TOO_SMALL", False)
                if budget.max_requests - budget.used < worst:
                    return _feature_result("FAILED", 0, "REQUEST_BUDGET_EXCEEDED", True)

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
        행으로 승격한다. 각 항목은 자기 SAVEPOINT 안에서 독립적으로 처리한다 - 클레임 행 생성과 "해결됨" 표시가
        한 단위로 반영되거나 함께 취소되고(반쪽 승격 없음), 한 항목의 DB 오류가 나머지 항목과 이어지는
        반품·교환 수집 전체를 막지 않는다(실패한 항목은 로그에 클래스만 남기고 다음 실행에서 다시 시도)."""
        resolved = 0
        for pending in self.unmatched_repo.list_unresolved(platform_id):
            order = self.order_repo.get_by_platform_order_no(platform_id, pending.platform_order_no)
            if order is None:
                continue
            savepoint = self.session.begin_nested()
            try:
                entity_id = self._promote_unmatched(pending, order.id)
                if entity_id is None:
                    savepoint.rollback()
                    continue
                pending.resolved_entity_id = entity_id
                pending.resolved_at = datetime.now(timezone.utc)
                self.session.flush()
                savepoint.commit()
                resolved += 1
            except Exception as exc:  # noqa: BLE001 - 항목별 격리(내부 전문·식별자 미노출)
                savepoint.rollback()
                logger.warning("미매칭 클레임 승격 실패(다음 실행에서 재시도): type=%s", type(exc).__name__)
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


def _feature_result(
    status: str, count: int, reason_code: Optional[str], retryable: Optional[bool], **extra: int
) -> dict[str, Any]:
    return {"status": status, "count": count, "reason_code": reason_code, "retryable": retryable, **extra}


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
