"""
services/cs_inquiry_catchup_service.py
-----------------------------------------
쿠팡 CS 문의 자동수집의 "15분 정기 실행 + PC 비가동 기간 catch-up".

이 ERP는 PC가 켜져 있는 동안에만 scheduler가 돈다(평일 업무시간 위주). 꺼져 있던 기간의
문의를 다음 실행에서 따라잡으려면 "어디까지 처리했는가"를 source별로 기억해야 한다.

checkpoint(source별 진행 위치)
-----------------------------
- 키: platform_id + external_source(COUPANG_CALL_CENTER / COUPANG_PRODUCT_INQUIRY).
- 저장소: 기존 integration_status 테이블(새 테이블/migration 없음) -
  integration_type="CS_CHECKPOINT", integration_code="<platform_id>:<source>"(30자 이내).
  last_success_at = "covered_until": 이 시각까지의 문의를 해당 source에서 완전히 가져와
  저장했다는 뜻(naive UTC). status NORMAL/ERROR, last_error_message = 안전한 오류 코드 한
  개(문의 ID/주문번호/본문/credential은 절대 저장하지 않는다), updated_at.
- 전진 조건(전부 충족): fetch·정규화·DB 저장 성공 + failed=0 + 요청예산/페이지 제한 초과
  없음 + commit 성공. 데이터 저장과 checkpoint 갱신은 같은 트랜잭션이라 한쪽만 반영되는
  일이 없다. 실패/부분실패/rollback이면 전진하지 않는다(단조 증가만 허용 - 뒤로 가지 않음).

조회 구간 계산(모두 Asia/Seoul 날짜 기준, 요청은 날짜 단위)
------------------------------------------------------
- checkpoint 없음(최초): settings.cs_inquiry_sync_window_days(기본 1)일, 오늘 포함.
- checkpoint 있음: 시작일 = covered_until의 Asia/Seoul 날짜, 종료일 = 오늘. 시작일을 다시
  포함해 경계 누락을 막고(중복은 source별 dedup으로 같은 case에 수렴), 같은 날을 15분마다
  반복 조회해도 안전하다.
- 쿠팡 문의 조회 API 최대 기간은 7일(콜센터/상품별 모두) - 구간을 최대 7일로 쪼개 가장
  오래된 것부터 처리한다. 한 구간 성공마다 checkpoint를 그 구간 끝까지만 옮긴다(오늘로
  건너뛰지 않는다). 지난 날짜 구간은 다음 날 0시(KST)를, 오늘이 포함된 구간은 실행 시작
  시각을 covered_until로 기록한다.

요청 예산(플랫폼 1개당 실행 1회 = 이 클래스의 sync_platform() 1회)
----------------------------------------------------------------
settings의 max_pages_per_query(3)·max_retries_per_page(2)·max_requests_per_run(45)을 그대로
쓴다. 두 source와 모든 구간이 하나의 RequestBudget을 공유한다. 구간 1개의 최악 요청 수:
콜센터 = 상태 4 x 3페이지 x (1+2) = 36, 상품별 = 1 x 3 x 3 = 9 (합 45 = 예산). 구간을 시작하기
전에 남은 예산이 그 구간의 최악 요청 수 이상일 때만 시작한다 - 부족하면 남은 구간은 다음
15분 실행으로 넘긴다(구간을 시작해 놓고 중간에 예산이 끊겨 버리는 낭비를 없앤다).
source는 번갈아(라운드 로빈) 처리해 한 source가 예산을 독점하지 못한다. 시작 조건이 최악 요청 수
기준이라 보수적이다: 기본 설정에서 구간당 실제 비용이 가장 작아도(콜센터 4 + 상품별 1) 한 실행에서
콜센터 구간은 2개까지(사용 10 -> 남은 35 < 36), 상품별 구간은 그보다 많이 시작된다. 남은 구간은
15분 뒤 실행이 이어받는다(예: 20일 공백 = 구간 3개 -> 콜센터 2개 + 다음 실행에서 1개).
예산 초과(REQUEST_BUDGET_EXCEEDED)/페이지 초과(PAGE_LIMIT_EXCEEDED)로 실패한 구간은 DB에
아무것도 쓰지 않고 checkpoint도 옮기지 않는다.

동시 실행 방지: (platform_id, source) advisory lock - services/cs_sync_lock.py 참고.
"""

