"""
api/routers/reports.py
---------------------------
종합 보고서(SRS 3.7 FR-REPORT-01~04) 조회/다운로드 + 예약 보고서(report_schedules) 관리.

월별 경영보고서(FR-REPORT-03)는 ReportService가 조합하고, XLSX/PDF
내보내기(FR-REPORT-04)는 services/export_service.py의 순수 함수를 사용한다.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.deps import get_db, require_permission
from models.extra import ReportSchedule
from repositories.extra_repository import ReportScheduleRepository
from services.export_service import monthly_report_to_excel, monthly_report_to_pdf
from services.report_service import ReportService

router = APIRouter(prefix="/api/reports", tags=["reports"], dependencies=[Depends(require_permission("REPORT_VIEW"))])


class ProfitLossOut(BaseModel):
    gross_revenue: float
    net_revenue: float
    order_count: int
    ad_cost: float
    ad_conversion_revenue: float
    cost_of_goods: float
    platform_fee: float
    shipping_cost: float
    packaging_cost: float
    other_cost: float
    total_cost: float
    net_profit: float
    net_profit_rate: float


class PlatformBreakdownOut(BaseModel):
    platform_id: int
    platform_code: str
    platform_name: str
    revenue: float
    order_count: int


class ProductPerformanceOut(BaseModel):
    product_option_id: int
    sales_qty: int
    revenue: float
    net_profit: float
    ad_cost: float
    roas: float


class AdPlatformPerformanceOut(BaseModel):
    ad_platform_code: str
    impressions: int
    clicks: int
    cost: float
    conversions: int
    conversion_amount: float
    cpc: float
    cpm: float
    roas: float


class KpiComparisonOut(BaseModel):
    metric: str
    target_value: float
    actual_value: float
    achievement_rate: float


class CostTypeBreakdownOut(BaseModel):
    fixed_cost: float
    variable_cost: float


class MonthlyReportOut(BaseModel):
    period_key: str
    profit_loss: ProfitLossOut
    platform_breakdown: list[PlatformBreakdownOut]
    best_products: list[ProductPerformanceOut]
    worst_products: list[ProductPerformanceOut]
    ad_performance: list[AdPlatformPerformanceOut]
    return_rate: float
    exchange_rate: float
    cancel_rate: float
    cost_type_breakdown: CostTypeBreakdownOut
    kpi_comparisons: list[KpiComparisonOut]


def _to_monthly_report_out(report) -> MonthlyReportOut:
    pl = report.profit_loss
    return MonthlyReportOut(
        period_key=report.period_key,
        profit_loss=ProfitLossOut(
            gross_revenue=float(pl.gross_revenue),
            net_revenue=float(pl.net_revenue),
            order_count=pl.order_count,
            ad_cost=float(pl.ad_cost),
            ad_conversion_revenue=float(pl.ad_conversion_revenue),
            cost_of_goods=float(pl.cost_of_goods),
            platform_fee=float(pl.platform_fee),
            shipping_cost=float(pl.shipping_cost),
            packaging_cost=float(pl.packaging_cost),
            other_cost=float(pl.other_cost),
            total_cost=float(pl.total_cost),
            net_profit=float(pl.net_profit),
            net_profit_rate=float(pl.net_profit_rate),
        ),
        platform_breakdown=[PlatformBreakdownOut(**vars(p)) for p in report.platform_breakdown],
        best_products=[
            ProductPerformanceOut(
                product_option_id=p.product_option_id,
                sales_qty=p.sales_qty,
                revenue=float(p.revenue),
                net_profit=float(p.net_profit),
                ad_cost=float(p.ad_cost),
                roas=float(p.roas),
            )
            for p in report.best_products
        ],
        worst_products=[
            ProductPerformanceOut(
                product_option_id=p.product_option_id,
                sales_qty=p.sales_qty,
                revenue=float(p.revenue),
                net_profit=float(p.net_profit),
                ad_cost=float(p.ad_cost),
                roas=float(p.roas),
            )
            for p in report.worst_products
        ],
        ad_performance=[AdPlatformPerformanceOut(**vars(a)) for a in report.ad_performance],
        return_rate=report.return_rate,
        exchange_rate=report.exchange_rate,
        cancel_rate=report.cancel_rate,
        cost_type_breakdown=CostTypeBreakdownOut(**vars(report.cost_type_breakdown)),
        kpi_comparisons=[KpiComparisonOut(**vars(k)) for k in report.kpi_comparisons],
    )


@router.get(
    "/monthly",
    response_model=MonthlyReportOut,
    summary="월별 경영보고서 조회",
    description="SRS FR-REPORT-03: 지정한 연/월의 손익, 플랫폼별 매출, 베스트/워스트 상품, "
    "광고성과, 반품/교환/취소율, KPI 달성률을 계산해 반환한다.",
)
def get_monthly_report(year: int, month: int, db: Session = Depends(get_db)) -> MonthlyReportOut:
    report = ReportService(db).generate_monthly_report(year, month)
    db.commit()
    return _to_monthly_report_out(report)


@router.get(
    "/monthly/excel",
    summary="월별 경영보고서 엑셀 다운로드",
    description="SRS FR-REPORT-04: 월별 경영보고서를 시트별(손익요약/플랫폼별매출/베스트상품/워스트상품/"
    "광고성과보고서/KPI)로 나눈 xlsx 파일로 다운로드한다.",
)
def download_monthly_report_excel(year: int, month: int, db: Session = Depends(get_db)) -> StreamingResponse:
    report = ReportService(db).generate_monthly_report(year, month)
    db.commit()
    buffer = monthly_report_to_excel(report)
    filename = f"monthly_report_{report.period_key}.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get(
    "/monthly/pdf",
    summary="월별 경영보고서 PDF 다운로드",
    description="SRS FR-REPORT-04: 손익/플랫폼별 분석/광고성과/상품별 분석/KPI 요약을 포함한 "
    "월별 경영보고서 PDF를 다운로드한다.",
)
def download_monthly_report_pdf(year: int, month: int, db: Session = Depends(get_db)) -> StreamingResponse:
    report = ReportService(db).generate_monthly_report(year, month)
    db.commit()
    buffer = monthly_report_to_pdf(report)
    filename = f"monthly_report_{report.period_key}.pdf"
    return StreamingResponse(
        buffer, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


class ReportScheduleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    report_type: str
    frequency: str
    output_format: str
    recipient_emails: Optional[str]
    is_enabled: bool
    next_run_at: datetime
    last_run_at: Optional[datetime]
    created_by: Optional[int]
    created_at: datetime


class ReportScheduleCreate(BaseModel):
    report_type: str
    frequency: str
    output_format: str
    recipient_emails: Optional[str] = None
    next_run_at: datetime
    is_enabled: bool = True


class ReportScheduleUpdate(BaseModel):
    frequency: Optional[str] = None
    output_format: Optional[str] = None
    recipient_emails: Optional[str] = None
    is_enabled: Optional[bool] = None
    next_run_at: Optional[datetime] = None


@router.get(
    "/schedules",
    response_model=list[ReportScheduleOut],
    summary="예약 보고서 목록 조회",
    description="등록된 예약 보고서(report_schedules) 전체 목록을 반환한다.",
)
def list_report_schedules(db: Session = Depends(get_db)) -> list:
    return ReportScheduleRepository(db).list_all(limit=200)


@router.post(
    "/schedules",
    response_model=ReportScheduleOut,
    status_code=status.HTTP_201_CREATED,
    summary="예약 보고서 등록",
    description="정기적으로 자동 생성/발송할 보고서를 예약한다. 실제 생성/발송은 스케줄러의 "
    "report_generate_job이 next_run_at 도래 시 처리한다.",
)
def create_report_schedule(payload: ReportScheduleCreate, db: Session = Depends(get_db)) -> ReportSchedule:
    schedule = ReportSchedule(**payload.model_dump(), created_at=datetime.now(timezone.utc))
    ReportScheduleRepository(db).add(schedule)
    db.commit()
    return schedule


@router.patch(
    "/schedules/{schedule_id}",
    response_model=ReportScheduleOut,
    summary="예약 보고서 수정",
    description="주기/형식/수신자/활성화 여부/다음 실행 시각을 수정한다.",
    responses={404: {"description": "예약 보고서를 찾을 수 없습니다."}},
)
def update_report_schedule(
    schedule_id: int, payload: ReportScheduleUpdate, db: Session = Depends(get_db)
) -> ReportSchedule:
    repo = ReportScheduleRepository(db)
    schedule = repo.get_by_id(schedule_id)
    if schedule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="예약 보고서를 찾을 수 없습니다.")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(schedule, field, value)
    db.commit()
    return schedule


@router.delete(
    "/schedules/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="예약 보고서 삭제",
    responses={404: {"description": "예약 보고서를 찾을 수 없습니다."}},
)
def delete_report_schedule(schedule_id: int, db: Session = Depends(get_db)) -> None:
    repo = ReportScheduleRepository(db)
    schedule = repo.get_by_id(schedule_id)
    if schedule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="예약 보고서를 찾을 수 없습니다.")
    repo.delete(schedule)
    db.commit()
