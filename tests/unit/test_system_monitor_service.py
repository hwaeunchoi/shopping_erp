"""
tests/unit/test_system_monitor_service.py
--------------------------------------------------
SystemMonitorService(시스템 모니터링 > 시스템상태 탭) 단위 테스트.
UI 와이어프레임 v1.1 4.3절 대응.
"""

from datetime import datetime, timezone

from models.system import BackupHistory
from services.system_monitor_service import SystemMonitorService


class TestGetSystemStatus:
    def test_returns_status_with_no_backup_history(self, db_session):
        status = SystemMonitorService(db_session).get_system_status()

        assert status.db_size_bytes >= 0
        assert status.log_dir_size_bytes >= 0
        assert status.latest_backup is None
        assert status.uptime_seconds >= 0

    def test_returns_latest_backup_when_present(self, db_session):
        db_session.add(
            BackupHistory(
                file_path="/tmp/old.db",
                file_size_bytes=1000,
                status="SUCCESS",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )
        db_session.add(
            BackupHistory(
                file_path="/tmp/new.db",
                file_size_bytes=2000,
                status="SUCCESS",
                created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            )
        )
        db_session.flush()

        status = SystemMonitorService(db_session).get_system_status()

        assert status.latest_backup is not None
        assert status.latest_backup.file_size_bytes == 2000
        assert status.latest_backup.status == "SUCCESS"
