"""
services/alert_evaluation_service.py
-----------------------------------------
UI v1.0 알림센터: alert_rules(사용자 정의 규칙)을 현재 지표와 비교해
조건을 만족하면 notifications를 생성한다.

지원 metric(models.system.AlertRule 컬럼 주석 기준):
- ORDER_COUNT_TODAY: 오늘 주문건수
- AD_COST: 오늘 전체 광고비 합계
- ROAS: 오늘 전체 광고 ROAS(%)
- RETURN_RATE: 최근 7일 반품율(%)
- UNSHIPPED_DAYS: 현재 미배송(NEW/PREPARING) 주문건수
- API_FAILURE: 연동상태(integration_status)가 ERROR인 건수
- BACKUP_FAILURE: 가장 최근 백업이 FAILED면 1, 아니면 0

같은 규칙으로 아직 읽지 않은 알림이 있으면 중복 생성하지 않는다
(NotificationRepository.has_unread_for_rule).
"""

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from repositories.ad_repository import AdPerformanceRepository
from repositories.extra_repository import IntegrationStatusRepository
from repositories.order_repository import OrderRepository, ReturnRepository
from repositories.system_repository import AlertRuleRepository, BackupHistoryRepository, NotificationRepository
from services.notification_service import NotificationService

OPERATORS = {
    "LT": lambda value, threshold: value < threshold,
    "LTE": lambda value, threshold: value <= threshold,
    "GT": lambda value, threshold: value > threshold,
    "GTE": lambda value, threshold: value >= threshold,
    "EQ": lambda value, threshold: value == threshold,
}


class AlertEvaluationService:
    def __init__(self, session) -> None:
        self.session = session
        self.rule_repo = AlertRuleRepository(session)
        self.notification_repo = NotificationRepository(session)
        self.notification_service = NotificationService(session)
        self.order_repo = OrderRepository(session)
        self.return_repo = ReturnRepository(session)
        self.ad_perf_repo = AdPerformanceRepository(session)
        self.integration_status_repo = IntegrationStatusRepository(session)
        self.backup_repo = BackupHistoryRepository(session)

    def evaluate_all(self) -> list[int]:
        """도래한(조건 만족) 규칙에 대해 알림을 생성하고, 생성된 notification id 목록을 반환한다."""
        created_ids: list[int] = []
        for rule in self.rule_repo.list_enabled():
            value = self._compute_metric(rule.metric)
            if value is None or rule.threshold_value is None:
                continue
            compare = OPERATORS.get(rule.operator)
            if compare is None:
                continue
            if not compare(value, float(rule.threshold_value)):
                continue
            if self.notification_repo.has_unread_for_rule(rule.id):
                continue

            notification = self.notification_service.notify(
                type_=rule.metric,
                severity="CRITICAL" if rule.metric in ("API_FAILURE", "BACKUP_FAILURE") else "WARNING",
                message=f"[{rule.name}] {rule.metric} 값이 {value}로 조건({rule.operator} {rule.threshold_value})을 만족했습니다.",
                rule_id=rule.id,
            )
            created_ids.append(notification.id)
        self.session.flush()
        return created_ids

    def _compute_metric(self, metric: str) -> Optional[float]:
        today = date.today()
        if metric == "ORDER_COUNT_TODAY":
            start = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
            end = start + timedelta(days=1)
            return float(len(self.order_repo.list_by_date_range(start, end)))

        if metric == "AD_COST":
            perf = self.ad_perf_repo.list_by_date(today)
            return round(sum(float(p.cost) for p in perf), 2)

        if metric == "ROAS":
            perf = self.ad_perf_repo.list_by_date(today)
            cost = sum(float(p.cost) for p in perf)
            conversion = sum(float(p.conversion_amount) for p in perf)
            return round(conversion / cost * 100, 2) if cost > 0 else 0.0

        if metric == "RETURN_RATE":
            week_start = today - timedelta(days=7)
            total = self.order_repo.count_filtered(start_date=week_start, end_date=today)
            returned = self.return_repo.count_filtered(start_date=week_start, end_date=today)
            return round(returned / total * 100, 2) if total > 0 else 0.0

        if metric == "UNSHIPPED_DAYS":
            return float(len(self.order_repo.list_by_status("NEW")) + len(self.order_repo.list_by_status("PREPARING")))

        if metric == "API_FAILURE":
            statuses = self.integration_status_repo.list_all_status()
            return float(sum(1 for s in statuses if s.status == "ERROR"))

        if metric == "BACKUP_FAILURE":
            latest = self.backup_repo.list_recent(limit=1)
            return 1.0 if latest and latest[0].status == "FAILED" else 0.0

        return None
