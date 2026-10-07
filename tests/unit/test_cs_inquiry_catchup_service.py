"""
tests/unit/test_cs_inquiry_catchup_service.py
-------------------------------------------------
services.cs_inquiry_catchup_service - CS 문의 자동수집의 날짜 구간 계산·source별 checkpoint·
7일 구간 분할·요청 예산·동시 실행 방지 검증. 실제 채널 API는 호출하지 않는다(스텁 커넥터만 사용,
합성 데이터만 사용).

잠금은 항상 이 테스트의 SQLite 엔진에 바인딩된 프로세스 내 잠금으로 동작한다(PostgreSQL
advisory lock 실제 경합은 tests/integration/test_cs_sync_lock_pg.py가 검증한다).
"""

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import pytest

from config.settings import settings
from integrations.malls.coupang_connector import (
    CALL_CENTER_INQUIRY_MAX_RANGE_DAYS,
    CALL_CENTER_INQUIRY_STATUSES,
    ONLINE_INQUIRY_MAX_RANGE_DAYS,
)
from integrations.malls.errors import MarketplaceExternalAPIError, RequestBudget
from repositories.cs_case_repository import CsCaseRepository
from repositories.extra_repository import IntegrationStatusRepository
from services.cs_channel_sync_service import COUPANG_CALL_CENTER_SOURCE, COUPANG_PRODUCT_INQUIRY_SOURCE
from services.cs_inquiry_catchup_service import (
    CHECKPOINT_INTEGRATION_TYPE,
    KST,
    MAX_SEGMENT_DAYS,
    QUERIES_PER_SEGMENT,
    SOURCES,
    CsInquiryCatchupService,
    CsSyncCheckpointRepository,
    checkpoint_code,
    covered_until_after,
    initial_window,
    kst_date,
    kst_midnight_as_naive_utc,
    plan_segments,
    worst_case_requests,
)
from services.cs_sync_lock import cs_sync_source_lock

CC = COUPANG_CALL_CENTER_SOURCE
PR = COUPANG_PRODUCT_INQUIRY_SOURCE


