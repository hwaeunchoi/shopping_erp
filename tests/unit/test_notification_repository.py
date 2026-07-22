"""
tests/unit/test_notification_repository.py
--------------------------------------------------
NotificationRepository/AlertRuleRepository 단위 테스트. UI v1.0 알림센터 대응.
"""

from datetime import datetime, timezone

from models.system import AlertRule, Notification
from repositories.system_repository import AlertRuleRepository, NotificationRepository


def _make_rule(db_session, name="테스트규칙", metric="RETURN_RATE", is_enabled=True):
    rule = AlertRule(
        name=name,
        metric=metric,
        operator="GT",
        threshold_value=10.0,
        check_frequency="HOURLY",
        is_enabled=is_enabled,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(rule)
    db_session.flush()
    return rule


class TestAlertRuleRepository:
    def test_list_enabled_excludes_disabled(self, db_session):
        enabled = _make_rule(db_session, name="enabled", is_enabled=True)
        _make_rule(db_session, name="disabled", is_enabled=False)

        result = AlertRuleRepository(db_session).list_enabled()

        assert [r.id for r in result] == [enabled.id]


class TestNotificationRepository:
    def test_list_recent_unread_only_filters(self, db_session):
        repo = NotificationRepository(db_session)
        unread = repo.add(
            Notification(
                type="RETURN_RATE",
                severity="WARNING",
                message="msg1",
                is_read=False,
                created_at=datetime.now(timezone.utc),
            )
        )
        repo.add(
            Notification(
                type="RETURN_RATE",
                severity="WARNING",
                message="msg2",
                is_read=True,
                created_at=datetime.now(timezone.utc),
            )
        )

        result = repo.list_recent(unread_only=True)

        assert [n.id for n in result] == [unread.id]

    def test_count_unread(self, db_session):
        repo = NotificationRepository(db_session)
        repo.add(
            Notification(type="X", severity="INFO", message="a", is_read=False, created_at=datetime.now(timezone.utc))
        )
        repo.add(
            Notification(type="X", severity="INFO", message="b", is_read=False, created_at=datetime.now(timezone.utc))
        )
        repo.add(
            Notification(type="X", severity="INFO", message="c", is_read=True, created_at=datetime.now(timezone.utc))
        )

        assert repo.count_unread() == 2

    def test_has_unread_for_rule(self, db_session):
        rule = _make_rule(db_session)
        repo = NotificationRepository(db_session)

        assert repo.has_unread_for_rule(rule.id) is False

        repo.add(
            Notification(
                rule_id=rule.id,
                type="X",
                severity="WARNING",
                message="a",
                is_read=False,
                created_at=datetime.now(timezone.utc),
            )
        )

        assert repo.has_unread_for_rule(rule.id) is True

    def test_mark_all_read(self, db_session):
        repo = NotificationRepository(db_session)
        repo.add(
            Notification(type="X", severity="INFO", message="a", is_read=False, created_at=datetime.now(timezone.utc))
        )
        repo.add(
            Notification(type="X", severity="INFO", message="b", is_read=False, created_at=datetime.now(timezone.utc))
        )

        marked = repo.mark_all_read()

        assert marked == 2
        assert repo.count_unread() == 0