import logging
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, ContextManager, Optional

from config.settings import settings
from integrations.malls.errors import RequestBudget
from models.extra import IntegrationStatus
from repositories.extra_repository import CS_CHECKPOINT_INTEGRATION_TYPE, IntegrationStatusRepository
from services.cs_channel_sync_service import (
    _INQUIRY_SOURCES,
    COUPANG_CALL_CENTER_SOURCE,
    COUPANG_PRODUCT_INQUIRY_SOURCE,
    CsChannelSyncService,
)
from services.cs_sync_lock import cs_sync_source_lock

logger = logging.getLogger(__name__)

CHECKPOINT_INTEGRATION_TYPE = CS_CHECKPOINT_INTEGRATION_TYPE
# checkpoint를 두는 source 목록(공식 계약이 확인된 소스 - CsChannelSyncService와 같은 순서).
SOURCES: tuple[str, ...] = tuple(_INQUIRY_SOURCES)
# 쿠팡 문의 조회 API 최대 조회기간(일) - 콜센터/상품별 문의 모두 7일(coupang_connector의
# CALL_CENTER_INQUIRY_MAX_RANGE_DAYS/ONLINE_INQUIRY_MAX_RANGE_DAYS와 같은 값이어야 한다 -
# tests/unit/test_cs_inquiry_catchup_service.py가 일치를 검증한다).
MAX_SEGMENT_DAYS = 7
# source 하나가 구간 1개를 처리할 때 보내는 "쿼리" 수(각 쿼리는 최대 max_pages 페이지, 페이지마다
# 최대 1+max_retries 시도). 콜센터는 상태 4종(NONE/ANSWER/NO_ANSWER/TRANSFER) 순회, 상품별 문의는
# answeredType=ALL 1종 - coupang_connector.CALL_CENTER_INQUIRY_STATUSES와 같아야 한다(테스트 검증).
QUERIES_PER_SEGMENT: dict[str, int] = {COUPANG_CALL_CENTER_SOURCE: 4, COUPANG_PRODUCT_INQUIRY_SOURCE: 1}

# 한국은 DST가 없다 - 고정 +09:00 오프셋이 Asia/Seoul과 동일하다(tzdata 의존성 없이 컨테이너/
# Windows 어디서나 같은 결과). coupang_connector._KST와 같은 방식이다.
KST = timezone(timedelta(hours=9))

LockFactory = Callable[[int, str], ContextManager[bool]]


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def kst_date(naive_utc: datetime) -> date:
    """naive UTC 시각의 Asia/Seoul 날짜."""
    return naive_utc.replace(tzinfo=timezone.utc).astimezone(KST).date()


def kst_midnight_as_naive_utc(day: date) -> datetime:
    """해당 날짜 00:00(KST)의 naive UTC 시각."""
    return datetime(day.year, day.month, day.day, tzinfo=KST).astimezone(timezone.utc).replace(tzinfo=None)


def initial_window(today: date) -> tuple[date, date]:
    """checkpoint가 없을 때의 조회 범위 - cs_inquiry_sync_window_days일, 오늘 포함."""
    days = max(1, settings.cs_inquiry_sync_window_days)
    return today - timedelta(days=days - 1), today


def plan_segments(covered_until: Optional[datetime], now: datetime) -> list[tuple[date, date]]:
    """처리해야 할 날짜 구간을 가장 오래된 것부터 최대 7일 단위로 반환한다(순수 함수).
    covered_until/now는 naive UTC. 시계가 어긋나 covered_until이 미래여도 오늘보다 뒤로
    가지 않는다. 항상 오늘이 포함된 마지막 구간이 있다."""
    today = kst_date(now)
    if covered_until is None:
        start, _ = initial_window(today)
    else:
        start = min(kst_date(covered_until), today)
    segments: list[tuple[date, date]] = []
    cursor = start
    while cursor <= today:
        end = min(cursor + timedelta(days=MAX_SEGMENT_DAYS - 1), today)
        segments.append((cursor, end))
        cursor = end + timedelta(days=1)
    return segments


