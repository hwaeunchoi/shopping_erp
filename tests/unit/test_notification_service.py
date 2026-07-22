"""
tests/unit/test_notification_service.py
------------------------------------------------
NotificationService 단위 테스트.
"""

from services.notification_service import NotificationService


class TestNotificationService:
    def test_notify_creates_unread_notification(self, db_session):
        service = NotificationService(db_session)

        notification = service.notify("API_FAILURE", "CRITICAL", "수집 실패")

        assert notification.id is not None
        assert notification.is_read is False
        assert notification.severity == "CRITICAL"

    def test_notify_truncates_long_message(self, db_session):
        service = NotificationService(db_session)

        notification = service.notify("X", "INFO", "a" * 2000)

        assert len(notification.message) == 1000

    def test_mark_read_updates_flag(self, db_session):
        service = NotificationService(db_session)
        notification = service.notify("X", "INFO", "msg")

        service.mark_read(notification)

        assert notification.is_read is True

    def test_unread_count_and_mark_all_read(self, db_session):
        service = NotificationService(db_session)
        service.notify("A", "INFO", "1")
        service.notify("B", "INFO", "2")

        assert service.unread_count() == 2

        marked = service.mark_all_read()

        assert marked == 2
        assert service.unread_count() == 0
