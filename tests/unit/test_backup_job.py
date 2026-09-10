"""
tests/unit/test_backup_job.py
------------------------------------------
scheduler/jobs/backup_job.py는 DB 엔진에 따라 구현을 분기하는 얇은 진입점이다.

SQLite 경로(_run_sqlite_backup)는 session_scope()를 쓰는 기존 로직을 전혀
바꾸지 않았다(회귀 없음) - 그래서 이 테스트는 그 내부 로직을 다시 검증하지
않고(기존 코드베이스 방침: session_scope()를 쓰는 스케줄러 잡은 격리된 실제
DB로 단위테스트하지 않는다), run()이 database_url에 따라 올바른 구현으로
"분기"하는지만 monkeypatch로 확인한다. PostgreSQL 경로의 실제 백업 로직은
tests/unit/test_postgres_backup_service.py가 담당한다.
"""

from unittest.mock import MagicMock

import pytest

import services
from config.settings import settings
from scheduler.jobs import backup_job


class TestDispatch:
    def test_sqlite_url_dispatches_to_sqlite_backup(self, monkeypatch):
        monkeypatch.setattr(settings, "database_url", "sqlite:///erp.db")
        stub = MagicMock(return_value={"status": "SUCCESS"})
        monkeypatch.setattr(backup_job, "_run_sqlite_backup", stub)

        result = backup_job.run()

        stub.assert_called_once_with()
        assert result == {"status": "SUCCESS"}

    def test_postgres_url_dispatches_to_postgres_backup_service(self, monkeypatch):
        monkeypatch.setattr(settings, "database_url", "postgresql+psycopg://u:p@host/db")
        fake_service = MagicMock()
        fake_service.run_backup_job.return_value = {"skipped_disabled": 1}
        # backup_job.run()이 "from services import postgres_backup_service"를 쓰므로,
        # sys.modules가 아니라 services 패키지 객체의 속성 자체를 바꿔치기해야
        # getattr(services, "postgres_backup_service")가 이 fake를 돌려준다.
        monkeypatch.setattr(services, "postgres_backup_service", fake_service)

        result = backup_job.run()

        fake_service.run_backup_job.assert_called_once_with()
        assert result == {"skipped_disabled": 1}

    def test_plain_postgres_scheme_also_dispatches_to_postgres(self, monkeypatch):
        monkeypatch.setattr(settings, "database_url", "postgres://u:p@host/db")
        fake_service = MagicMock()
        fake_service.run_backup_job.return_value = {"skipped_disabled": 1}
        monkeypatch.setattr(services, "postgres_backup_service", fake_service)

        result = backup_job.run()

        fake_service.run_backup_job.assert_called_once_with()
        assert result == {"skipped_disabled": 1}

    def test_unsupported_engine_raises_not_implemented(self, monkeypatch):
        monkeypatch.setattr(settings, "database_url", "mysql://u:p@host/db")

        with pytest.raises(NotImplementedError):
            backup_job.run()
