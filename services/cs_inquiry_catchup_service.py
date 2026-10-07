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
  integration_type="CS_CHECKPOINT", integration_code="<platform_id>:<CC|PI>"(source별 고정 2자 코드, 최대 13자 - platforms.id는 integer).
  last_success_at = "covered_until": 이 시각까지의 문의를 해당 source에서 완전히 가져와
  저장했다는 뜻(naive UTC). status NORMAL/ERROR, last_error_message = 안전한 오류 코드 한
  개(문의 ID/주문번호/본문/credential은 절대 저장하지 않는다), updated_at.
- 전진 조건(전부 충족): fetch·정규화·DB 저장 성공 + failed=0 + 요청예산/페이지 제한 초과
  없음 + commit 성공. checkpoint가 전진할 때는 항상 같은 트랜잭션의 데이터와 함께다(commit이 실패하면 둘 다
  롤백). 실패/rollback이면 전진하지 않고(단조 증가만 허용 - 뒤로 가지 않음), 항목 일부만 실패한
  PARTIAL_SUCCESS는 "계약 A"를 따른다 - 항목별로 성공한 case/history는 commit하되 checkpoint는 전진하지
  않아 다음 실행이 같은 구간 전체를 다시 조회한다(unique 제약 + upsert로 중복 없이 수렴).

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
source는 checkpoint가 더 뒤처진 쪽부터 번갈아(라운드 로빈) 처리해 한 source가 예산을 독점하지 못한다(기본
설정에서 두 source 모두 backlog가 있으면 매 실행마다 각자 최소 1구간씩 전진한다). 시작 조건이 최악 요청
수 기준이라 보수적이며, 한 실행에서 처리되는 구간 수는 실제 비용과 상대 source의 backlog에 따라 다르다
(20일 공백 = 구간 3개 기준 시뮬레이션 - docs/COMMERCIAL_ERP_ROADMAP.md 표 참고):
- 최소 비용(콜센터 4/상품별 1), 양쪽 backlog: 콜센터 2구간·상품별 3구간 -> 따라잡는 데 2회 실행.
- 최소 비용, 콜센터만 backlog: 콜센터 3구간(상품별은 오늘 구간 1개) -> 1회 실행.
- 최악 비용(36/9): 각 source 1구간씩 -> 3회 실행.
구간당 최악 요청 수가 실행당 예산보다 크면(설정 오류) BUDGET_TOO_SMALL로 드러낸다.
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


# integration_status.integration_code는 varchar(30)이고 platforms.id는 integer(최대 10자리)다. 긴 source 이름을
# 키에 넣으면 7자리 platform_id부터 길이를 넘으므로, source마다 고정된 짧은 코드(2자)를 쓴다 -
# "<platform_id>:<코드>"는 최대 13자라 어떤 integer platform_id에도 안전하고, 코드가 서로 다르므로 충돌하지
# 않는다(tests가 유일성을 검증). 새 source(예: 11번가)를 추가하면 여기에도 코드를 하나 추가해야 한다.
CHECKPOINT_SOURCE_CODES: dict[str, str] = {COUPANG_CALL_CENTER_SOURCE: "CC", COUPANG_PRODUCT_INQUIRY_SOURCE: "PI"}


