"""
api/routers/tasks.py
-------------------------
UI 와이어프레임 v1.1 3장: 대시보드 빠른실행 버튼 5종(주문수집/광고수집/
보고서생성/백업/전체동기화)의 수동 트리거 API.

scheduler/jobs/*.run()은 이미 순수 함수(세션을 스스로 열고 닫는)이므로
스케줄러 프로세스와 이 API가 동일한 잡 구현을 그대로 재사용한다 - 자동
실행(SCHEDULE)과 수동 실행(MANUAL)의 유일한 차이는 trigger_type/
triggered_by뿐이다.
"""

from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.deps import get_current_user, get_db
from models.user import User
from repositories.extra_repository import TaskExecutionHistoryRepository
from repositories.user_repository import PermissionRepository
from scheduler.jobs import ad_collect_job, backup_job, customer_stats_job, order_collect_job, settlement_sync_job
from services.report_service import ReportService

router = APIRouter(prefix="/api/tasks", tags=["tasks"], dependencies=[Depends(get_current_user)])

TaskType = Literal["ORDER_COLLECT", "AD_COLLECT", "REPORT_GENERATE", "BACKUP", "FULL_SYNC"]

# 빠른실행 버튼별로 필요한 권한 - 화면별 쓰기 권한을 그대로 재사용한다
# (새 권한 코드를 추가하지 않고 기존 ORDER_EDIT/AD_MANAGE/REPORT_VIEW/SETTINGS_MANAGE를 따른다).
TASK_TYPE_PERMISSION: dict[str, str] = {
    "ORDER_COLLECT": "ORDER_EDIT",
    "AD_COLLECT": "AD_MANAGE",
    "REPORT_GENERATE": "REPORT_VIEW",
    "BACKUP": "SETTINGS_MANAGE",
    "FULL_SYNC": "ORDER_EDIT",
}


class TaskTriggerIn(BaseModel):
    task_type: TaskType


class TaskTriggerOut(BaseModel):
    id: int
    task_type: str
    status: str
    result_summary: Optional[str]
    error_message: Optional[str]


def _run_task(task_type: str, db: Session) -> object:
    if task_type == "ORDER_COLLECT":
        return order_collect_job.run()
    if task_type == "AD_COLLECT":
        return ad_collect_job.run()
    if task_type == "BACKUP":
        return backup_job.run()
    if task_type == "REPORT_GENERATE":
        today = date.today()
        report = ReportService(db).generate_monthly_report(today.year, today.month)
        db.commit()
        return {"period_key": report.period_key, "net_profit": float(report.profit_loss.net_profit)}
    # FULL_SYNC: 주문/광고 수집 + 정산 확인 + 고객 통계 재계산을 순서대로 실행한다.
    return {
        "order_collect": order_collect_job.run(),
        "ad_collect": ad_collect_job.run(),
        "settlement_sync": settlement_sync_job.run(),
        "customer_stats": customer_stats_job.run(),
    }


@router.post(
    "/trigger",
    response_model=TaskTriggerOut,
    summary="빠른실행 트리거",
    description="대시보드 빠른실행 버튼 5종(주문수집/광고수집/보고서생성/백업/전체동기화)을 즉시 실행하고 "
    "task_execution_history에 trigger_type=MANUAL로 기록한다. task_type별로 필요 권한이 다르다 "
    "(ORDER_COLLECT/FULL_SYNC=ORDER_EDIT, AD_COLLECT=AD_MANAGE, REPORT_GENERATE=REPORT_VIEW, BACKUP=SETTINGS_MANAGE).",
    responses={403: {"description": "해당 작업에 필요한 권한이 없습니다."}},
)
def trigger_task(
    payload: TaskTriggerIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> TaskTriggerOut:
    required_permission = TASK_TYPE_PERMISSION[payload.task_type]
    granted = {p.code for p in PermissionRepository(db).list_by_role(current_user.role_id)}
    if required_permission not in granted:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=f"이 작업을 실행할 권한이 없습니다: {required_permission}"
        )

    history_repo = TaskExecutionHistoryRepository(db)
    history = history_repo.start(task_type=payload.task_type, trigger_type="MANUAL", triggered_by=current_user.id)
    db.commit()

    try:
        result = _run_task(payload.task_type, db)
        history_repo.finish(history, status="SUCCESS", result_summary=str(result))
        db.commit()
    except Exception as e:
        db.rollback()
        history_repo.finish(history, status="FAILED", error_message=str(e))
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"작업 실행 중 오류가 발생했습니다: {e}"
        ) from e

    return TaskTriggerOut(
        id=history.id,
        task_type=history.task_type,
        status=history.status,
        result_summary=history.result_summary,
        error_message=history.error_message,
    )