def kst_to_utc(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    """KST 벽시각 -> naive UTC."""
    return datetime(y, mo, d, h, mi, tzinfo=KST).astimezone(timezone.utc).replace(tzinfo=None)


# 2026-10-07(수) 10:00 KST
NOW = kst_to_utc(2026, 10, 7, 10, 0)
TODAY = date(2026, 10, 7)


@pytest.fixture(autouse=True)
def _enable_cs_inquiry_sync(monkeypatch):
    monkeypatch.setattr(settings, "cs_inquiry_sync_enabled", True)
    monkeypatch.setattr(settings, "cs_inquiry_sync_window_days", 1)
    monkeypatch.setattr(settings, "cs_inquiry_sync_max_pages_per_query", 3)
    monkeypatch.setattr(settings, "cs_inquiry_sync_max_retries_per_page", 2)
    monkeypatch.setattr(settings, "cs_inquiry_sync_max_requests_per_run", 45)
    monkeypatch.setattr(settings, "cs_inquiry_sync_interval_minutes", 15)


def _item(
    inquiry_id: str = "1001", content: str = "배송 문의", inquiry_at: Optional[datetime] = None
) -> dict[str, Any]:
    return {
        "platform_inquiry_id": inquiry_id,
        "content": content,
        "inquiry_at": inquiry_at or datetime(2026, 10, 7, 0, 30, 0),
        "raw_status": "progress:requestAnswer",
        "needs_answer": True,
        "platform_order_no": None,
        "customer_phone": "010-0000-0000",
    }


class _Connector:
    """호출된 (source, start, end)와 넘겨받은 예산을 기록하는 스텁. 실제 커넥터처럼 호출마다 예산을
    소비한다(콜센터 cost_cc회, 상품별 cost_pr회 - 기본값은 페이지 1·재시도 0인 일반적인 경우)."""

    supports_inquiry_sync = True
    supports_product_inquiry_sync = True

    def __init__(
        self,
        cc_items: Optional[list[dict[str, Any]]] = None,
        pr_items: Optional[list[dict[str, Any]]] = None,
        cost_cc: int = 4,
        cost_pr: int = 1,
        cc_error: Optional[Exception] = None,
        pr_error: Optional[Exception] = None,
    ) -> None:
        self.cc_items = cc_items or []
        self.pr_items = pr_items or []
        self.cost_cc = cost_cc
        self.cost_pr = cost_pr
        self.cc_error = cc_error
        self.pr_error = pr_error
        self.calls: list[tuple[str, date, date]] = []
        self.budgets: list[Any] = []
        self.kwargs: list[dict[str, Any]] = []

    def _spend(self, budget: Any, n: int) -> None:
        for _ in range(n):
            budget.consume("coupang")  # 실제 커넥터처럼 요청 "직전"에 소비 - 한도 초과면 여기서 예외

    def fetch_inquiries(self, start_date, end_date, *, max_pages=None, max_retries=None, request_budget=None):
        self.calls.append((CC, start_date, end_date))
        self.budgets.append(request_budget)
        self.kwargs.append({"max_pages": max_pages, "max_retries": max_retries})
        self._spend(request_budget, self.cost_cc)
        if self.cc_error is not None:
            raise self.cc_error
        return self.cc_items

    def fetch_product_inquiries(self, start_date, end_date, *, max_pages=None, max_retries=None, request_budget=None):
        self.calls.append((PR, start_date, end_date))
        self.budgets.append(request_budget)
        self.kwargs.append({"max_pages": max_pages, "max_retries": max_retries})
        self._spend(request_budget, self.cost_pr)
        if self.pr_error is not None:
            raise self.pr_error
        return self.pr_items

    def calls_for(self, source: str) -> list[tuple[date, date]]:
        return [(s, e) for src, s, e in self.calls if src == source]


class _UnsupportedConnector:
    supports_inquiry_sync = False
    supports_product_inquiry_sync = False


def _service(db_session, engine, now: datetime = NOW) -> CsInquiryCatchupService:
    return CsInquiryCatchupService(
        db_session,
        lock_factory=lambda platform_id, source: cs_sync_source_lock(platform_id, source, engine),
        now_fn=lambda: now,
    )


def _covered(db_session, platform_id: int, source: str) -> Optional[datetime]:
    return CsSyncCheckpointRepository(db_session).get_covered_until(platform_id, source)


def _seed_checkpoint(db_session, platform_id: int, source: str, covered_until: datetime) -> None:
    CsSyncCheckpointRepository(db_session).advance(platform_id, source, covered_until)
    db_session.commit()


# ---------------------------------------------------------------------------
# 상수/최악 조건 계산 - 코드와 실제 커넥터 계약이 어긋나지 않도록 고정한다
# ---------------------------------------------------------------------------


class TestContractsAndWorstCase:
    def test_segment_length_matches_coupang_api_limit(self):
        assert MAX_SEGMENT_DAYS == CALL_CENTER_INQUIRY_MAX_RANGE_DAYS == ONLINE_INQUIRY_MAX_RANGE_DAYS == 7

    def test_queries_per_segment_match_connector(self):
        assert QUERIES_PER_SEGMENT[CC] == len(CALL_CENTER_INQUIRY_STATUSES) == 4
        assert QUERIES_PER_SEGMENT[PR] == 1

    def test_sources_cover_both_official_sources_in_order(self):
        assert SOURCES == (CC, PR)

    def test_worst_case_per_segment_with_default_limits(self):
        # 콜센터: 4상태 x 3페이지 x (1+2회 시도) = 36, 상품별: 1 x 3 x 3 = 9.
        assert worst_case_requests(CC) == 36
        assert worst_case_requests(PR) == 9

    def test_worst_case_of_one_segment_for_both_sources_equals_the_run_budget(self):
        assert worst_case_requests(CC) + worst_case_requests(PR) == settings.cs_inquiry_sync_max_requests_per_run == 45

    def test_checkpoint_key_fits_integration_code_column(self):
        assert len(checkpoint_code(99999, PR)) <= 30
        with pytest.raises(ValueError):
            checkpoint_code(10**9, PR)


# ---------------------------------------------------------------------------
# 날짜/구간 계산(순수 함수)
# ---------------------------------------------------------------------------


class TestDateAndSegmentPlanning:
    def test_first_run_without_checkpoint_uses_window_days_including_today(self):
        assert plan_segments(None, NOW) == [(TODAY, TODAY)]

    def test_first_run_respects_larger_window_days(self, monkeypatch):
        monkeypatch.setattr(settings, "cs_inquiry_sync_window_days", 3)
        assert initial_window(TODAY) == (date(2026, 10, 5), TODAY)
        assert plan_segments(None, NOW) == [(date(2026, 10, 5), TODAY)]

    def test_same_day_rerun_after_15_minutes_queries_today_only(self):
        covered = NOW - timedelta(minutes=15)
        assert plan_segments(covered, NOW) == [(TODAY, TODAY)]

    def test_next_day_run_includes_previous_day_for_boundary_safety(self):
        covered = kst_to_utc(2026, 10, 6, 17, 45)
        assert plan_segments(covered, NOW) == [(date(2026, 10, 6), TODAY)]

    def test_friday_shutdown_then_monday_restart_queries_friday_through_monday(self):
        friday_evening = kst_to_utc(2026, 10, 2, 17, 59)  # 금
        monday = kst_to_utc(2026, 10, 5, 9, 30)  # 월
        assert plan_segments(friday_evening, monday) == [(date(2026, 10, 2), date(2026, 10, 5))]

    def test_exactly_seven_days_is_one_segment(self):
        covered = kst_to_utc(2026, 10, 1, 12, 0)  # 10/1..10/7 = 7일
        assert plan_segments(covered, NOW) == [(date(2026, 10, 1), TODAY)]

    def test_eight_days_splits_into_two_segments_oldest_first(self):
        covered = kst_to_utc(2026, 9, 30, 12, 0)  # 9/30..10/7 = 8일
        assert plan_segments(covered, NOW) == [(date(2026, 9, 30), date(2026, 10, 6)), (TODAY, TODAY)]

    @pytest.mark.parametrize("gap_days, expected_segments", [(8, 2), (14, 3), (20, 3), (21, 4), (60, 9)])
    def test_long_gaps_are_split_into_segments_of_at_most_seven_days(self, gap_days, expected_segments):
        covered = kst_to_utc(2026, 10, 7, 12, 0) - timedelta(days=gap_days)
        segments = plan_segments(covered, NOW)
        assert len(segments) == expected_segments
        assert segments[0][0] == TODAY - timedelta(days=gap_days)
        assert segments[-1][1] == TODAY
        for start, end in segments:
            assert (end - start).days + 1 <= MAX_SEGMENT_DAYS
        # 빈틈·겹침 없이 이어진다.
        for (_, prev_end), (next_start, _) in zip(segments, segments[1:], strict=False):
            assert next_start == prev_end + timedelta(days=1)

    def test_clock_skew_checkpoint_in_the_future_never_goes_past_today(self):
        assert plan_segments(NOW + timedelta(days=3), NOW) == [(TODAY, TODAY)]

    def test_seoul_midnight_boundary_just_before_and_after(self):
        before = kst_to_utc(2026, 10, 6, 23, 59)
        after = kst_to_utc(2026, 10, 7, 0, 1)
        assert kst_date(before) == date(2026, 10, 6)
        assert kst_date(after) == date(2026, 10, 7)
        # UTC 날짜로는 둘 다 10/6이지만(15:59Z/15:01Z... KST 자정은 15:00Z) 기준은 항상 KST다.
        assert plan_segments(before, after) == [(date(2026, 10, 6), date(2026, 10, 7))]

    def test_covered_until_for_past_segment_is_next_kst_midnight(self):
        assert covered_until_after(date(2026, 10, 3), NOW) == kst_midnight_as_naive_utc(date(2026, 10, 4))
        assert covered_until_after(date(2026, 10, 3), NOW) == kst_to_utc(2026, 10, 4, 0, 0)

    def test_covered_until_for_segment_containing_today_is_run_start(self):
        assert covered_until_after(TODAY, NOW) == NOW


# ---------------------------------------------------------------------------
# checkpoint 저장소
# ---------------------------------------------------------------------------


class TestCheckpointRepository:
    def test_advance_creates_row_under_checkpoint_type_with_platform_and_source_key(self, db_session, platform):
        repo = CsSyncCheckpointRepository(db_session)
        repo.advance(platform.id, CC, NOW)
        record = IntegrationStatusRepository(db_session).get_by_type_and_code(
            CHECKPOINT_INTEGRATION_TYPE, checkpoint_code(platform.id, CC)
        )
        assert record is not None
        assert record is not None
        assert record.status == "NORMAL"
        assert record.integration_code == f"{platform.id}:{CC}"
        assert repo.get_covered_until(platform.id, CC) == NOW

    def test_advance_is_monotonic_never_moves_backwards(self, db_session, platform):
        repo = CsSyncCheckpointRepository(db_session)
        repo.advance(platform.id, CC, NOW)
        repo.advance(platform.id, CC, NOW - timedelta(days=2))
        assert repo.get_covered_until(platform.id, CC) == NOW

    def test_failure_records_safe_code_without_moving_progress(self, db_session, platform):
        repo = CsSyncCheckpointRepository(db_session)
        repo.advance(platform.id, CC, NOW)
        repo.record_failure(platform.id, CC, "PAGE_LIMIT_EXCEEDED")
        record = IntegrationStatusRepository(db_session).get_by_type_and_code(
            CHECKPOINT_INTEGRATION_TYPE, checkpoint_code(platform.id, CC)
        )
        assert record is not None
        assert record.status == "ERROR"
        assert record.last_error_message == "PAGE_LIMIT_EXCEEDED"
        assert repo.get_covered_until(platform.id, CC) == NOW

    def test_failure_without_prior_checkpoint_leaves_no_progress(self, db_session, platform):
        repo = CsSyncCheckpointRepository(db_session)
        repo.record_failure(platform.id, PR, "REQUEST_BUDGET_EXCEEDED")
        assert repo.get_covered_until(platform.id, PR) is None

    def test_success_after_failure_clears_error_message(self, db_session, platform):
        repo = CsSyncCheckpointRepository(db_session)
        repo.record_failure(platform.id, CC, "RATE_LIMITED")
        repo.advance(platform.id, CC, NOW)
        record = IntegrationStatusRepository(db_session).get_by_type_and_code(
            CHECKPOINT_INTEGRATION_TYPE, checkpoint_code(platform.id, CC)
        )
        assert record is not None
        assert record.status == "NORMAL"
        assert record.last_error_message is None

    def test_checkpoint_rows_are_hidden_from_integration_status_listing(self, db_session, platform):
        CsSyncCheckpointRepository(db_session).advance(platform.id, CC, NOW)
        IntegrationStatusRepository(db_session).upsert_success("CS_INQUIRY", "coupang")
        db_session.flush()
        listed = IntegrationStatusRepository(db_session).list_all_status()
        assert [r.integration_type for r in listed] == ["CS_INQUIRY"]

    def test_checkpoints_are_independent_per_platform_and_source(self, db_session, platform, naver_platform):
        repo = CsSyncCheckpointRepository(db_session)
        repo.advance(platform.id, CC, NOW)
        assert repo.get_covered_until(platform.id, PR) is None
        assert repo.get_covered_until(naver_platform.id, CC) is None


# ---------------------------------------------------------------------------
# sync_platform - 정상 흐름
# ---------------------------------------------------------------------------


class TestSyncPlatformHappyPath:
    def test_first_run_creates_cases_and_checkpoints_for_both_sources(self, db_session, engine, platform):
        connector = _Connector(cc_items=[_item("1")], pr_items=[_item("2")])

        result = _service(db_session, engine).sync_platform(connector, platform.id)

        assert result["status"] == "SUCCESS"
        assert result["created"] == 2
        assert connector.calls_for(CC) == [(TODAY, TODAY)]
        assert connector.calls_for(PR) == [(TODAY, TODAY)]
        assert _covered(db_session, platform.id, CC) == NOW
        assert _covered(db_session, platform.id, PR) == NOW
        assert result["by_source"][CC]["segments_pending"] == 0

    def test_same_day_rerun_converges_to_same_cases_without_duplicates(self, db_session, engine, platform):
        connector = _Connector(cc_items=[_item("1")], pr_items=[_item("2")])
        _service(db_session, engine, NOW).sync_platform(connector, platform.id)

        later = NOW + timedelta(minutes=15)
        result = _service(db_session, engine, later).sync_platform(connector, platform.id)

        assert result["created"] == 0
        assert result["updated"] == 2
        repo = CsCaseRepository(db_session)
        assert repo.get_by_external(platform.id, CC, "1") is not None
        assert repo.get_by_external(platform.id, PR, "2") is not None
        assert _covered(db_session, platform.id, CC) == later
        assert connector.calls_for(CC) == [(TODAY, TODAY), (TODAY, TODAY)]

    def test_next_day_run_requeries_previous_day_and_advances(self, db_session, engine, platform):
        _seed_checkpoint(db_session, platform.id, CC, kst_to_utc(2026, 10, 6, 17, 45))
        _seed_checkpoint(db_session, platform.id, PR, kst_to_utc(2026, 10, 6, 17, 45))
        connector = _Connector()

        _service(db_session, engine).sync_platform(connector, platform.id)

        assert connector.calls_for(CC) == [(date(2026, 10, 6), TODAY)]
        assert connector.calls_for(PR) == [(date(2026, 10, 6), TODAY)]
        assert _covered(db_session, platform.id, CC) == NOW

    def test_friday_to_monday_restart_catches_up_in_one_call_per_source(self, db_session, engine, platform):
        friday = kst_to_utc(2026, 10, 2, 17, 59)
        monday = kst_to_utc(2026, 10, 5, 9, 30)
        _seed_checkpoint(db_session, platform.id, CC, friday)
        _seed_checkpoint(db_session, platform.id, PR, friday)
        connector = _Connector()

        _service(db_session, engine, monday).sync_platform(connector, platform.id)

        assert connector.calls_for(CC) == [(date(2026, 10, 2), date(2026, 10, 5))]
        assert connector.calls_for(PR) == [(date(2026, 10, 2), date(2026, 10, 5))]

    def test_both_sources_share_one_budget_instance_and_pass_configured_limits(self, db_session, engine, platform):
        connector = _Connector()
        _service(db_session, engine).sync_platform(connector, platform.id)

        assert len({id(b) for b in connector.budgets}) == 1
        assert isinstance(connector.budgets[0], RequestBudget)
        assert connector.budgets[0].max_requests == 45
        assert {(k["max_pages"], k["max_retries"]) for k in connector.kwargs} == {(3, 2)}

    def test_unsupported_connector_makes_no_calls_and_no_checkpoints(self, db_session, engine, platform):
        result = _service(db_session, engine).sync_platform(_UnsupportedConnector(), platform.id)

        assert result["status"] == "UNSUPPORTED"
        assert _covered(db_session, platform.id, CC) is None
        assert _covered(db_session, platform.id, PR) is None

    def test_same_numeric_id_in_both_sources_coexists(self, db_session, engine, platform):
        connector = _Connector(cc_items=[_item("7777")], pr_items=[_item("7777")])
        result = _service(db_session, engine).sync_platform(connector, platform.id)

        assert result["created"] == 2
        repo = CsCaseRepository(db_session)
        assert repo.get_by_external(platform.id, CC, "7777") is not None
        assert repo.get_by_external(platform.id, PR, "7777") is not None

    def test_duplicate_item_in_one_fetch_converges_to_one_case_without_type_error(self, db_session, engine, platform):
        """운영에서 TypeError를 일으킨 조건(같은 문의가 한 번의 수집에 두 번) - 같은 case로 수렴."""
        connector = _Connector(cc_items=[_item("42"), _item("42")])
        result = _service(db_session, engine).sync_platform(connector, platform.id)

        assert result["by_source"][CC]["status"] == "SUCCESS"
        assert result["by_source"][CC]["created"] == 1
        assert result["by_source"][CC]["updated"] == 1


# ---------------------------------------------------------------------------
# 실패 시 checkpoint 불변, source 격리
# ---------------------------------------------------------------------------


class TestFailuresDoNotAdvanceCheckpoint:
    def test_page_limit_exceeded_writes_nothing_and_keeps_checkpoint(self, db_session, engine, platform):
        old = kst_to_utc(2026, 10, 7, 9, 0)
        _seed_checkpoint(db_session, platform.id, PR, old)
        connector = _Connector(
            cc_items=[_item("1")],
            pr_items=[_item("2")],
            pr_error=MarketplaceExternalAPIError("coupang", "PAGE_LIMIT_EXCEEDED", False),
        )

        result = _service(db_session, engine).sync_platform(connector, platform.id)

        assert result["by_source"][PR]["status"] == "FAILED"
        assert result["by_source"][PR]["reason_code"] == "PAGE_LIMIT_EXCEEDED"
        assert _covered(db_session, platform.id, PR) == old  # 불변
        assert CsCaseRepository(db_session).get_by_external(platform.id, PR, "2") is None  # 쓰기 없음

    def test_failed_source_does_not_roll_back_the_other_sources_success(self, db_session, engine, platform):
        connector = _Connector(
            cc_items=[_item("1")],
            pr_error=MarketplaceExternalAPIError("coupang", "RATE_LIMITED", True, http_status=429),
        )

        result = _service(db_session, engine).sync_platform(connector, platform.id)

        assert result["status"] == "PARTIAL_SUCCESS"
        assert result["by_source"][CC]["status"] == "SUCCESS"
        assert _covered(db_session, platform.id, CC) == NOW  # 성공 source는 전진
        assert _covered(db_session, platform.id, PR) is None  # 실패 source는 그대로
        assert CsCaseRepository(db_session).get_by_external(platform.id, CC, "1") is not None

    def test_failed_run_records_safe_error_code_on_checkpoint_row(self, db_session, engine, platform):
        connector = _Connector(cc_error=MarketplaceExternalAPIError("coupang", "SERVER_ERROR", True, http_status=503))
        _service(db_session, engine).sync_platform(connector, platform.id)

        record = IntegrationStatusRepository(db_session).get_by_type_and_code(
            CHECKPOINT_INTEGRATION_TYPE, checkpoint_code(platform.id, CC)
        )
        assert record is not None
        assert record.status == "ERROR"
        assert record.last_error_message == "SERVER_ERROR"

    def test_partial_failure_keeps_idempotent_data_but_does_not_advance(
        self, db_session, engine, platform, monkeypatch
    ):
        old = kst_to_utc(2026, 10, 7, 9, 0)
        _seed_checkpoint(db_session, platform.id, CC, old)
        service = _service(db_session, engine)
        original = service.sync_service.sync_inquiries

        def _partial(connector, platform_id, start, end, *, source, **kwargs):
            if source == CC:
                return {"status": "PARTIAL_SUCCESS", "created": 1, "updated": 0, "failed": 1}
            return original(connector, platform_id, start, end, source=source, **kwargs)

        monkeypatch.setattr(service.sync_service, "sync_inquiries", _partial)

        result = service.sync_platform(_Connector(), platform.id)

        assert result["by_source"][CC]["status"] == "PARTIAL_SUCCESS"
        assert _covered(db_session, platform.id, CC) == old

    def test_unexpected_exception_rolls_back_and_reports_only_the_exception_class(
        self, db_session, engine, platform, monkeypatch
    ):
        db_session.commit()  # fixture가 flush만 한 platform 행을 보존한다(아래 rollback이 지우지 않도록)
        service = _service(db_session, engine)
        original = service.sync_service.sync_inquiries

        def _boom(connector, platform_id, start, end, *, source, **kwargs):
            if source == CC:
                raise TypeError("민감한 본문 010-1234-5678 이 예외 메시지에 섞여 있다")
            return original(connector, platform_id, start, end, source=source, **kwargs)

        monkeypatch.setattr(service.sync_service, "sync_inquiries", _boom)

        result = service.sync_platform(_Connector(pr_items=[_item("9")]), platform.id)

        assert result["by_source"][CC]["status"] == "FAILED"
        assert result["by_source"][CC]["reason_code"] == "INTERNAL_ERROR:TypeError"
        assert _covered(db_session, platform.id, CC) is None
        record = IntegrationStatusRepository(db_session).get_by_type_and_code(
            CHECKPOINT_INTEGRATION_TYPE, checkpoint_code(platform.id, CC)
        )
        assert record is not None
        assert "010" not in (record.last_error_message or "")
        # 다른 source는 영향받지 않는다.
        assert result["by_source"][PR]["status"] == "SUCCESS"
        assert _covered(db_session, platform.id, PR) == NOW

    def test_failure_on_first_segment_blocks_later_segments_of_same_source(self, db_session, engine, platform):
        """실패한 구간을 건너뛰어 더 최근 구간을 처리하면 그 사이 데이터가 영영 누락된다."""
        gap_start = kst_to_utc(2026, 9, 17, 0, 0)
        _seed_checkpoint(db_session, platform.id, CC, gap_start)
        connector = _Connector(cc_error=MarketplaceExternalAPIError("coupang", "SERVER_ERROR", True, http_status=500))

        result = _service(db_session, engine).sync_platform(connector, platform.id)

        assert connector.calls_for(CC) == [(date(2026, 9, 17), date(2026, 9, 23))]  # 첫 구간 하나만 시도
        assert result["by_source"][CC]["segments_done"] == 0
        assert _covered(db_session, platform.id, CC) == gap_start


# ---------------------------------------------------------------------------
# 7일 초과 공백 + 요청 예산
# ---------------------------------------------------------------------------


class TestLongGapAndRequestBudget:
    GAP_START = kst_to_utc(2026, 9, 17, 0, 0)  # 20일 전 -> 9/17..10/7 = 21일 = 구간 3개

    def _seed_both(self, db_session, platform):
        for source in (CC, PR):
            _seed_checkpoint(db_session, platform.id, source, self.GAP_START)

    def test_typical_cost_catches_up_oldest_first_and_stops_when_worst_case_no_longer_fits(
        self, db_session, engine, platform
    ):
        """구간 시작 조건이 "남은 예산 >= 그 source의 구간당 최악 요청 수"라서, 실제 비용이 작아도(콜센터
        4 + 상품별 1) 콜센터는 라운드 2번(사용 10 -> 남은 35 < 36)까지만 구간을 시작한다. 상품별은
        최악 9라 남은 예산이 충분해 세 구간 모두 처리한다. 남은 콜센터 구간은 다음 실행이 이어간다."""
        self._seed_both(db_session, platform)
        connector = _Connector()

        result = _service(db_session, engine).sync_platform(connector, platform.id)

        segments = [
            (date(2026, 9, 17), date(2026, 9, 23)),
            (date(2026, 9, 24), date(2026, 9, 30)),
            (date(2026, 10, 1), TODAY),
        ]
        assert connector.calls_for(CC) == segments[:2]  # 가장 오래된 것부터, 오늘로 건너뛰지 않는다
        assert connector.calls_for(PR) == segments
        assert result["by_source"][CC]["segments_done"] == 2
        assert result["by_source"][CC]["segments_pending"] == 1
        assert result["by_source"][PR]["segments_pending"] == 0
        assert connector.budgets[0].used == 2 * 4 + 3 * 1
        assert _covered(db_session, platform.id, CC) == kst_to_utc(2026, 10, 1, 0, 0)
        assert _covered(db_session, platform.id, PR) == NOW

        # 다음 15분 실행이 남은 콜센터 구간을 마무리하고 이후에는 오늘만 반복 조회한다.
        later = NOW + timedelta(minutes=15)
        connector2 = _Connector()
        result2 = _service(db_session, engine, later).sync_platform(connector2, platform.id)
        assert connector2.calls_for(CC) == [(date(2026, 10, 1), TODAY)]
        assert connector2.calls_for(PR) == [(TODAY, TODAY)]
        assert result2["by_source"][CC]["segments_pending"] == 0
        assert _covered(db_session, platform.id, CC) == later

    def test_worst_case_cost_processes_one_segment_per_run_and_resumes_next_run(self, db_session, engine, platform):
        self._seed_both(db_session, platform)
        connector = _Connector(cost_cc=36, cost_pr=9)  # 구간당 최악 45요청

        run1 = _service(db_session, engine, NOW).sync_platform(connector, platform.id)

        assert connector.budgets[0].used == 45  # 예산을 넘기지 않는다
        assert run1["by_source"][CC]["segments_done"] == 1
        assert run1["by_source"][CC]["segments_pending"] == 2
        assert run1["by_source"][PR]["segments_done"] == 1
        # checkpoint는 성공한 마지막 구간 끝까지만 - 오늘로 건너뛰지 않는다.
        assert _covered(db_session, platform.id, CC) == kst_to_utc(2026, 9, 24, 0, 0)
        assert _covered(db_session, platform.id, PR) == kst_to_utc(2026, 9, 24, 0, 0)

        connector2 = _Connector(cost_cc=36, cost_pr=9)
        run2 = _service(db_session, engine, NOW + timedelta(minutes=15)).sync_platform(connector2, platform.id)
        assert connector2.calls_for(CC) == [(date(2026, 9, 24), date(2026, 9, 30))]
        assert run2["by_source"][CC]["segments_pending"] == 1
        assert _covered(db_session, platform.id, CC) == kst_to_utc(2026, 10, 1, 0, 0)

        connector3 = _Connector(cost_cc=36, cost_pr=9)
        run3 = _service(db_session, engine, NOW + timedelta(minutes=30)).sync_platform(connector3, platform.id)
        assert connector3.calls_for(CC) == [(date(2026, 10, 1), TODAY)]
        assert run3["by_source"][CC]["segments_pending"] == 0
        assert _covered(db_session, platform.id, CC) == NOW + timedelta(minutes=30)

    def test_run_never_exceeds_the_budget_and_never_starts_a_segment_that_cannot_fit(
        self, db_session, engine, platform
    ):
        self._seed_both(db_session, platform)
        connector = _Connector(cost_cc=20, cost_pr=5)  # 구간당 25: 1구간 후 20 남음 < 콜센터 최악 36

        _service(db_session, engine).sync_platform(connector, platform.id)

        assert connector.budgets[0].used <= 45
        assert len(connector.calls_for(CC)) == 1  # 남은 20 < 36이라 두 번째 콜센터 구간은 시작하지 않는다
        assert connector.budgets[0].max_requests - connector.budgets[0].used >= 0

    def test_budget_exhausted_inside_a_segment_blocks_before_sending_and_keeps_checkpoint(
        self, db_session, engine, platform
    ):
        self._seed_both(db_session, platform)
        connector = _Connector(cost_cc=50)  # 한 구간이 예산(45)보다 많이 쓰려 한다 -> consume()이 전송 전에 차단

        result = _service(db_session, engine).sync_platform(connector, platform.id)

        assert result["by_source"][CC]["status"] == "FAILED"
        assert result["by_source"][CC]["reason_code"] == "REQUEST_BUDGET_EXCEEDED"
        assert connector.budgets[0].used == 45  # 46번째는 보내지 않았다
        assert _covered(db_session, platform.id, CC) == self.GAP_START
        # 남은 예산이 0이라 상품별 source는 시작하지 않고 다음 실행으로 미룬다(실패가 아님).
        assert result["by_source"][PR]["status"] == "DEFERRED"
        assert connector.calls_for(PR) == []
        assert _covered(db_session, platform.id, PR) == self.GAP_START

    def test_sources_alternate_so_one_source_cannot_starve_the_other(self, db_session, engine, platform):
        self._seed_both(db_session, platform)
        connector = _Connector(cost_cc=16, cost_pr=4)

        _service(db_session, engine).sync_platform(connector, platform.id)

        order = [src for src, _, _ in connector.calls]
        assert order[:2] == [CC, PR]  # 라운드 로빈

    def test_retry_attempts_are_counted_by_the_shared_budget(self, db_session, engine, platform):
        """재시도도 같은 예산을 소비한다 - 실제 커넥터가 시도마다 consume()하므로 retry 포함 요청수가
        예산 사용량에 그대로 반영되고, 한도 직전에서 전송 전 차단된다."""
        connector = _Connector(cost_cc=12, cost_pr=3)  # 쿼리마다 (1+재시도2)=3시도
        _service(db_session, engine).sync_platform(connector, platform.id)
        assert connector.budgets[0].used == 15


# ---------------------------------------------------------------------------
# 동시 실행 방지
# ---------------------------------------------------------------------------


class TestConcurrentExecutionGuard:
    def test_locked_source_reports_already_running_and_makes_no_calls_or_writes(self, db_session, engine, platform):
        connector = _Connector(cc_items=[_item("1")], pr_items=[_item("2")])
        with cs_sync_source_lock(platform.id, CC, engine) as held:
            assert held is True
            result = _service(db_session, engine).sync_platform(connector, platform.id)

        assert result["by_source"][CC]["status"] == "ALREADY_RUNNING"
        assert connector.calls_for(CC) == []
        assert CsCaseRepository(db_session).get_by_external(platform.id, CC, "1") is None
        assert _covered(db_session, platform.id, CC) is None
        # 다른 source는 영향받지 않고 정상 진행한다.
        assert result["by_source"][PR]["status"] == "SUCCESS"
        assert connector.calls_for(PR) == [(TODAY, TODAY)]

    def test_both_sources_locked_means_whole_run_already_running_without_any_call(self, db_session, engine, platform):
        connector = _Connector()
        with cs_sync_source_lock(platform.id, CC, engine), cs_sync_source_lock(platform.id, PR, engine):
            result = _service(db_session, engine).sync_platform(connector, platform.id)

        assert result["status"] == "ALREADY_RUNNING"
        assert connector.calls == []

    def test_lock_is_released_after_run_so_next_run_proceeds(self, db_session, engine, platform):
        service = _service(db_session, engine)
        service.sync_platform(_Connector(), platform.id)
        with cs_sync_source_lock(platform.id, CC, engine) as acquired:
            assert acquired is True

    def test_lock_is_released_even_when_a_source_fails(self, db_session, engine, platform):
        connector = _Connector(cc_error=MarketplaceExternalAPIError("coupang", "SERVER_ERROR", True, http_status=500))
        _service(db_session, engine).sync_platform(connector, platform.id)
        with cs_sync_source_lock(platform.id, CC, engine) as acquired:
            assert acquired is True

    def test_second_run_while_first_is_in_progress_is_rejected_and_first_unaffected(self, db_session, engine, platform):
        """진행 중인 실행 안에서(커넥터 호출 중) 겹치는 두 번째 실행은 외부 호출·쓰기 없이 거절된다."""
        inner: dict[str, Any] = {}

        class _Reentrant(_Connector):
            def fetch_inquiries(self, *a, **k):
                if "result" not in inner:
                    inner["connector"] = _Connector(cc_items=[_item("x")])
                    inner["result"] = _service(db_session, engine).sync_platform(inner["connector"], platform.id)
                return super().fetch_inquiries(*a, **k)

        outer = _Reentrant(cc_items=[_item("1")])
        _service(db_session, engine).sync_platform(outer, platform.id)

        assert inner["result"]["by_source"][CC]["status"] == "ALREADY_RUNNING"
        assert inner["connector"].calls_for(CC) == []
        assert CsCaseRepository(db_session).get_by_external(platform.id, CC, "x") is None
        assert CsCaseRepository(db_session).get_by_external(platform.id, CC, "1") is not None

    def test_locks_of_different_platforms_do_not_block_each_other(self, db_session, engine, platform, naver_platform):
        with cs_sync_source_lock(naver_platform.id, CC, engine) as held_other:
            assert held_other is True
            with cs_sync_source_lock(platform.id, CC, engine) as held:
                assert held is True

    def test_unknown_source_is_rejected_instead_of_silently_unlocked(self, engine):
        with pytest.raises(ValueError), cs_sync_source_lock(1, "UNKNOWN_SOURCE", engine):
            pass


# ---------------------------------------------------------------------------
# 재시작 직후 catch-up 판단
# ---------------------------------------------------------------------------


class TestStartupUpToDateCheck:
    def test_no_checkpoint_is_not_up_to_date(self, db_session, engine, platform):
        assert _service(db_session, engine).is_up_to_date(platform.id, list(SOURCES)) is False

    def test_recent_checkpoint_for_all_sources_is_up_to_date(self, db_session, engine, platform):
        for source in SOURCES:
            _seed_checkpoint(db_session, platform.id, source, NOW - timedelta(minutes=5))
        assert _service(db_session, engine).is_up_to_date(platform.id, list(SOURCES)) is True

    def test_one_stale_source_makes_it_not_up_to_date(self, db_session, engine, platform):
        _seed_checkpoint(db_session, platform.id, CC, NOW - timedelta(minutes=5))
        _seed_checkpoint(db_session, platform.id, PR, NOW - timedelta(hours=3))
        assert _service(db_session, engine).is_up_to_date(platform.id, list(SOURCES)) is False

    def test_checkpoint_older_than_interval_is_not_up_to_date(self, db_session, engine, platform):
        for source in SOURCES:
            _seed_checkpoint(db_session, platform.id, source, NOW - timedelta(minutes=16))
        assert _service(db_session, engine).is_up_to_date(platform.id, list(SOURCES)) is False

    def test_checkpoint_from_yesterday_is_not_up_to_date_even_if_within_interval(self, db_session, engine, platform):
        just_after_midnight = kst_to_utc(2026, 10, 7, 0, 5)
        for source in SOURCES:
            _seed_checkpoint(db_session, platform.id, source, kst_to_utc(2026, 10, 6, 23, 55))
        assert _service(db_session, engine, just_after_midnight).is_up_to_date(platform.id, list(SOURCES)) is False
