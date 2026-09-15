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
import services.postgres_backup_service  # noqa: F401 - services 패키지에 이 서브모듈을

# 속성으로 실제 등록해두기 위한 import다(단순 "import services"만으로는 하위 모듈이
# 아직 한 번도 import되지 않았으면 getattr(services, "postgres_backup_service")가
# 실패한다 - 이 파일을 다른 테스트 파일보다 먼저/단독으로 실행해도 monkeypatch.setattr()이
# 항상 통과하도록 실행 순서와 무관하게 만든다).
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


class TestCatchupDispatch:
    """fix/postgres-backup-missed-run-recovery: run_catchup()도 run()과 동일한
    분기 구조 - PostgreSQL만 실제로 위임하고, SQLite는 "떠 있지 않던 기간에
    놓친 실행"이라는 개념 자체가 없어(컨테이너 시작 시 스냅샷 복사 방식) 항상
    skipped_disabled다."""

    def test_postgres_url_delegates_to_run_catchup_if_needed(self, monkeypatch):
        monkeypatch.setattr(settings, "database_url", "postgresql+psycopg://u:p@host/db")
        fake_service = MagicMock()
        fake_service.run_catchup_if_needed.return_value = {"skipped_up_to_date": 1}
        monkeypatch.setattr(services, "postgres_backup_service", fake_service)

        result = backup_job.run_catchup()

        fake_service.run_catchup_if_needed.assert_called_once_with()
        assert result == {"skipped_up_to_date": 1}

    def test_plain_postgres_scheme_also_delegates(self, monkeypatch):
        monkeypatch.setattr(settings, "database_url", "postgres://u:p@host/db")
        fake_service = MagicMock()
        fake_service.run_catchup_if_needed.return_value = {"skipped_disabled": 1}
        monkeypatch.setattr(services, "postgres_backup_service", fake_service)

        result = backup_job.run_catchup()

        fake_service.run_catchup_if_needed.assert_called_once_with()
        assert result == {"skipped_disabled": 1}

    def test_sqlite_url_is_always_skipped_disabled_without_touching_sqlite_backup(self, monkeypatch):
        monkeypatch.setattr(settings, "database_url", "sqlite:///erp.db")
        stub = MagicMock()
        monkeypatch.setattr(backup_job, "_run_sqlite_backup", stub)

        result = backup_job.run_catchup()

        stub.assert_not_called()
        assert result == {"skipped_disabled": 1}

    def test_unsupported_engine_is_also_skipped_disabled_not_raised(self, monkeypatch):
        """run()과 달리 run_catchup()은 지원 안 하는 엔진이어도 예외를 던지지
        않는다 - catch-up은 "있으면 좋은" 보조 기능이라 scheduler 기동 자체를
        막을 이유가 없다."""
        monkeypatch.setattr(settings, "database_url", "mysql://u:p@host/db")

        result = backup_job.run_catchup()

        assert result == {"skipped_disabled": 1}
