"""
api/routers/system_monitor.py
----------------------------------
UI v1.1 4장 시스템 모니터링(탭 3종: 연동상태/작업이력/시스템상태).
"""

from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.deps import get_db, require_permission
from repositories.extra_repository import IntegrationStatusRepository, TaskExecutionHistoryRepository
from services.system_monitor_service import SystemMonitorService

router = APIRouter(
    prefix="/api/system-monitor",
    tags=["system-monitor"],
    dependencies=[Depends(require_permission("SYSTEM_MONITOR_VIEW"))],
)


class IntegrationStatusOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    integration_type: str
    integration_code: str
    status: str
    last_success_at: Optional[datetime]
    last_error_at: Optional[datetime]
    last_error_message: Optional[str]
    token_expires_at: Optional[datetime]
    updated_at: datetime


class TaskExecutionHistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_type: str
    target: Optional[str]
    trigger_type: str
    triggered_by: Optional[int]
    status: str
    started_at: datetime
    finished_at: Optional[datetime]
    result_summary: Optional[str]
    error_message: Optional[str]


class LatestBackupOut(BaseModel):
    created_at: datetime
    status: str
    file_size_bytes: Optional[int]


class SystemStatusOut(BaseModel):
    db_size_bytes: int
    log_dir_size_bytes: int
    latest_backup: Optional[LatestBackupOut]
    uptime_seconds: float


@router.get(
    "/integrations",
    response_model=list[IntegrationStatusOut],
    summary="연동상태 탭 조회",
    description="쇼핑몰/광고 플랫폼별 최근 연동 상태(정상/오류/토큰만료임박) 스냅샷을 반환한다.",
)
def list_integration_status(db: Session = Depends(get_db)) -> list:
    return IntegrationStatusRepository(db).list_all_status()


@router.get(
    "/task-history",
    response_model=list[TaskExecutionHistoryOut],
    summary="작업이력 탭 조회",
    description="유형(task_type)/상태(status)/기간으로 필터링한 작업 실행 이력을 최신순으로 반환한다.",
)
def list_task_history(
    task_type: Optional[str] = None,
    status_filter: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    db: Session = Depends(get_db),
) -> list:
    start_dt = datetime.combine(start_date, datetime.min.time()) if start_date else None
    end_dt = datetime.combine(end_date, datetime.min.time()) + timedelta(days=1) if end_date else None
    return TaskExecutionHistoryRepository(db).list_recent(
        task_type=task_type, status=status_filter, start_date=start_dt, end_date=end_dt
    )


@router.get(
    "/status",
    response_model=SystemStatusOut,
    summary="시스템상태 탭 조회",
    description="DB/로그 용량(실시간 조회), 최근 백업 결과, 서버 uptime을 반환한다.",
)
def get_system_status(db: Session = Depends(get_db)) -> SystemStatusOut:
    status_data = SystemMonitorService(db).get_system_status()
    return SystemStatusOut(
        db_size_bytes=status_data.db_size_bytes,
        log_dir_size_bytes=status_data.log_dir_size_bytes,
        latest_backup=(
            LatestBackupOut(
                created_at=status_data.latest_backup.created_at,
                status=status_data.latest_backup.status,
                file_size_bytes=status_data.latest_backup.file_size_bytes,
            )
            if status_data.latest_backup
            else None
        ),
        uptime_seconds=status_data.uptime_seconds,
    )
