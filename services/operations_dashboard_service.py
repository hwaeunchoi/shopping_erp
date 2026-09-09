"""
services/operations_dashboard_service.py
---------------------------------------------
상용 ERP 확장(6단계) - 통합 운영 대시보드 통계 조회 전용 서비스.

이 서비스는 데이터를 만들지 않는다 - 오직 기존 테이블을 조회해 요약할 뿐이다
(요구사항: "조회 API는 데이터 상태를 변경하지 않는다"). 새 집계 테이블도 만들지
않는다 - 전부 기존 테이블에 대한 조회/GROUP BY로 계산 가능해서다.

명령 단위 이력이 있는 도메인 vs 없는 도메인(중요한 구분):
- 송장 전송/상품 등록/옵션조합 등록/재고 전송/판매상태 전송/상품정보 수정
  (repositories.integration_sync_repository.WRITE_COMMAND_TYPES 6종)은 매 시도가
  ExternalCommand 행 하나로 남아 시도횟수/성공/실패/재시도대기/UNKNOWN을 명령
  단위로 정확히 집계할 수 있다 - "최근 24시간/7일 명령 성공률"은 이 6종만
  대상으로 한다.
- 주문 수집/클레임(취소·반품·교환) 수집/정산 수집/채널 상태 재조회/CS 문의
  수집은 읽기 전용 배치 작업이라(채널에 아무것도 쓰지 않음) 명령 단위 이력이
  없다 - 대신 플랫폼당 하나의 스냅샷(models.extra.IntegrationStatus, upsert만
  가능)과 잡 실행 단위 이력(models.extra.TaskExecutionHistory)만 있다. 이번
  단계에서 claim_sync_job/settlement_sync_job/channel_status_sync_job/
  cs_inquiry_sync_job에 IntegrationStatus 기록을 추가했다(order_collect_job/
  ad_collect_job/product_sync_job은 이미 기록하고 있었다 - 이 넷만 새로 맞춘
  것이지 그 잡들의 수집 로직 자체는 건드리지 않았다). 그 결과 "플랫폼별 마지막
  성공·실패 시각"은 7개 integration_type(MALL/AD/MALL_PRODUCT/CLAIM/SETTLEMENT/
  ORDER_STATUS_SYNC/CS_INQUIRY) 전체에서 나오지만, "성공/부분성공/실패"의
  "부분성공"은 CLAIM(클레임 3종 중 일부만 성공한 경우)에서만 실제로 관측되고
  나머지는 NORMAL/ERROR 이분법이다(각 잡의 기존 반환값이 부분성공을 구분해
  주는 경우만 IntegrationStatusRepository.upsert_partial을 쓴다 - 없는 잡에
  억지로 부분성공을 만들어내지 않는다).

시간 기준(요구사항 4번): 이 서비스의 모든 함수는 timezone-aware UTC datetime을
받고 UTC 경계로만 계산한다. 이 앱은 사용자별 timezone 설정이 없다(models.user.
User에 theme_preference는 있어도 timezone 컬럼은 없다) - 화면 표시를 위한
KST 변환은 프론트엔드가 필요하면 하되, API는 항상 UTC 기준값만 주고받는다.
"오늘"은 캘린더 개념이라 timezone에 따라 경계가 달라지는데, 이 서비스는 UTC
캘린더일(자정~다음 자정)을 "오늘"로 정의한다(KST는 UTC+9라 KST 자정은 UTC
전날 15시다 - 화면에 "오늘(UTC 기준)"이라고 명시해 혼동을 막는다). "최근
24시간"/"최근 7일"은 캘린더 경계가 아니라 now 기준 rolling window다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from config.settings import settings
from repositories.cs_case_repository import CsCaseRepository
from repositories.extra_repository import IntegrationStatusRepository, TaskExecutionHistoryRepository
from repositories.fulfillment_repository import FulfillmentBatchItemRepository
from repositories.integration_sync_repository import (
    WRITE_COMMAND_TYPES,
    ExternalCommandRepository,
    OrderStatusConflictRepository,
)
from repositories.order_repository import OrderRepository
from repositories.settlement_repository import SettlementRepository
from services.exchange_return_service import OrderRateService

# 출고 배치항목 10개 상태를 KPI 카드 4개(출고대기/피킹/검수/포장완료)로 묶는 매핑.
# 여기 없는 상태(SUBMIT_PENDING/SUBMITTED/BLOCKED/CANCELLED)는 raw_by_status에서
# 그대로 볼 수 있다 - 4개 카드는 "물리적 출고 준비 단계"만 다룬다(채널 송장 전송
# 이후 상태는 다른 카드(송장 전송 명령 상태)가 다룬다 - models/fulfillment.py의
# "3층 분리" 설계 원칙과 동일하게 이 서비스도 그 경계를 그대로 따른다).
FULFILLMENT_KPI_BUCKETS: dict[str, tuple[str, ...]] = {
    "READY_TO_PICK": ("READY",),
    "PICKING": ("PICKING", "PICKED"),
    "INSPECTING": ("VERIFYING", "VERIFIED"),
    "PACKED": ("PACKED",),
}

SEVERITY_INFO = "INFO"
SEVERITY_WARNING = "WARNING"
SEVERITY_ERROR = "ERROR"
SEVERITY_CRITICAL = "CRITICAL"


def utc_day_bounds(now: datetime) -> tuple[datetime, datetime]:
    """now가 속한 UTC 캘린더일의 [00:00, 다음날 00:00) 경계(naive UTC)를 돌려준다."""
    naive_now = now.astimezone(timezone.utc).replace(tzinfo=None) if now.tzinfo else now
    start = naive_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


@dataclass
class SuccessRate:
    success: int
    failed: int

    @property
    def total(self) -> int:
        return self.success + self.failed

    @property
    def rate_percent(self) -> Optional[float]:
        """분모(성공+실패 확정 건)가 0이면 None(화면에서 N/A로 표시) - 0%로
        꾸미지 않는다(요구사항 3번)."""
        if self.total == 0:
            return None
        return round(self.success / self.total * 100, 2)


@dataclass
class IntegrationRow:
    integration_type: str
    integration_code: str
    status: str
    last_success_at: Optional[datetime]
    last_error_at: Optional[datetime]
    last_error_message: Optional[str]
    severity: str


@dataclass
class SchedulerJobRow:
    target: Optional[str]
    task_type: str
    status: str
    started_at: datetime
    finished_at: Optional[datetime]
    error_message: Optional[str]
    severity: str


def classify_integration_severity(status: str, last_success_at: Optional[datetime], now: datetime) -> str:
    """오류 문자열 검색이 아니라 status 값(NORMAL/PARTIAL/ERROR/TOKEN_EXPIRING)과
    last_success_at 경과일수만으로 판정한다."""
    if status == "ERROR":
        if last_success_at is None:
            return SEVERITY_CRITICAL  # 한 번도 성공한 적이 없다 - 연동이 아예 안 되는 상태.
        if now - last_success_at >= timedelta(days=settings.ops_integration_down_after_days):
            return SEVERITY_CRITICAL  # 연동 전체 중단(장기간 성공 없음).
        return SEVERITY_ERROR
    if status in ("PARTIAL", "TOKEN_EXPIRING"):
        return SEVERITY_WARNING
    return SEVERITY_INFO  # NORMAL


class OperationsDashboardService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.command_repo = ExternalCommandRepository(session)
        self.integration_repo = IntegrationStatusRepository(session)
        self.task_history_repo = TaskExecutionHistoryRepository(session)
        self.order_repo = OrderRepository(session)
        self.fulfillment_item_repo = FulfillmentBatchItemRepository(session)
        self.cs_case_repo = CsCaseRepository(session)
        self.conflict_repo = OrderStatusConflictRepository(session)
        self.settlement_repo = SettlementRepository(session)

    # --- 핵심 KPI 요약 ----------------------------------------------------

    def summary(self, now: Optional[datetime] = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        today_start, today_end = utc_day_bounds(now)
        now_naive = _naive_utc(now)
        last_24h_start = now_naive - timedelta(hours=24)
        last_7d_start = now_naive - timedelta(days=7)

        alerts = OrderRateService(self.session).pending_alerts()
        fulfillment_raw = self.fulfillment_item_repo.count_by_status()
        fulfillment_buckets = {
            label: sum(fulfillment_raw.get(s, 0) for s in statuses)
            for label, statuses in FULFILLMENT_KPI_BUCKETS.items()
        }
        shipment_command_counts = self.command_repo.count_grouped_by_status(command_type="SHIPMENT_SUBMIT")
        product_command_counts: dict[str, int] = {}
        for ct in WRITE_COMMAND_TYPES:
            if ct == "SHIPMENT_SUBMIT":
                continue
            for status_key, count in self.command_repo.count_grouped_by_status(command_type=ct).items():
                product_command_counts[status_key] = product_command_counts.get(status_key, 0) + count
        cs_summary = self.cs_case_repo.count_by_status()
        rate_24h = self.command_repo.success_rate_window(last_24h_start, now_naive)
        rate_7d = self.command_repo.success_rate_window(last_7d_start, now_naive)

        return {
            "generated_at": now.isoformat(),
            "window_definition": {
                "today_utc": [today_start.isoformat() + "Z", today_end.isoformat() + "Z"],
                "last_24h_utc": [last_24h_start.isoformat() + "Z", now_naive.isoformat() + "Z"],
                "last_7d_utc": [last_7d_start.isoformat() + "Z", now_naive.isoformat() + "Z"],
                "note": "이 앱은 사용자별 timezone 설정이 없어 API는 항상 UTC 기준이다"
                "(docs/COMMERCIAL_ERP_ROADMAP.md 6단계 절 참고).",
            },
            "orders": {
                "collected_today": self.order_repo.count_created_between(today_start, today_end),
                "unshipped": alerts["unshipped_count"],
                "delayed_unshipped": alerts["delayed_unshipped_count"],
            },
            "fulfillment": {"by_kpi_bucket": fulfillment_buckets, "raw_by_status": fulfillment_raw},
            "shipment_commands_by_status": {
                s: shipment_command_counts.get(s, 0) for s in ("PENDING", "RUNNING", "RETRY_WAIT", "FAILED", "UNKNOWN")
            },
            "product_commands_by_status": {
                s: product_command_counts.get(s, 0) for s in ("PENDING", "RUNNING", "RETRY_WAIT", "FAILED", "UNKNOWN")
            },
            "order_status_conflicts_unresolved": self.conflict_repo.count_unresolved(),
            "cs": {
                "by_status": cs_summary,
                "unassigned": self.cs_case_repo.count_unassigned(),
                "overdue": self.cs_case_repo.count_overdue(now_naive),
            },
            "claims_pending": {
                "exchange": alerts["exchange_pending_count"],
                "return": alerts["return_pending_count"],
                "cancellation": alerts["cancellation_pending_count"],
            },
            "settlement_by_status": self.settlement_repo.count_by_status(),
            "success_rate": {
                "last_24h": _rate_dict(rate_24h),
                "last_7d": _rate_dict(rate_7d),
                "scope_note": "SHIPMENT_SUBMIT/PRODUCT_CREATE/PRODUCT_OPTION_CREATE/INVENTORY_UPDATE/"
                "SALE_STATUS_UPDATE/PRODUCT_INFO_UPDATE 6종 명령만 대상 - PENDING/RUNNING/"
                "RETRY_WAIT/UNKNOWN/CANCELLED는 분자·분모 모두 제외.",
            },
        }

    # --- 시계열(추이) ------------------------------------------------------

    def timeseries(
        self,
        days: int = 7,
        command_type: Optional[str] = None,
        platform_id: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        """최근 days일(UTC 캘린더일 단위)의 일별 성공/실패 명령 수 - created_at
        기준. 하루치 원본 행을 Python에서 날짜별로 묶는다(대상 기간이 최대
        7~30일로 제한돼 있어 방언별 date_trunc 차이를 신경 쓰지 않아도 될 만큼
        작다 - services 모듈 docstring "성능과 DB" 참고)."""
        now = now or datetime.now(timezone.utc)
        today_start, _ = utc_day_bounds(now)
        range_start = today_start - timedelta(days=days - 1)

        buckets: dict[date, dict[str, int]] = {
            (range_start + timedelta(days=i)).date(): {"success": 0, "failed": 0} for i in range(days)
        }
        for day_offset in range(days):
            day_start = range_start + timedelta(days=day_offset)
            day_end = day_start + timedelta(days=1)
            success, failed = self.command_repo.success_rate_window(
                day_start, day_end, command_type=command_type, platform_id=platform_id
            )
            buckets[day_start.date()] = {"success": success, "failed": failed}

        return [
            {"date": d.isoformat(), "success": v["success"], "failed": v["failed"]} for d, v in sorted(buckets.items())
        ]

    # --- 플랫폼별 연동 상태 -------------------------------------------------

    def integrations_status(self, now: Optional[datetime] = None) -> list[IntegrationRow]:
        now = now or datetime.now(timezone.utc)
        rows = self.integration_repo.list_all_status()
        result = []
        for r in rows:
            last_success_aware = r.last_success_at.replace(tzinfo=timezone.utc) if r.last_success_at else None
            result.append(
                IntegrationRow(
                    integration_type=r.integration_type,
                    integration_code=r.integration_code,
                    status=r.status,
                    last_success_at=r.last_success_at,
                    last_error_at=r.last_error_at,
                    last_error_message=r.last_error_message,
                    severity=classify_integration_severity(r.status, last_success_aware, now),
                )
            )
        return result

    # --- scheduler 잡 상태(잡별 최신 실행 1건) ------------------------------

    def scheduler_jobs_status(self, lookback_rows: int = 500) -> list[SchedulerJobRow]:
        """target(잡 이름)별 가장 최근 실행 1건만 남긴다. task_type은 여러 잡이
        공유하므로(예: settlement_sync/claim_sync/cs_inquiry_sync가 모두
        "FULL_SYNC") target으로 구분해야 한다(scheduler/scheduler.py의 _run_job
        참고). lookback_rows는 유한하게 최근 N건만 가져와 Python에서 잡별
        최신값으로 줄인다 - 잡 종류가 20개 미만으로 적어 이 정도로 충분하다."""
        rows = self.task_history_repo.list_recent(limit=lookback_rows)
        latest_by_target: dict[str, Any] = {}
        for row in rows:  # list_recent는 started_at desc 정렬 - 먼저 나온 것이 최신.
            key = row.target or f"__no_target_{row.id}"
            if key not in latest_by_target:
                latest_by_target[key] = row
        return [
            SchedulerJobRow(
                target=row.target,
                task_type=row.task_type,
                status=row.status,
                started_at=row.started_at,
                finished_at=row.finished_at,
                error_message=row.error_message,
                severity=SEVERITY_ERROR if row.status == "FAILED" else SEVERITY_INFO,
            )
            for row in sorted(latest_by_target.values(), key=lambda r: r.started_at, reverse=True)
        ]


def _rate_dict(rate: tuple[int, int]) -> dict[str, Any]:
    sr = SuccessRate(success=rate[0], failed=rate[1])
    return {"success": sr.success, "failed": sr.failed, "total": sr.total, "rate_percent": sr.rate_percent}
