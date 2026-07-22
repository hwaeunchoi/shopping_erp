"""
models/system.py
------------------
ERD 2.1(v1.0) + v1.2 확장: system_logs, backup_history, system_settings,
api_credentials, dashboard_widgets, notifications, alert_rules

alert_rules는 v1.2에서 notification_rules를 대체한 사용자 정의 조건 규칙이다.
scope_type/scope_id는 단일 대상 기본 구조이며, 다중 대상이 필요해지면
alert_rule_targets(rule_id, target_type, target_id) 매핑 테이블을 추가하고
scope_type='MULTI'일 때 이를 참조하도록 확장한다 (설계 방침, 현재 미생성).
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class SystemLog(Base):
    """시스템/API/사용자 활동 로그."""

    __tablename__ = "system_logs"
    __table_args__ = (Index("idx_logs_type_date", "log_type", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    log_type: Mapped[str] = mapped_column(String(30), nullable=False)  # API_COLLECT/USER_ACTIVITY/SYSTEM_ERROR/BACKUP
    source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    level: Mapped[str] = mapped_column(String(10), nullable=False)  # INFO/WARN/ERROR
    message: Mapped[str] = mapped_column(String(2000), nullable=False)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class BackupHistory(Base):
    """DB 백업 이력."""

    __tablename__ = "backup_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    file_path: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size_bytes: Mapped[Optional[int]] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # SUCCESS/FAILED
    error_message: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class SystemSetting(Base):
    """시스템 환경설정 (Key-Value, category별)."""

    __tablename__ = "system_settings"
    __table_args__ = (UniqueConstraint("category", "key", name="uq_system_setting"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String(30), nullable=False)  # BACKUP/REPORT/SYSTEM 등
    key: Mapped[str] = mapped_column(String(50), nullable=False)
    value: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)  # JSON 문자열 허용
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ApiCredential(Base):
    """플랫폼/광고 API 인증정보. key_value_encrypted는 애플리케이션 레벨에서 암호화 후 저장."""

    __tablename__ = "api_credentials"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    owner_type: Mapped[str] = mapped_column(String(20), nullable=False)  # PLATFORM/AD_PLATFORM
    owner_id: Mapped[int] = mapped_column(nullable=False)
    key_name: Mapped[str] = mapped_column(String(50), nullable=False)  # client_id/client_secret/access_token 등
    key_value_encrypted: Mapped[str] = mapped_column(String(1000), nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class DashboardWidget(Base):
    """사용자별 대시보드 위젯 배치."""

    __tablename__ = "dashboard_widgets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    widget_code: Mapped[str] = mapped_column(String(50), nullable=False)  # TODAY_SALES/ROAS_TREND 등
    position: Mapped[int] = mapped_column(nullable=False)
    size: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)  # SMALL/MEDIUM/LARGE
    is_visible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class AlertRule(Base):
    """사용자 정의 알림 규칙 (v1.2). notifications.rule_id가 이 테이블을 참조한다."""

    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # ORDER_COUNT_TODAY/AD_COST/ROAS/RETURN_RATE/UNSHIPPED_DAYS/API_FAILURE/BACKUP_FAILURE 등
    metric: Mapped[str] = mapped_column(String(30), nullable=False)
    operator: Mapped[str] = mapped_column(String(10), nullable=False)  # LT/LTE/GT/GTE/EQ
    threshold_value: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    scope_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # ALL/PLATFORM/PRODUCT
    scope_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    check_frequency: Mapped[str] = mapped_column(String(20), nullable=False)  # REALTIME/HOURLY/DAILY
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Notification(Base):
    """알림 발생 이력."""

    __tablename__ = "notifications"
    __table_args__ = (Index("idx_notifications_created", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    rule_id: Mapped[Optional[int]] = mapped_column(ForeignKey("alert_rules.id"), nullable=True)
    type: Mapped[str] = mapped_column(String(30), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False)  # INFO/WARNING/CRITICAL
    message: Mapped[str] = mapped_column(String(1000), nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