def covered_until_after(segment_end: date, now: datetime) -> datetime:
    """구간 [.., segment_end] 성공 후 기록할 covered_until. 오늘이 포함된 구간이면 실행 시작
    시각(그 이후에 생긴 문의는 다음 실행이 가져간다), 지난 날짜 구간이면 다음 날 0시(KST)."""
    today = kst_date(now)
    if segment_end >= today:
        return now
    return kst_midnight_as_naive_utc(segment_end + timedelta(days=1))


def worst_case_requests(source: str) -> int:
    """구간 1개를 source 하나가 처리할 때의 최악 HTTP 요청 수."""
    return (
        QUERIES_PER_SEGMENT[source]
        * settings.cs_inquiry_sync_max_pages_per_query
        * (1 + settings.cs_inquiry_sync_max_retries_per_page)
    )


def checkpoint_code(platform_id: int, source: str) -> str:
    code = f"{platform_id}:{source}"
    if len(code) > 30:  # integration_status.integration_code String(30)
        raise ValueError("checkpoint 키가 integration_code 길이(30자)를 넘습니다.")
    return code


class CsSyncCheckpointRepository:
    """source별 checkpoint(integration_status, type=CS_CHECKPOINT)."""

    def __init__(self, session: Any) -> None:
        self.session = session
        self.repo = IntegrationStatusRepository(session)

    def get_covered_until(self, platform_id: int, source: str) -> Optional[datetime]:
        record = self.repo.get_by_type_and_code(CHECKPOINT_INTEGRATION_TYPE, checkpoint_code(platform_id, source))
        if record is None or record.last_success_at is None:
            return None
        return _as_naive_utc(record.last_success_at)

    def advance(self, platform_id: int, source: str, covered_until: datetime) -> None:
        """단조 증가만 허용한다 - 이미 더 뒤의 값이 있으면 건드리지 않는다."""
        now = _utcnow_naive()
        code = checkpoint_code(platform_id, source)
        record = self.repo.get_by_type_and_code(CHECKPOINT_INTEGRATION_TYPE, code)
        if record is None:
            self.repo.add(
                IntegrationStatus(
                    integration_type=CHECKPOINT_INTEGRATION_TYPE,
                    integration_code=code,
                    status="NORMAL",
                    last_success_at=covered_until,
                    updated_at=now,
                )
            )
            return
        current = None if record.last_success_at is None else _as_naive_utc(record.last_success_at)
        if current is None or covered_until > current:
            record.last_success_at = covered_until
        record.status = "NORMAL"
        record.last_error_message = None
        record.updated_at = now
        self.session.flush()

    def record_failure(self, platform_id: int, source: str, error_code: str) -> None:
        """안전한 오류 코드만 남긴다. last_success_at(진행 위치)은 건드리지 않는다."""
        now = _utcnow_naive()
        code = checkpoint_code(platform_id, source)
        record = self.repo.get_by_type_and_code(CHECKPOINT_INTEGRATION_TYPE, code)
        if record is None:
            self.repo.add(
                IntegrationStatus(
                    integration_type=CHECKPOINT_INTEGRATION_TYPE,
                    integration_code=code,
                    status="ERROR",
                    last_error_at=now,
                    last_error_message=error_code[:100],
                    updated_at=now,
                )
            )
            return
        record.status = "ERROR"
        record.last_error_at = now
        record.last_error_message = error_code[:100]
        record.updated_at = now
        self.session.flush()


class _SourceState:
    def __init__(self, source: str, segments: list[tuple[date, date]]) -> None:
        self.source = source
        self.pending = list(segments)
        self.done = 0
        self.created = 0
        self.updated = 0
        self.failed = 0
        self.stopped = False
        self.status: Optional[str] = None  # 중단 사유 상태(FAILED/ALREADY_RUNNING/DEFERRED 등)
        self.reason_code: Optional[str] = None

    def result(self) -> dict[str, Any]:
        status = self.status if self.status is not None else "SUCCESS"
        out: dict[str, Any] = {
            "status": status,
            "created": self.created,
            "updated": self.updated,
            "failed": self.failed,
            "segments_done": self.done,
            "segments_pending": len(self.pending),
        }
        if self.reason_code:
            out["reason_code"] = self.reason_code
        return out


