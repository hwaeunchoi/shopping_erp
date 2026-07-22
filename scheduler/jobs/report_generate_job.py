"""
scheduler/jobs/report_generate_job.py
------------------------------------------
예약 보고서(report_schedules)를 확인해 next_run_at이 도래한 건을 처리한다.
SRS FR-REPORT-03(월별 경영보고서)만 구현되어 있으므로, 도래한 예약 건 중
frequency="MONTHLY"인 건만 "직전 달"의 월별 경영보고서를 생성해
output_format(XLSX/PDF)에 맞춰 settings.reports_dir/generated에 저장한다.

frequency가 DAILY/WEEKLY인 예약은 대응하는 계산엔진(일별/주별 손익 집계)이
아직 없으므로 이번 실행에서는 건드리지 않고 건너뛴다(잘못된 기간의 보고서를
만들어 내지 않기 위함) - next_run_at도 그대로 두어 해당 계산엔진이 준비된
뒤에 정상 처리되도록 한다.

recipient_emails로의 실제 이메일 발송은 이번 범위에 포함하지 않는다(파일
생성까지만 - 발송 채널 연동은 SMTP/알림센터 설계가 확정된 뒤 별도 작업).
"""

from datetime import datetime, timedelta, timezone

from config.settings import settings
from core.database import session_scope
from repositories.extra_repository import ReportScheduleRepository
from services.export_service import monthly_report_to_excel, monthly_report_to_pdf
from services.report_service import ReportService


def _previous_month(now: datetime) -> tuple[int, int]:
    first_of_this_month = now.replace(day=1)
    last_day_prev_month = first_of_this_month - timedelta(days=1)
    return last_day_prev_month.year, last_day_prev_month.month


def _advance_next_run(current: datetime, frequency: str) -> datetime:
    if frequency == "DAILY":
        return current + timedelta(days=1)
    if frequency == "WEEKLY":
        return current + timedelta(days=7)
    if frequency == "MONTHLY":
        year, month = current.year, current.month
        if month == 12:
            return current.replace(year=year + 1, month=1)
        return current.replace(month=month + 1)
    return current + timedelta(days=1)


def run() -> dict[str, object]:
    now = datetime.now(timezone.utc)
    generated_files: list[str] = []

    with session_scope() as db:
        schedule_repo = ReportScheduleRepository(db)
        due_schedules = [s for s in schedule_repo.list_due(now) if s.frequency == "MONTHLY"]
        if not due_schedules:
            return {"processed": 0, "generated_files": generated_files}

        report_service = ReportService(db)
        year, month = _previous_month(now)
        report = report_service.generate_monthly_report(year, month)

        output_dir = settings.reports_dir / "generated"
        output_dir.mkdir(parents=True, exist_ok=True)

        for schedule in due_schedules:
            if schedule.output_format == "PDF":
                buffer = monthly_report_to_pdf(report)
                ext = "pdf"
            else:
                buffer = monthly_report_to_excel(report)
                ext = "xlsx"

            filename = f"report_schedule_{schedule.id}_{report.period_key}.{ext}"
            file_path = output_dir / filename
            file_path.write_bytes(buffer.getvalue())
            generated_files.append(str(file_path))

            schedule.last_run_at = now
            schedule.next_run_at = _advance_next_run(schedule.next_run_at, schedule.frequency)

    return {"processed": len(generated_files), "generated_files": generated_files}
