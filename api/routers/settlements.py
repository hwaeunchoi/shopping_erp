"""
api/routers/settlements.py
------------------------------
정산 조회 + 상용 ERP 확장(2단계) 정산 회차/상세 수집 트리거·미매칭/차액 조회.
"""

import logging
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from api.deps import get_db, require_permission
from config.settings import settings
from integrations.malls import get_mall_connector
from repositories.platform_repository import PlatformRepository
from repositories.settlement_repository import SettlementDiscrepancyRepository, SettlementRepository
from services.settlement_sync_service import SettlementSyncService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/settlements", tags=["settlements"], dependencies=[Depends(require_permission("SETTLEMENT_VIEW"))]
)


class SettlementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    platform_id: int
    settlement_cycle: str
    settlement_type: Optional[str] = None
    status: str
    expected_amount: float
    settled_amount: float
    unsettled_amount: float


class SettlementDetailOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    gross_amount: float
    fee_amount: float
    net_amount: float
    sale_type: Optional[str] = None
    recognition_date: Optional[date] = None


@router.get(
    "",
    response_model=list[SettlementOut],
    summary="정산 목록 조회",
    description="platform_id를 지정하면 해당 플랫폼의 정산 건만, 지정하지 않으면 최근 순으로 " "최대 200건을 반환한다.",
)
def list_settlements(platform_id: Optional[int] = None, db: Session = Depends(get_db)) -> list:
    repo = SettlementRepository(db)
    return repo.list_by_platform(platform_id) if platform_id is not None else repo.list_all(limit=200)


@router.get(
    "/{settlement_id}/details",
    response_model=list[SettlementDetailOut],
    summary="정산 상세 내역 조회",
    description="정산 건에 포함된 주문별 정산 상세(매출/수수료/실지급액)를 조회한다. "
    "정산 건이 없으면 빈 목록을 반환한다.",
)
def list_settlement_details(settlement_id: int, db: Session = Depends(get_db)) -> list:
    return SettlementRepository(db).list_details(settlement_id)


class SettlementDiscrepancyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    platform_id: int
    settlement_id: Optional[int]
    order_id: Optional[int]
    reason: str
    expected_amount: Optional[Decimal]
    actual_amount: Optional[Decimal]
    diff_amount: Optional[Decimal]
    detail_summary: Optional[str]
    detected_at: datetime
    resolved_at: Optional[datetime]
    resolution: Optional[str]


@router.get(
    "/discrepancies",
    response_model=list[SettlementDiscrepancyOut],
    summary="정산 미매칭/차액 조회",
    description="주문/정산 매칭 실패(NO_MATCHING_ORDER/NO_MATCHING_SETTLEMENT)와 금액 불일치"
    "(AMOUNT_MISMATCH) 중 아직 해소되지 않은 건을 조회한다. platform_id를 지정하면 해당 "
    "플랫폼만 필터링한다.",
)
def list_settlement_discrepancies(platform_id: Optional[int] = None, db: Session = Depends(get_db)) -> list:
    return SettlementDiscrepancyRepository(db).list_unresolved(platform_id)


class SettlementSyncRequest(BaseModel):
    platform_id: int
    start_date: date
    end_date: date


class SettlementFeatureResult(BaseModel):
    """정산 기능(회차 요약/주문단위 상세)별 결과. 미지원과 결과 0건을 status로 구분한다."""

    status: str  # SUCCESS | UNSUPPORTED | FAILED
    count: int
    reason_code: Optional[str] = None
    retryable: Optional[bool] = None


class SettlementSyncResult(BaseModel):
    overall_status: str  # SUCCESS | PARTIAL | UNSUPPORTED | FAILED
    settlements: SettlementFeatureResult
    settlement_details: SettlementFeatureResult


_EXTERNAL_REASONS = {
    "AUTH_FAILED",
    "RATE_LIMITED",
    "SERVER_ERROR",
    "BAD_RESPONSE",
    "TIMEOUT",
    "CONNECT_FAILED",
    "TRANSPORT_ERROR",
    "PARSE_FAILED",
}