class CsInquiryCatchupService:
    def __init__(
        self,
        session: Any,
        *,
        lock_factory: Optional[LockFactory] = None,
        now_fn: Callable[[], datetime] = _utcnow_naive,
    ) -> None:
        self.session = session
        # 기본 잠금은 이 세션이 바인딩된 엔진으로 건다 - 데이터를 쓰는 DB와 잠금을 거는 DB가 항상 같다.
        self.lock_factory: LockFactory = (
            lock_factory
            if lock_factory is not None
            else (lambda platform_id, source: cs_sync_source_lock(platform_id, source, session.get_bind()))
        )
        self.now_fn = now_fn
        self.sync_service = CsChannelSyncService(session)
        self.checkpoints = CsSyncCheckpointRepository(session)

    def is_up_to_date(self, platform_id: int, sources: list[str], now: Optional[datetime] = None) -> bool:
        """모든 source의 checkpoint가 최근 실행 주기 안이면 True - 재시작 직후 catch-up이
        "이미 최신"이면 외부 호출 없이 건너뛰는 판단에 쓴다."""
        current = now if now is not None else self.now_fn()
        interval = timedelta(minutes=settings.cs_inquiry_sync_interval_minutes)
        for source in sources:
            covered = self.checkpoints.get_covered_until(platform_id, source)
            if covered is None or kst_date(covered) != kst_date(current) or current - covered >= interval:
                return False
        return True

    def sync_platform(self, connector: Any, platform_id: int) -> dict[str, Any]:
        """플랫폼 1개를 한 번 실행한다(= 요청 예산 1개). 반환값은
        CsChannelSyncService.sync_all_inquiries()와 같은 키(status/created/updated/failed/
        by_source[/reason_code])에 source별 segments_done/segments_pending이 추가된 형태다."""
        now = self.now_fn()
        budget = RequestBudget(max_requests=settings.cs_inquiry_sync_max_requests_per_run)
        by_source: dict[str, dict[str, Any]] = {}
        states: dict[str, _SourceState] = {}

        for source in _INQUIRY_SOURCES:
            capability_attr = _INQUIRY_SOURCES[source][0]
            if not getattr(connector, capability_attr, False):
                by_source[source] = {"status": "UNSUPPORTED", "created": 0, "updated": 0, "failed": 0}
                continue
            states[source] = _SourceState(
                source, plan_segments(self.checkpoints.get_covered_until(platform_id, source), now)
            )

        with ExitStack() as stack:
            for source, state in states.items():
                acquired = stack.enter_context(self.lock_factory(platform_id, source))
                if not acquired:
                    state.stopped = True
                    state.status = "ALREADY_RUNNING"
                    state.reason_code = "ALREADY_RUNNING"
                    logger.info(
                        "CS 문의 동기화가 이미 실행 중이라 건너뜁니다: platform_id=%s source=%s", platform_id, source
                    )
            self._run_rounds(connector, platform_id, now, budget, states)

        for source, state in states.items():
            by_source[source] = state.result()
        return self._combine(by_source)

    def _run_rounds(
        self, connector: Any, platform_id: int, now: datetime, budget: RequestBudget, states: dict[str, _SourceState]
    ) -> None:
        progressed = True
        while progressed:
            progressed = False
            for source, state in states.items():
                if state.stopped or not state.pending:
                    continue
                remaining = budget.max_requests - budget.used
                if remaining < worst_case_requests(source):
                    # 구간을 시작하지 않는다 - 남은 구간은 다음 실행에서 이어간다.
                    state.stopped = True
                    if state.done == 0:
                        state.status = "DEFERRED"
                        state.reason_code = "BUDGET_RESERVED"
                    continue
                self._run_segment(connector, platform_id, now, budget, state)
                progressed = True

    def _run_segment(
        self, connector: Any, platform_id: int, now: datetime, budget: RequestBudget, state: _SourceState
    ) -> None:
        source = state.source
        seg_start, seg_end = state.pending[0]
        try:
            result = self.sync_service.sync_inquiries(
                connector,
                platform_id,
                seg_start,
                seg_end,
                source=source,
                max_pages=settings.cs_inquiry_sync_max_pages_per_query,
                max_retries=settings.cs_inquiry_sync_max_retries_per_page,
                request_budget=budget,
            )
        except Exception as exc:  # noqa: BLE001 - source 단위 격리(예상 밖 예외도 다른 source는 계속)
            self.session.rollback()
            self._fail(state, platform_id, f"INTERNAL_ERROR:{type(exc).__name__}", commit_data=False)
            logger.warning(
                "CS 문의 동기화 예외: platform_id=%s source=%s type=%s", platform_id, source, type(exc).__name__
            )
            return

        status = result.get("status")
        failed = int(result.get("failed", 0))
        if status == "SUCCESS" and failed == 0:
            self.checkpoints.advance(platform_id, source, covered_until_after(seg_end, now))
            self.session.commit()
            state.pending.pop(0)
            state.done += 1
            state.created += int(result.get("created", 0))
            state.updated += int(result.get("updated", 0))
            return

        if status == "UNSUPPORTED":
            self.session.rollback()
            state.stopped = True
            state.status = "UNSUPPORTED"
            return

        # PARTIAL_SUCCESS(항목 일부 실패): 이미 저장된 항목은 멱등이므로 보존(commit)하되 checkpoint는
        # 전진하지 않는다. FAILED/DISABLED 등은 쓰기가 없으므로 rollback.
        keep_data = status == "PARTIAL_SUCCESS"
        reason = str(result.get("reason_code") or status or "UNKNOWN")
        state.created += int(result.get("created", 0)) if keep_data else 0
        state.updated += int(result.get("updated", 0)) if keep_data else 0
        state.failed += failed
        if not keep_data:
            self.session.rollback()
        self._fail(state, platform_id, reason, commit_data=keep_data, partial=keep_data)

    def _fail(
        self, state: _SourceState, platform_id: int, reason: str, *, commit_data: bool, partial: bool = False
    ) -> None:
        self.checkpoints.record_failure(platform_id, state.source, reason)
        self.session.commit()  # (commit_data=True면 보존된 데이터와 함께, False면 checkpoint 오류 기록만)
        state.stopped = True
        # 앞선 구간을 이미 성공시킨 source는 완전 실패로 위장하지 않는다(진행분은 checkpoint에 남아 있다).
        state.status = "PARTIAL_SUCCESS" if (partial or state.done > 0) else "FAILED"
        state.reason_code = reason

    @staticmethod
    def _combine(by_source: dict[str, dict[str, Any]]) -> dict[str, Any]:
        statuses = {r["status"] for r in by_source.values()}
        total_created = sum(int(r.get("created", 0)) for r in by_source.values())
        total_updated = sum(int(r.get("updated", 0)) for r in by_source.values())
        total_failed = sum(int(r.get("failed", 0)) for r in by_source.values())
        considered = statuses - {"UNSUPPORTED", "ALREADY_RUNNING", "DEFERRED"}
        if statuses == {"UNSUPPORTED"}:
            overall = "UNSUPPORTED"
        elif not considered:
            overall = "ALREADY_RUNNING" if "ALREADY_RUNNING" in statuses else "DEFERRED"
        elif considered <= {"SUCCESS"} and total_failed == 0:
            overall = "SUCCESS"
        elif considered & {"SUCCESS", "PARTIAL_SUCCESS"}:
            overall = "PARTIAL_SUCCESS"
        else:
            overall = "FAILED"
        combined: dict[str, Any] = {
            "status": overall,
            "created": total_created,
            "updated": total_updated,
            "failed": total_failed,
            "by_source": by_source,
        }
        reasons = [
            str(r["reason_code"]) for r in by_source.values() if r.get("status") == "FAILED" and r.get("reason_code")
        ]
        if reasons:
            combined["reason_code"] = ",".join(reasons)
        return combined
