"""
services/stale_task_recovery_service.py
------------------------------------------
재시작·종료로 RUNNING 상태가 영원히 남은 scheduler 작업 이력(task_execution_history)을
"중단됨"으로 공식 정리한다.

배경: scheduler 작업은 시작할 때 status=RUNNING 행을 만들고 끝날 때 SUCCESS/FAILED로 바꾼다.
PC 종료·Docker 재시작·프로세스 강제 종료로 작업이 도중에 사라지면 그 행은 RUNNING으로 영원히
남는다(운영에서 3주 전 PRODUCT_SYNC 행이 실제로 발견됐다). 이 서비스는 scheduler 시작 시 한 번
그런 잔존 행만 정리한다.

정리 조건(전부 충족해야만 종료 처리):
1. status == "RUNNING" 이고 started_at이 settings.task_stale_running_threshold_minutes(기본 360분)보다 오래됨.
2. target이 호출자가 넘긴 scheduler job 목록에 있고 trigger_type이 SCHEDULE/CATCHUP (API가 만든 수동
   실행 이력은 건드리지 않는다).
3. 같은 작업의 실제 활성 실행이 없음 - advisory lock을 쓰는 작업(CS 문의 동기화·백업)은 잠금을
   실제로 누가 들고 있는지 확인해 들고 있으면 건드리지 않는다. 잠금이 없는 작업은 scheduler가 방금
   (재)시작한 시점이고 이 배포는 scheduler 인스턴스가 하나이므로(docker-compose) 임계시간을 넘긴
   RUNNING 행은 이전 프로세스의 잔존물이다.

기록: 기존 상태 계약(RUNNING/SUCCESS/FAILED)을 따라 status="FAILED", error_message=
"PROCESS_INTERRUPTED"(task_execution_history에는 error_code 컬럼이 없어 안전한 고정 코드 문자열을
error_message에 둔다), finished_at=정리 시각. 원본 오류 문자열·Secret·개인정보는 기록하지 않는다.
이 함수는 scheduler 프로세스 시작 시에만 호출된다(scheduler/scheduler.py main()).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from sqlalchemy.orm import Session

from config.settings import settings
from repositories.extra_repository import TaskExecutionHistoryRepository
from repositories.platform_repository import PlatformRepository
from services.cs_sync_lock import is_cs_sync_lock_held
from services.postgres_backup_service import is_backup_lock_held

logger = logging.getLogger(__name__)

PROCESS_INTERRUPTED = "PROCESS_INTERRUPTED"
RECOVERABLE_TRIGGER_TYPES = ("SCHEDULE", "CATCHUP")
_CS_TARGETS = frozenset({"cs_inquiry_sync", "cs_inquiry_catchup"})
_BACKUP_TARGETS = frozenset({"backup", "backup_catchup"})


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def make_target_active_probe(db: Session) -> Callable[[str], bool]:
    """advisory lock으로 "지금 실제로 실행 중인가"를 확인할 수 있는 작업만 판단한다."""

    def _probe(target: str) -> bool:
        if target in _CS_TARGETS:
            from services.cs_inquiry_catchup_service import SOURCES

            return any(
                is_cs_sync_lock_held(platform.id, source, db.get_bind())
                for platform in PlatformRepository(db).list_all()
                for source in SOURCES
            )
        if target in _BACKUP_TARGETS:
            return is_backup_lock_held(db.get_bind())
        return False

    return _probe


def recover_stale_running_tasks(
    db: Session,
    targets: Iterable[str],
    *,
    now: Optional[datetime] = None,
    threshold_minutes: Optional[int] = None,
    is_target_active: Optional[Callable[[str], bool]] = None,
) -> dict[str, int]:
    """조건을 모두 충족한 stale RUNNING 행만 FAILED/PROCESS_INTERRUPTED로 정리한다.
    반환: {"recovered": n, "skipped_active": n}. 호출자가 commit한다."""
    current = now if now is not None else _utcnow_naive()
    minutes = settings.task_stale_running_threshold_minutes if threshold_minutes is None else threshold_minutes
    cutoff = current - timedelta(minutes=minutes)
    probe = is_target_active if is_target_active is not None else make_target_active_probe(db)

    repo = TaskExecutionHistoryRepository(db)
    recovered = 0
    skipped_active = 0
    for history in repo.list_stale_running(cutoff, list(targets), RECOVERABLE_TRIGGER_TYPES):
        target = history.target or ""
        if probe(target):
            skipped_active += 1
            continue
        repo.finish(history, status="FAILED", error_message=PROCESS_INTERRUPTED)
        recovered += 1
    if recovered or skipped_active:
        logger.info("stale RUNNING 작업 정리: recovered=%s skipped_active=%s", recovered, skipped_active)
    return {"recovered": recovered, "skipped_active": skipped_active}