def _overall_status(settlements: dict, details: dict) -> str:
    statuses = [settlements["status"], details["status"]]
    if all(s == "UNSUPPORTED" for s in statuses):
        return "UNSUPPORTED"
    has_success = any(s == "SUCCESS" for s in statuses)
    has_failed = any(s == "FAILED" for s in statuses)
    if has_success and has_failed:
        return "PARTIAL"
    if has_success:
        return "SUCCESS"
    return "FAILED"


def _settlement_sync_http_status(overall: str, settlements: dict, details: dict) -> int:
    """api.routers.orders._claim_http_status와 동일한 원칙(구조화된 결과 -> HTTP 상태,
    전 기능 미지원을 200 성공으로 보이지 않게 한다)."""
    if overall in ("SUCCESS", "PARTIAL"):
        return status.HTTP_200_OK
    if overall == "UNSUPPORTED":
        return status.HTTP_501_NOT_IMPLEMENTED
    failed = [f for f in (settlements, details) if f["status"] == "FAILED"]
    codes = {f["reason_code"] for f in failed}
    if codes == {"CREDENTIAL_MISSING"}:
        return status.HTTP_409_CONFLICT
    if codes and codes <= _EXTERNAL_REASONS:
        return (
            status.HTTP_503_SERVICE_UNAVAILABLE if any(f["retryable"] for f in failed) else status.HTTP_502_BAD_GATEWAY
        )
    return status.HTTP_500_INTERNAL_SERVER_ERROR


@router.post(
    "/sync",
    response_model=SettlementSyncResult,
    summary="정산 회차/상세 수집",
    description="지정한 플랫폼에서 [start_date, end_date] 구간의 정산 회차 요약과 주문단위 상세를 "
    "capability 기준으로 수집하고 대사(reconciliation)한다. 기능별 결과(SUCCESS/UNSUPPORTED/FAILED)를 "
    "구조화해 반환하며, HTTP 상태는 전체 결과에 따라 200/409/501/502/503/500이 될 수 있으나 body 형식은 "
    "동일하다. settings.claims_settlement_sync_enabled가 False(기본값)면 커넥터를 만들지 않고 즉시 "
    "501(UNSUPPORTED, reason_code=FEATURE_DISABLED)로 응답한다.",
    responses={404: {"description": "플랫폼을 찾을 수 없습니다."}},
)
def sync_settlements(payload: SettlementSyncRequest, db: Session = Depends(get_db)) -> JSONResponse:
    if not settings.claims_settlement_sync_enabled:
        disabled = {"status": "UNSUPPORTED", "count": 0, "reason_code": "FEATURE_DISABLED", "retryable": False}
        disabled_result = {"overall_status": "UNSUPPORTED", "settlements": disabled, "settlement_details": disabled}
        return JSONResponse(status_code=status.HTTP_501_NOT_IMPLEMENTED, content=disabled_result)

    platform = PlatformRepository(db).get_by_id(payload.platform_id)
    if platform is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="플랫폼을 찾을 수 없습니다.")

    connector = get_mall_connector(platform.connector_class, session=db, platform_id=platform.id)
    sync_service = SettlementSyncService(db)
    settlements_result = sync_service.sync_settlements(connector, platform.id, payload.start_date, payload.end_date)
    details_result = sync_service.sync_settlement_details(connector, platform.id, payload.start_date, payload.end_date)
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.warning("정산 수집 커밋 실패: trace=%s", uuid.uuid4().hex[:8])
        failed = {"status": "FAILED", "count": 0, "reason_code": "DB_WRITE_FAILED", "retryable": False}
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"overall_status": "FAILED", "settlements": failed, "settlement_details": failed},
        )

    overall = _overall_status(settlements_result, details_result)
    result = {"overall_status": overall, "settlements": settlements_result, "settlement_details": details_result}
    return JSONResponse(
        status_code=_settlement_sync_http_status(overall, settlements_result, details_result), content=result
    )
