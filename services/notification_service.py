"""
services/notification_service.py
------------------------------------
UI v1.0 알림센터: notifications 테이블에 대한 생성/조회/읽음처리.

Repository만 사용하고 세션 쿼리를 직접 다루지 않는다. 알림 생성 자체는
어디서나(스케줄러 잡의 실패 처리, AlertEvaluationService 등) 호출할 수
있도록 얇은 래퍼로 유지한다.
"""

from datetime import datetime, timezone
from typing import Optional

from models.system import Notification
from repositories.system_repository import NotificationRepository


class NotificationService:
    def __init__(self, session) -> None:
        self.session = session
        self.notification_repo = NotificationRepository(session)

    def notify(self, type_: str, severity: str, message: str, rule_id: Optional[int] = None) -> Notification:
        return self.notification_repo.add(
            Notification(
                rule_id=rule_id,
                type=type_,
                severity=severity,
                message=message[:1000],
                is_read=False,
                created_at=datetime.now(timezone.utc),
            )
        )

    def list_recent(self, unread_only: bool = False, limit: int = 50) -> list[Notification]:
        return self.notification_repo.list_recent(unread_only=unread_only, limit=limit)

    def unread_count(self) -> int:
        return self.notification_repo.count_unread()

    def mark_read(self, notification: Notification) -> Notification:
        notification.is_read = True
        self.session.flush()
        return notification

    def mark_all_read(self) -> int:
        return self.notification_repo.mark_all_read()
