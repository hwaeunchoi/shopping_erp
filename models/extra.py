"""
models/extra.py
-----------------
v1.0 부가기능 + v1.1 실무효율 + v1.2 확장 기능 테이블 모음:
memos, attachments, recent_views, favorites, task_execution_history,
integration_status, report_schedules, import_export_jobs, audit_logs,
ai_analysis_results

memos/attachments/recent_views/favorites는 target_type+target_id 조합의
다형성(polymorphic) 패턴을 사용하여 여러 엔티티에 재사용한다 (FK를 직접
걸지 않고 애플리케이션 레벨에서 검증).

v1.2 권장 개선사항 반영:
- audit_logs.request_id: 하나의 요청에서 발생한 여러 변경을 작업 단위로 묶어 조회
- task_execution_history.progress_percent/processed_count/total_count: 진행률 표시
- import_export_jobs.storage_type: LOCAL/NAS/S3 저장소 추상화 대비
- report_schedules.recipient_emails: 초기엔 TEXT 유지, 채널 확장 시
  report_recipients(1:N) 테이블로 이관 예정 (설계 방침, 현재 미생성)
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class Memo(Base):
    """메모 (다형성: ORDER/PRODUCT/CUSTOMER)."""

    __tablename__ = "memos"
    __table_args__ = (Index("idx_memos_target", "target_type", "target_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[int] = mapped_column(nullable=False)
    content: Mapped[str] = mapped_column(String(2000), nullable=False)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Attachment(Base):
    """첨부파일 (다형성: ORDER/PRODUCT/CUSTOMER/RETURN/EXCHANGE)."""

    __tablename__ = "attachments"
    __table_args__ = (Index("idx_attachments_target", "target_type", "target_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[int] = mapped_column(nullable=False)
    file_path: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    uploaded_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class RecentView(Base):
    """최근 본 항목 (v1.1). 동일 대상 재열람 시 viewed_at만 갱신."""

    __tablename__ = "recent_views"
    __table_args__ = (
        Index("idx_recent_views_user", "user_id", "viewed_at"),
        UniqueConstraint("user_id", "target_type", "target_id", name="uq_recent_view"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)  # ORDER/PRODUCT/CUSTOMER
    target_id: Mapped[int] = mapped_column(nullable=False)
    viewed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Favorite(Base):
    """즐겨찾기 (v1.1). PRODUCT/REPORT/CUSTOMER 등."""

    __tablename__ = "favorites"
    __table_args__ = (UniqueConstraint("user_id", "target_type", "target_id", name="uq_favorite"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class TaskExecutionHistory(Base):
    """작업 실행 이력 (v1.1, v1.2 진행률 필드 추가). 주문수집/광고수집/백업/보고서생성/Import 공통."""

    __tablename__ = "task_execution_history"
    __table_args__ = (Index("idx_task_exec_started", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_type: Mapped[str] = mapped_column(
        String(30), nullable=False
    )  # ORDER_COLLECT/AD_COLLECT/BACKUP/REPORT_GENERATE/FULL_SYNC/IMPORT/EXPORT
    target: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    trigger_type: Mapped[str] = mapped_column(String(10), nullable=False)  # SCHEDULE/MANUAL
    triggered_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # RUNNING/SUCCESS/FAILED
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    result_summary: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)
    # v1.2 권장 개선사항: 진행률 표시
    progress_percent: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)
    processed_count: Mapped[Optional[int]] = mapped_column(nullable=True)
    total_count: Mapped[Optional[int]] = mapped_column(nullable=True)


class IntegrationStatus(Base):
    """플랫폼/광고 연동 상태 스냅샷 (v1.1). 수집 작업 종료 시 갱신된다."""

    __tablename__ = "integration_status"
    __table_args__ = (UniqueConstraint("integration_type", "integration_code", name="uq_integration_status"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    integration_type: Mapped[str] = mapped_column(String(20), nullable=False)  # MALL/AD
    integration_code: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # NORMAL/ERROR/TOKEN_EXPIRING
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error_message: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ReportSchedule(Base):
    """예약 보고서 (v1.2). recipient_emails는 콤마구분 문자열, 향후 채널 확장 시 별도 테이블로 이관 예정."""

    __tablename__ = "report_schedules"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    report_type: Mapped[str] = mapped_column(String(30), nullable=False)  # ORDER_LIST/SALES_REPORT/PROFIT_REPORT 등
    frequency: Mapped[str] = mapped_column(String(10), nullable=False)  # DAILY/WEEKLY/MONTHLY
    output_format: Mapped[str] = mapped_column(String(10), nullable=False)  # XLSX/PDF
    recipient_emails: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    next_run_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ImportExportJob(Base):
    """가져오기/내보내기 작업 이력 (v1.2). storage_type으로 향후 NAS/S3 전환을 대비한다."""

    __tablename__ = "import_export_jobs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_type: Mapped[str] = mapped_column(String(10), nullable=False)  # IMPORT/EXPORT
    target_entity: Mapped[str] = mapped_column(String(30), nullable=False)  # PRODUCT/PRODUCT_COST/COST/SUPPLIER
    file_format: Mapped[str] = mapped_column(String(10), nullable=False)  # XLSX/CSV
    file_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    storage_type: Mapped[str] = mapped_column(String(10), default="LOCAL", nullable=False)  # LOCAL/NAS/S3
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # RUNNING/SUCCESS/FAILED/PARTIAL_SUCCESS
    total_rows: Mapped[Optional[int]] = mapped_column(nullable=True)
    success_rows: Mapped[Optional[int]] = mapped_column(nullable=True)
    error_rows: Mapped[Optional[int]] = mapped_column(nullable=True)
    error_detail_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AuditLog(Base):
    """변경 이력 공통 테이블 (v1.2). before_json을 재적용하면 복원 가능한 수준까지 스냅샷을 남긴다."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("idx_audit_entity", "entity_type", "entity_id", "changed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(
        String(30), nullable=False
    )  # PRODUCT/PRODUCT_COST/COST/ORDER/SETTING/ROLE_PERMISSION
    entity_id: Mapped[int] = mapped_column(nullable=False)
    action: Mapped[str] = mapped_column(String(10), nullable=False)  # CREATE/UPDATE/DELETE
    before_json: Mapped[Optional[str]] = mapped_column(String(4000), nullable=True)
    after_json: Mapped[Optional[str]] = mapped_column(String(4000), nullable=True)
    # v1.2 권장 개선사항: 하나의 요청 단위로 여러 변경을 묶어 조회
    request_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    changed_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # 업무 설계: "누가 무엇을 언제 왜 했는가"를 추적하려면 사유와 업무 구분이 필요하다.
    # command는 어떤 업무 명령이 이 변경을 냈는지(예: orders.confirm) 기록한다.
    reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    command: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)


class AiAnalysisResult(Base):
    """AI 분석 결과 저장 (스키마만 우선 확정). 결과 구조가 분석 유형마다 달라 JSON으로 유연하게 저장한다."""

    __tablename__ = "ai_analysis_results"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # SALES_FORECAST/AD_PERFORMANCE/INVENTORY_FORECAST/AUTO_PURCHASE_SUGGEST/PRODUCT_RECOMMEND/PROFIT_ANALYSIS 등
    analysis_type: Mapped[str] = mapped_column(String(30), nullable=False)
    target_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # PRODUCT/PLATFORM/GLOBAL
    target_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    period_key: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    result_json: Mapped[str] = mapped_column(String(8000), nullable=False)
    model_version: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