def checkpoint_code(platform_id: int, source: str) -> str:
    short = CHECKPOINT_SOURCE_CODES.get(source)
    if short is None:
        raise ValueError(f"checkpoint 코드가 정의되지 않은 source입니다: {source}")
    if not 0 <= platform_id < 2**31:
        raise ValueError("platform_id가 integer 범위를 벗어났습니다.")
    return f"{platform_id}:{short}"


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
    def __init__(self, source: str, segments: list[tuple[date, date]], covered: Optional[datetime] = None) -> None:
        self.source = source
        self.covered = covered  # 이 실행 시작 시점의 checkpoint(라운드 로빈 순서 결정용)
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
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.session = session
        # 기본 잠금은 이 세션이 바인딩된 엔진으로 건다 - 데이터를 쓰는 DB와 잠금을 거는 DB가 항상 같다.
        self.lock_factory: LockFactory = (
            lock_factory
            if lock_factory is not None
            else (lambda platform_id, source: cs_sync_source_lock(platform_id, source, session.get_bind()))
        )
        # 기본 시계는 호출 시점에 모듈의 _utcnow_naive를 조회한다(테스트가 모듈 단위로 시계를 고정할 수 있게).
        self.now_fn: Callable[[], datetime] = now_fn if now_fn is not None else (lambda: _utcnow_naive())
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
        supported: list[str] = []

        for source in _INQUIRY_SOURCES:
            capability_attr = _INQUIRY_SOURCES[source][0]
            if getattr(connector, capability_attr, False):
                supported.append(source)
            else:
                by_source[source] = {"status": "UNSUPPORTED", "created": 0, "updated": 0, "failed": 0}

        states: dict[str, _SourceState] = {}
        with ExitStack() as stack:
            # 잠금은 항상 고정 순서(_INQUIRY_SOURCES)로 논블로킹 시도한다(대기하지 않으므로 교착 없음).
            # 구간 계획은 잠금을 얻은 "뒤에" checkpoint를 새로 읽어 만든다 - 앞선 실행이 방금 끝낸 진행
            # 위치를 놓치고 이미 처리한 구간을 다시 요청하지 않도록.
            for source in supported:
                acquired = stack.enter_context(self.lock_factory(platform_id, source))
                if not acquired:
                    blocked = _SourceState(source, [])
                    blocked.stopped = True
                    blocked.status = "ALREADY_RUNNING"
                    blocked.reason_code = "ALREADY_RUNNING"
                    states[source] = blocked
                    logger.info(
                        "CS 문의 동기화가 이미 실행 중이라 건너뜁니다: platform_id=%s source=%s", platform_id, source
                    )
                    continue
                covered = self.checkpoints.get_covered_until(platform_id, source)
                states[source] = _SourceState(source, plan_segments(covered, now), covered)

            # 예산 라운드 로빈은 checkpoint가 더 뒤처진 source(없으면 가장 먼저)부터 돈다 - 한 source가
            # 계속 먼저 예산을 가져가 다른 source가 굶는 일을 막는다. 잠금 순서와는 무관하다.
            ordered = dict(sorted(states.items(), key=lambda kv: (kv[1].covered is not None, kv[1].covered or now)))
            self._run_rounds(connector, platform_id, now, budget, ordered)

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
                worst = worst_case_requests(source)
                if worst > budget.max_requests:
                    # 설정 오류(구간 1개의 최악 요청 수가 실행당 예산보다 큼) - 조용히 영원히 미루지 않고
                    # 안전한 오류 코드로 드러낸다.
                    self._fail(state, platform_id, "BUDGET_TOO_SMALL", commit_data=False)
                    continue
                remaining = budget.max_requests - budget.used
                if remaining < worst:
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
            self._rollback_quietly()
            self._fail(state, platform_id, f"INTERNAL_ERROR:{type(exc).__name__}", commit_data=False)
            logger.warning(
                "CS 문의 동기화 예외: platform_id=%s source=%s type=%s", platform_id, source, type(exc).__name__
            )
            return

        status = result.get("status")
        failed = int(result.get("failed", 0))
        if status == "SUCCESS" and failed == 0:
            try:
                # 데이터(case/history)와 checkpoint를 같은 트랜잭션에서 한 번에 commit한다 - 중간에 어떤
                # 함수도 commit하지 않는다(sync_inquiries는 SAVEPOINT만 쓴다). commit이 실패하면 둘 다
                # 롤백돼 checkpoint만 전진하거나 데이터만 남는 일이 없다.
                self.checkpoints.advance(platform_id, source, covered_until_after(seg_end, now))
                self.session.commit()
            except Exception as exc:  # noqa: BLE001 - 이 source만 실패 처리하고 다른 source는 계속
                self._rollback_quietly()
                self._fail(state, platform_id, f"COMMIT_FAILED:{type(exc).__name__}", commit_data=False)
                logger.warning(
                    "CS 문의 동기화 commit 실패: platform_id=%s source=%s type=%s",
                    platform_id,
                    source,
                    type(exc).__name__,
                )
                return
            state.pending.pop(0)
            state.done += 1
            state.created += int(result.get("created", 0))
            state.updated += int(result.get("updated", 0))
            return

        if status == "UNSUPPORTED":
            self._rollback_quietly()
            state.stopped = True
            state.status = "UNSUPPORTED"
            return

        # PARTIAL_SUCCESS(항목 일부 실패) = 계약 A: 항목별 SAVEPOINT로 성공한 case/history는 실패 기록과 함께
        # commit하고 checkpoint는 전진하지 않는다(다음 실행이 같은 구간을 다시 조회, upsert로 수렴).
        # FAILED/DISABLED 등은 쓰기가 없으므로 rollback.
        keep_data = status == "PARTIAL_SUCCESS"
        reason = str(result.get("reason_code") or status or "UNKNOWN")
        state.created += int(result.get("created", 0)) if keep_data else 0
        state.updated += int(result.get("updated", 0)) if keep_data else 0
        state.failed += failed
        if not keep_data:
            self._rollback_quietly()
        self._fail(state, platform_id, reason, commit_data=keep_data, partial=keep_data)

    def _rollback_quietly(self) -> None:
        """rollback이 실패해도(연결 끊김 등) 원래 실패 사유를 가리거나 다른 source를 막지 않는다. 삼키는 범위는
        rollback 호출 하나뿐이고 예외 메시지는 남기지 않는다(클래스 이름만 로그)."""
        try:
            self.session.rollback()
        except Exception as exc:  # noqa: BLE001
            logger.warning("CS 문의 동기화 rollback 실패: type=%s", type(exc).__name__)

    def _fail(
        self, state: _SourceState, platform_id: int, reason: str, *, commit_data: bool, partial: bool = False
    ) -> None:
        try:
            self.checkpoints.record_failure(platform_id, state.source, reason)
            self.session.commit()  # (commit_data=True면 보존된 데이터와 함께, False면 오류 기록만)
        except Exception as exc:  # noqa: BLE001
            # 실패 "기록" 자체가 실패해도 원래 실패 사유를 가리거나 다른 source를 막지 않는다.
            self._rollback_quietly()  # 연결이 이미 죽었어도 아래 결과 보고는 계속한다
            logger.warning(
                "CS 문의 동기화 실패 기록 저장 실패: platform_id=%s source=%s type=%s",
                platform_id,
                state.source,
                type(exc).__name__,
            )
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
