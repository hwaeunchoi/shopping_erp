"""
api/routers/operations.py
------------------------------
상용 ERP 확장(6단계) - 통합 운영 대시보드/실패 작업함 API.

권한(요구사항 11번 - 최소 구분):
  DASHBOARD_VIEW(기존 재사용) - 요약/시계열/연동상태/실패목록/실패상세 조회.
  SYSTEM_MONITOR_VIEW(기존 재사용) - scheduler 잡 상태 조회.
  OPERATIONS_RETRY(신규) - 실패 항목 선택 재처리.
  OPERATIONS_UNKNOWN_RESOLVE(신규) - UNKNOWN 명령 수동 해소.
조회 권한(DASHBOARD_VIEW/SYSTEM_MONITOR_VIEW)만 있고 위 두 신규 권한이 없는
사용자는 상태를 바꾸는 두 POST 엔드포인트를 호출할 수 없다 - 실제로 다른
403 응답을 받는다(테스트: tests/integration/test_api_operations.py).

목록(failures)과 통계(summary/timeseries)를 분리한다(요구사항 9번) - 대시보드가
새로고침될 때마다 실패 행 전체를 다시 가져오지 않는다. failures는 항상
limit/offset 페이지네이션과 안정적 정렬(id desc)을 쓴다 - 무제한 전체 조회를
허용하지 않는다(limit 상한 MAX_FAILURES_LIMIT).

개인정보/Secret 미노출: ExternalCommand 자체에 전화번호·주소·문의본문·자격증명이
없다(요약 문자열만 저장하는 모델 설계 - models/integration_sync.py 참고) - 이
라우터는 있는 필드를 그대로 노출할 뿐 별도 마스킹이 필요 없다. Authorization
헤더/DB 연결정보/원본 예외 stack trace는 애초에 이 라우터의 어떤 응답에도
담기지 않는다.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from api.deps import get_current_user, get_db, require_permission
from models.user import User
from repositories.integration_sync_repository import ExternalCommandRepository
from services.operations_dashboard_service import OperationsDashboardService
from services.operations_retry_service import OperationsRetryService, OperationsRetryValidationError

router = APIRouter(prefix="/api/operations", tags=["operations"])

MAX_FAILURES_LIMIT = 200


# --- 통계(조회 전용, 상태 변경 없음) ------------------------------------------


@router.get(
    "/summary", dependencies=[Depends(require_permission("DASHBOARD_VIEW"))], summary="운영 대시보드 핵심 KPI 요약"
)
def get_summary(db=Depends(get_db)) -> dict:
    return OperationsDashboardService(db).summary()


@router.get(
    "/timeseries",
    dependencies=[Depends(require_permission("DASHBOARD_VIEW"))],
    summary="최근 N일 명령 성공/실패 추이(일별)",
)
def get_timeseries(
    days: int = Query(default=7, ge=1, le=30),
    command_type: Optional[str] = None,
    platform_id: Optional[int] = None,
    db=Depends(get_db),
) -> list[dict]:
    return OperationsDashboardService(db).timeseries(days=days, command_type=command_type, platform_id=platform_id)


@router.get("/integrations", dependencies=[Depends(require_permission("DASHBOARD_VIEW"))], summary="플랫폼별 연동 상태")
def get_integrations(db=Depends(get_db)) -> list[dict]:
    rows = OperationsDashboardService(db).integrations_status()
    return [
        {
            "integration_type": r.integration_type,
            "integration_code": r.integration_code,
            "status": r.status,
            "last_success_at": r.last_success_at,
            "last_error_at": r.last_error_at,
            "last_error_message": r.last_error_message,
            "severity": r.severity,
        }
        for r in rows
    ]


@router.get(
    "/scheduler-jobs",
    dependencies=[Depends(require_permission("SYSTEM_MONITOR_VIEW"))],
    summary="scheduler 잡별 최근 실행 상태(잡 하나당 최신 1건)",
)
def get_scheduler_jobs(db=Depends(get_db)) -> list[dict]:
    rows = OperationsDashboardService(db).scheduler_jobs_status()
    return [
        {
            "target": r.target,
            "task_type": r.task_type,
            "status": r.status,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
            "error_message": r.error_message,
            "severity": r.severity,
        }
        for r in rows
    ]


# --- 통합 실패 작업함 ----------------------------------------------------------

# 명령종류 -> 상세화면 라우트(프론트엔드 경로). 개별 행까지 딥링크하지는 않는다
# (해당 화면에 command_id로 바로 스크롤하는 기능은 이번 단계 범위 밖) - 어떤
# 화면에서 이 실패를 다뤄야 하는지만 안내한다.
_DETAIL_LINK_BY_COMMAND_TYPE = {
    "SHIPMENT_SUBMIT": "/fulfillment",
    "PRODUCT_CREATE": "/products-bulk",
    "PRODUCT_OPTION_CREATE": "/products-bulk",
    "INVENTORY_UPDATE": "/products-bulk",
    "SALE_STATUS_UPDATE": "/products-bulk",
    "PRODUCT_INFO_UPDATE": "/products-bulk",
}


class FailureOut(BaseModel):
    id: int
    command_type: str
    platform_id: int
    platform_code: Optional[str]
    target_type: str
    target_id: int
    status: str
    attempt_count: int
    next_retry_at: Optional[datetime]
    error_code: Optional[str]
    created_at: datetime
    updated_at: datetime
    detail_link: str


def _to_failure_out(command) -> FailureOut:
    return FailureOut(
        id=command.id,
        command_type=command.command_type,
        platform_id=command.platform_id,
        platform_code=command.platform_code,
        target_type=command.target_type,
        target_id=command.target_id,
        status=command.status,
        attempt_count=command.attempt_count,
        next_retry_at=command.next_retry_at,
        error_code=command.error_code,
        created_at=command.created_at,
        updated_at=command.updated_at,
        detail_link=_DETAIL_LINK_BY_COMMAND_TYPE.get(command.command_type, "/system-monitoring"),
    )


class FailureListOut(BaseModel):
    items: list[FailureOut]
    total: int
    limit: int
    offset: int


@router.get(
    "/failures",
    response_model=FailureListOut,
    dependencies=[Depends(require_permission("DASHBOARD_VIEW"))],
    summary="통합 실패 작업함 목록(페이지네이션)",
    description="status를 지정하지 않으면 FAILED/RETRY_WAIT/UNKNOWN/RUNNING만 본다"
    "(PENDING/SUCCESS/CANCELLED는 실패가 아니므로 기본 목록에서 제외 - status를"
    " 명시하면 그 값 하나로만 조회 가능). limit은 최대 200으로 제한한다.",
)
def list_failures(
    command_type: Optional[str] = None,
    platform_id: Optional[int] = None,
    status_filter: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    limit: int = Query(default=50, ge=1, le=MAX_FAILURES_LIMIT),
    offset: int = Query(default=0, ge=0),
    db=Depends(get_db),
) -> FailureListOut:
    now = datetime.now(timezone.utc)
    if end_date is not None and end_date > now:
        end_date = now  # 미래 시각 필터는 지금 시각으로 자른다 - 아직 없는 데이터를 있는 것처럼 구간에 넣지 않는다.
    repo = ExternalCommandRepository(db)
    items = repo.list_failures(
        command_type=command_type,
        platform_id=platform_id,
        status=status_filter,
        created_from=start_date,
        created_to=end_date,
        limit=limit,
        offset=offset,
    )
    total = repo.count_failures(
        command_type=command_type,
        platform_id=platform_id,
        status=status_filter,
        created_from=start_date,
        created_to=end_date,
    )
    return FailureListOut(items=[_to_failure_out(c) for c in items], total=total, limit=limit, offset=offset)


class FailureDetailOut(FailureOut):
    request_summary: Optional[str]
    response_summary: Optional[str]
    available_unknown_actions: list[str]


class BulkRetryRequest(BaseModel):
    command_ids: list[int] = Field(min_length=1)


class BulkRetryItemOut(BaseModel):
    command_id: int
    outcome: str
    error_code: Optional[str] = None


# 아래 "/failures/bulk-retry"(정적 경로, POST)를 "/failures/{command_id}"(동적
# 경로)류보다 먼저 등록한다 - api/routers/cs_cases.py 모듈 docstring과 동일한
# 이유(등록 순서대로 매칭되고 타입 변환 실패 시 다음 라우트로 폴백하지 않음).
# 이 라우터는 실제로는 세그먼트 수가 서로 달라(bulk-retry 1개, {command_id} 뒤에
# 붙는 하위 경로들은 2개) 충돌하지 않지만, 향후 같은 깊이의 정적 경로가 추가될
# 때를 대비해 정적 경로를 먼저 두는 관례를 그대로 지킨다.
@router.post(
    "/failures/bulk-retry",
    response_model=list[BulkRetryItemOut],
    dependencies=[Depends(require_permission("OPERATIONS_RETRY"))],
    summary="선택한 FAILED 명령 대량 재처리",
    description="FAILED 상태 명령만 재처리한다. UNKNOWN은 이 API로 재처리되지 않는다"
    "(outcome=UNKNOWN_REQUIRES_RESOLUTION) - POST /failures/{id}/resolve-unknown으로"
    " 운영자가 직접 해소해야 한다. 한 번에 최대 50건.",
    responses={400: {"description": "선택 항목이 없거나 최대 개수(50건)를 초과했습니다."}},
)
def bulk_retry_failures(
    payload: BulkRetryRequest, db=Depends(get_db), current_user: User = Depends(get_current_user)
) -> list[BulkRetryItemOut]:
    service = OperationsRetryService(db)
    try:
        result = service.bulk_retry(payload.command_ids, current_user.id)
    except OperationsRetryValidationError as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    return [BulkRetryItemOut(command_id=i.command_id, outcome=i.outcome, error_code=i.error_code) for i in result.items]


@router.get(
    "/failures/{command_id}",
    response_model=FailureDetailOut,
    dependencies=[Depends(require_permission("DASHBOARD_VIEW"))],
    summary="실패 작업 상세",
    responses={404: {"description": "명령을 찾을 수 없습니다."}},
)
def get_failure_detail(command_id: int, db=Depends(get_db)) -> FailureDetailOut:
    repo = ExternalCommandRepository(db)
    command = repo.get_by_id(command_id)
    if command is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="명령을 찾을 수 없습니다.")
    base = _to_failure_out(command)
    actions = OperationsRetryService(db).list_unknown_actions(command_id)
    return FailureDetailOut(
        **base.model_dump(),
        request_summary=command.request_summary,
        response_summary=command.response_summary,
        available_unknown_actions=actions,
    )


@router.get(
    "/failures/{command_id}/unknown-actions",
    dependencies=[Depends(require_permission("DASHBOARD_VIEW"))],
    summary="UNKNOWN 명령에 대해 허용되는 해소 조치 목록",
)
def get_unknown_actions(command_id: int, db=Depends(get_db)) -> list[str]:
    return OperationsRetryService(db).list_unknown_actions(command_id)


class ResolveUnknownRequest(BaseModel):
    resolution: str
    evidence_note: str


@router.post(
    "/failures/{command_id}/resolve-unknown",
    response_model=FailureOut,
    dependencies=[Depends(require_permission("OPERATIONS_UNKNOWN_RESOLVE"))],
    summary="UNKNOWN 명령 수동 해소(외부 채널 확인 결과 반영)",
    description="resolution: CONFIRMED_NOT_SENT(채널 미반영 확인 - PENDING 복귀) / "
    "CONFIRMED_SUCCESS(채널 반영 확인 - SUCCESS 확정) / CONFIRMED_FAILED(채널 미반영 확정 - FAILED 확정). "
    "evidence_note(확인 근거)는 5자 이상 필수 - 감사로그에 그대로 남는다.",
    responses={400: {"description": "알 수 없는 해소 방식이거나 확인 근거가 없습니다."}},
)
def resolve_unknown_failure(
    command_id: int, payload: ResolveUnknownRequest, db=Depends(get_db), current_user: User = Depends(get_current_user)
) -> FailureOut:
    service = OperationsRetryService(db)
    try:
        command = service.resolve_unknown(command_id, payload.resolution, payload.evidence_note, current_user.id)
    except OperationsRetryValidationError as e:
        db.rollback()
        code = status.HTTP_404_NOT_FOUND if "찾을 수 없습니다" in str(e) else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=code, detail=str(e)) from e
    db.commit()
    return _to_failure_out(command)
