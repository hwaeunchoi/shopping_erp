"""
repositories/system_repository.py
--------------------------------------
ERD 2.1(v1.0)+v1.2 시스템 그룹 중, 현재 실제로 쓰이는 backup_history/
system_logs/alert_rules/notifications/system_settings/api_credentials에
대한 Repository를 제공한다.

dashboard_widgets 등 나머지 시스템 테이블은 이를 실제로 사용하는 화면/작업이
생길 때 함께 추가한다.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models.system import AlertRule, ApiCredential, BackupHistory, Notification, SystemLog, SystemSetting
from repositories.base_repository import BaseRepository


class BackupHistoryRepository(BaseRepository[BackupHistory]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, BackupHistory)

    def list_recent(self, limit: int = 30) -> list[BackupHistory]:
        stmt = select(BackupHistory).order_by(BackupHistory.created_at.desc()).limit(limit)
        return list(self.session.execute(stmt).scalars().all())


class SystemLogRepository(BaseRepository[SystemLog]):
    """SRS FR-LOG-01(API 수집이력/사용자활동/시스템오류 로그) 대응."""

    def __init__(self, session: Session) -> None:
        super().__init__(session, SystemLog)

    def list_recent(self, log_type: Optional[str] = None, limit: int = 100) -> list[SystemLog]:
        stmt = select(SystemLog)
        if log_type is not None:
            stmt = stmt.where(SystemLog.log_type == log_type)
        stmt = stmt.order_by(SystemLog.created_at.desc()).limit(limit)
        return list(self.session.execute(stmt).scalars().all())


class AlertRuleRepository(BaseRepository[AlertRule]):
    """UI v1.0 알림센터: 사용자 정의 알림 규칙(alert_rules) 관리."""

    def __init__(self, session: Session) -> None:
        super().__init__(session, AlertRule)

    def list_enabled(self) -> list[AlertRule]:
        stmt = select(AlertRule).where(AlertRule.is_enabled.is_(True))
        return list(self.session.execute(stmt).scalars().all())


class NotificationRepository(BaseRepository[Notification]):
    """UI v1.0 알림센터: 알림 발생 이력(notifications) 조회/읽음 처리."""

    def __init__(self, session: Session) -> None:
        super().__init__(session, Notification)

    def list_recent(self, unread_only: bool = False, limit: int = 50) -> list[Notification]:
        stmt = select(Notification)
        if unread_only:
            stmt = stmt.where(Notification.is_read.is_(False))
        stmt = stmt.order_by(Notification.created_at.desc()).limit(limit)
        return list(self.session.execute(stmt).scalars().all())

    def count_unread(self) -> int:
        stmt = select(func.count()).select_from(Notification).where(Notification.is_read.is_(False))
        return self.session.execute(stmt).scalar_one()

    def has_unread_for_rule(self, rule_id: int) -> bool:
        """같은 규칙으로 아직 안 읽은 알림이 있으면 True - 중복 알림 생성을 막는 데 쓴다."""
        stmt = (
            select(func.count())
            .select_from(Notification)
            .where(Notification.rule_id == rule_id, Notification.is_read.is_(False))
        )
        return self.session.execute(stmt).scalar_one() > 0

    def mark_all_read(self) -> int:
        stmt = select(Notification).where(Notification.is_read.is_(False))
        unread = list(self.session.execute(stmt).scalars().all())
        for n in unread:
            n.is_read = True
        self.session.flush()
        return len(unread)


class SystemSettingRepository(BaseRepository[SystemSetting]):
    """설정 화면의 시스템 설정(Key-Value) 관리."""

    def __init__(self, session: Session) -> None:
        super().__init__(session, SystemSetting)

    def list_by_category(self, category: Optional[str] = None) -> list[SystemSetting]:
        stmt = select(SystemSetting)
        if category is not None:
            stmt = stmt.where(SystemSetting.category == category)
        return list(self.session.execute(stmt).scalars().all())

    def get_by_category_key(self, category: str, key: str) -> Optional[SystemSetting]:
        stmt = select(SystemSetting).where(SystemSetting.category == category, SystemSetting.key == key)
        return self.session.execute(stmt).scalar_one_or_none()

    def upsert(self, category: str, key: str, value: Optional[str]) -> SystemSetting:
        existing = self.get_by_category_key(category, key)
        if existing is not None:
            existing.value = value
            existing.updated_at = datetime.now(timezone.utc)
            self.session.flush()
            return existing
        setting = SystemSetting(category=category, key=key, value=value, updated_at=datetime.now(timezone.utc))
        self.session.add(setting)
        self.session.flush()
        return setting


class ApiCredentialRepository(BaseRepository[ApiCredential]):
    """설정 화면의 API Credential(플랫폼/광고 API 인증정보) 관리.

    key_value_encrypted는 항상 core.crypto.encrypt_value()로 암호화된 값만
    담긴다 - 평문 저장은 Service 계층에서 금지한다.
    """

    def __init__(self, session: Session) -> None:
        super().__init__(session, ApiCredential)

    def list_by_owner(self, owner_type: Optional[str] = None, owner_id: Optional[int] = None) -> list[ApiCredential]:
        stmt = select(ApiCredential)
        if owner_type is not None:
            stmt = stmt.where(ApiCredential.owner_type == owner_type)
        if owner_id is not None:
            stmt = stmt.where(ApiCredential.owner_id == owner_id)
        return list(self.session.execute(stmt).scalars().all())

    def get_by_owner_and_key(self, owner_type: str, owner_id: int, key_name: str) -> Optional[ApiCredential]:
        stmt = select(ApiCredential).where(
            ApiCredential.owner_type == owner_type,
            ApiCredential.owner_id == owner_id,
            ApiCredential.key_name == key_name,
        )
        return self.session.execute(stmt).scalar_one_or_none()
