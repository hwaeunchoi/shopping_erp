"""
tests/unit/test_postgres_backup_service.py
------------------------------------------------
services/postgres_backup_service.py 단위테스트.

DB(advisory lock/실행이력 기록)가 필요한 경로는 실제 PostgreSQL 없이는 의미
있게 검증할 수 없다(SQLite에는 advisory lock이 없다 - 모듈 docstring 참고) -
이런 경로는 core.database.engine/session_scope를 이 테스트 모듈 안에서만
monkeypatch로 가짜 객체/함수로 바꿔치기해 순수하게 분기 로직만 검증한다
(tests/unit/test_outbox_dispatch_job.py 등 기존 스케줄러 잡 테스트 방침과
동일하게, 실제 운영 DB 엔진에는 전혀 연결하지 않는다). 실제 pg_dump/pg_restore
바이너리와 진짜 PostgreSQL을 상대로 한 end-to-end 검증(동시성 lock 포함)은
tests/integration/test_postgres_backup_restore_pg.py가 담당한다.
"""

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from config.settings import settings
from services import postgres_backup_service as svc

# --------------------------------------------------------------------------
# 기본 비활성화
# --------------------------------------------------------------------------


class TestDisabledByDefault:
    def test_default_is_disabled(self):
        assert settings.postgres_backup_enabled is False

    def test_run_backup_job_returns_immediately_without_any_db_or_fs_access(self, monkeypatch):
        monkeypatch.setattr(settings, "postgres_backup_enabled", False)

        def _fail_if_called(*args, **kwargs):
            raise AssertionError("기능이 OFF인데 engine.connect()/session_scope()가 호출되었습니다.")

        monkeypatch.setattr(svc, "engine", MagicMock(connect=_fail_if_called))
        monkeypatch.setattr(svc, "session_scope", _fail_if_called)
        monkeypatch.setattr(svc, "_run_backup_locked", _fail_if_called)

        result = svc.run_backup_job()

        assert result == {"skipped_disabled": 1}


# --------------------------------------------------------------------------
# URL 파싱
# --------------------------------------------------------------------------


class TestParsePostgresUrl:
    def test_parses_host_port_database_username_password(self):
        parsed = svc.parse_postgres_url("postgresql+psycopg://erp_user:hunter2@dbhost:5433/erp_db")
        assert parsed.host == "dbhost"
        assert parsed.port == 5433
        assert parsed.database == "erp_db"
        assert parsed.username == "erp_user"
        assert parsed.password == "hunter2"

    def test_percent_encoded_password_is_decoded(self):
        # 원본 비밀번호: P@ss:word/1 -> percent-encoding
        parsed = svc.parse_postgres_url("postgresql://erp_user:P%40ss%3Aword%2F1@dbhost/erp_db")
        assert parsed.password == "P@ss:word/1"

    def test_default_port_when_missing(self):
        parsed = svc.parse_postgres_url("postgresql://erp_user:pw@dbhost/erp_db")
        assert parsed.port == 5432

    def test_non_postgres_scheme_is_rejected(self):
        with pytest.raises(svc.PostgresBackupError) as exc_info:
            svc.parse_postgres_url("sqlite:///erp.db")
        assert exc_info.value.error_code == svc.ERR_INVALID_CONFIGURATION

    def test_missing_host_is_rejected(self):
        with pytest.raises(svc.PostgresBackupError) as exc_info:
            svc.parse_postgres_url("postgresql:///erp_db")
        assert exc_info.value.error_code == svc.ERR_INVALID_CONFIGURATION

    def test_malformed_url_is_rejected(self):
        with pytest.raises(svc.PostgresBackupError) as exc_info:
            svc.parse_postgres_url("not a valid url::://")
        assert exc_info.value.error_code == svc.ERR_INVALID_CONFIGURATION

    def test_error_message_never_contains_raw_password(self):
        with pytest.raises(svc.PostgresBackupError) as exc_info:
            svc.parse_postgres_url("postgresql://erp_user:hunter2@/erp_db")
        assert "hunter2" not in exc_info.value.safe_message


# --------------------------------------------------------------------------
# PGPASSFILE
# --------------------------------------------------------------------------


class TestPgpassfile:
    def _parsed(self):
        return svc.ParsedPostgresUrl(
            host="dbhost", port=5432, database="erp_db", username="erp_user", password="p:a\\ss"
        )

    @pytest.mark.skipif(
        os.name == "nt",
        reason="Windows는 POSIX 권한 비트를 지원하지 않는다 - 실제 실행 환경(Linux 컨테이너)에서만 의미가 있다.",
    )
    def test_permission_is_0600(self):
        path = svc._write_pgpassfile(self._parsed())
        try:
            mode = oct(os.stat(path).st_mode & 0o777)
            assert mode == oct(0o600)
        finally:
            path.unlink(missing_ok=True)

    def test_content_escapes_colon_and_backslash(self):
        path = svc._write_pgpassfile(self._parsed())
        try:
            line = path.read_text(encoding="utf-8").strip()
            assert line == "dbhost:5432:erp_db:erp_user:p\\:a\\\\ss"
        finally:
            path.unlink(missing_ok=True)

    def test_caller_can_delete_it_immediately(self):
        path = svc._write_pgpassfile(self._parsed())
        assert path.exists()
        path.unlink(missing_ok=True)
        assert not path.exists()


# --------------------------------------------------------------------------
# stderr 기반 오류 분류 (원문은 반환하지 않는다)
# --------------------------------------------------------------------------


class TestClassifyDumpFailure:
    def test_connection_refused_is_connection_failure(self):
        assert (
            svc._classify_dump_failure(b"pg_dump: error: connection to server ... Connection refused")
            == svc.ERR_CONNECTION_FAILURE
        )

    def test_auth_failure_is_connection_failure(self):
        assert (
            svc._classify_dump_failure(b"FATAL: password authentication failed for user") == svc.ERR_CONNECTION_FAILURE
        )

    def test_unrelated_error_is_dump_failure(self):
        assert (
            svc._classify_dump_failure(b"pg_dump: error: query failed: permission denied for table x")
            == svc.ERR_DUMP_FAILURE
        )

    def test_empty_stderr_is_dump_failure(self):
        assert svc._classify_dump_failure(b"") == svc.ERR_DUMP_FAILURE


# --------------------------------------------------------------------------
# pg_dump 실행 (subprocess는 항상 mock - 실제 바이너리 미실행)
# --------------------------------------------------------------------------


class TestRunPgDump:
    def _parsed(self):
        return svc.ParsedPostgresUrl(
            host="dbhost", port=5432, database="erp_db", username="erp_user", password="hunter2"
        )

    def test_tool_missing_raises(self, monkeypatch, tmp_path):
        monkeypatch.setattr(svc.shutil, "which", lambda name: None)
        with pytest.raises(svc.PostgresBackupError) as exc_info:
            svc._run_pg_dump(self._parsed(), tmp_path / "pgpass", tmp_path / "out.dump", 10)
        assert exc_info.value.error_code == svc.ERR_TOOL_MISSING

    def test_command_is_argument_array_without_shell_and_without_password(self, monkeypatch, tmp_path):
        monkeypatch.setattr(svc.shutil, "which", lambda name: f"/usr/bin/{name}")
        captured = {}

        def fake_run(cmd, env, capture_output, timeout, check, shell):
            captured["cmd"] = cmd
            captured["env"] = env
            captured["shell"] = shell
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=b"", stderr=b"")

        monkeypatch.setattr(svc.subprocess, "run", fake_run)
        svc._run_pg_dump(self._parsed(), tmp_path / "pgpass", tmp_path / "out.dump", 10)

        assert isinstance(captured["cmd"], list)
        assert captured["shell"] is False
        assert "hunter2" not in captured["cmd"]
        assert not any("hunter2" in str(part) for part in captured["cmd"])
        assert "--no-password" in captured["cmd"]
        assert captured["env"]["PGPASSFILE"] == str(tmp_path / "pgpass")

    def test_timeout_raises_dump_timeout(self, monkeypatch, tmp_path):
        monkeypatch.setattr(svc.shutil, "which", lambda name: f"/usr/bin/{name}")

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=float(kwargs.get("timeout") or 0))

        monkeypatch.setattr(svc.subprocess, "run", fake_run)
        with pytest.raises(svc.PostgresBackupError) as exc_info:
            svc._run_pg_dump(self._parsed(), tmp_path / "pgpass", tmp_path / "out.dump", 5)
        assert exc_info.value.error_code == svc.ERR_DUMP_TIMEOUT

    def test_nonzero_exit_raises_with_safe_message_only(self, monkeypatch, tmp_path):
        monkeypatch.setattr(svc.shutil, "which", lambda name: f"/usr/bin/{name}")

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, returncode=1, stdout=b"", stderr=b"some raw stderr with secret stuff"
            )

        monkeypatch.setattr(svc.subprocess, "run", fake_run)
        with pytest.raises(svc.PostgresBackupError) as exc_info:
            svc._run_pg_dump(self._parsed(), tmp_path / "pgpass", tmp_path / "out.dump", 5)
        assert exc_info.value.error_code == svc.ERR_DUMP_FAILURE
        assert "secret stuff" not in exc_info.value.safe_message


# --------------------------------------------------------------------------
# pg_restore --list 구조 검증
# --------------------------------------------------------------------------


class TestValidateWithRestoreList:
    def test_tool_missing_raises(self, monkeypatch, tmp_path):
        monkeypatch.setattr(svc.shutil, "which", lambda name: None)
        with pytest.raises(svc.PostgresBackupError) as exc_info:
            svc._validate_with_restore_list(tmp_path / "x.dump", 10)
        assert exc_info.value.error_code == svc.ERR_TOOL_MISSING

    def test_nonzero_exit_raises(self, monkeypatch, tmp_path):
        monkeypatch.setattr(svc.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(
            svc.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], returncode=1, stdout=b"", stderr=b"")
        )
        with pytest.raises(svc.PostgresBackupError) as exc_info:
            svc._validate_with_restore_list(tmp_path / "x.dump", 10)
        assert exc_info.value.error_code == svc.ERR_RESTORE_LIST_VALIDATION_FAILURE

    def test_zero_entries_raises(self, monkeypatch, tmp_path):
        monkeypatch.setattr(svc.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(
            svc.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess([], returncode=0, stdout=b";\n; header only\n", stderr=b""),
        )
        with pytest.raises(svc.PostgresBackupError) as exc_info:
            svc._validate_with_restore_list(tmp_path / "x.dump", 10)
        assert exc_info.value.error_code == svc.ERR_RESTORE_LIST_VALIDATION_FAILURE

    def test_success_returns_entry_count(self, monkeypatch, tmp_path):
        monkeypatch.setattr(svc.shutil, "which", lambda name: f"/usr/bin/{name}")
        stdout = b";\n; comment\n1; 123 table users\n2; 124 table orders\n"
        monkeypatch.setattr(
            svc.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess([], returncode=0, stdout=stdout, stderr=b""),
        )
        count = svc._validate_with_restore_list(tmp_path / "x.dump", 10)
        assert count == 2

    def test_timeout_raises(self, monkeypatch, tmp_path):
        monkeypatch.setattr(svc.shutil, "which", lambda name: f"/usr/bin/{name}")

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=float(kwargs.get("timeout") or 0))

        monkeypatch.setattr(svc.subprocess, "run", fake_run)
        with pytest.raises(svc.PostgresBackupError) as exc_info:
            svc._validate_with_restore_list(tmp_path / "x.dump", 5)
        assert exc_info.value.error_code == svc.ERR_RESTORE_LIST_VALIDATION_FAILURE


# --------------------------------------------------------------------------
# SHA-256
# --------------------------------------------------------------------------


class TestSha256File:
    def test_matches_known_digest(self, tmp_path):
        path = tmp_path / "f.bin"
        path.write_bytes(b"hello world")
        import hashlib

        expected = hashlib.sha256(b"hello world").hexdigest()
        assert svc._sha256_file(path) == expected


# --------------------------------------------------------------------------
# 경로 안전성 (symlink/path traversal)
# --------------------------------------------------------------------------


class TestSafeBackupPath:
    def test_normal_filename_accepted(self, tmp_path):
        result = svc._safe_backup_path(tmp_path, "erp_postgres_20260101_000000_UTC.dump")
        assert result == tmp_path / "erp_postgres_20260101_000000_UTC.dump"

    def test_path_traversal_rejected(self, tmp_path):
        with pytest.raises(svc.PostgresBackupError) as exc_info:
            svc._safe_backup_path(tmp_path, "../../etc/passwd")
        assert exc_info.value.error_code == svc.ERR_RENAME_FAILURE

    def test_nested_slash_rejected(self, tmp_path):
        with pytest.raises(svc.PostgresBackupError):
            svc._safe_backup_path(tmp_path, "sub/erp_postgres_x.dump")

    def test_symlink_target_rejected(self, tmp_path):
        outside = tmp_path.parent / f"outside-{tmp_path.name}.txt"
        outside.write_text("x")
        link = tmp_path / "erp_postgres_link.dump"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("이 환경에서는 symlink를 만들 권한이 없습니다.")
        try:
            with pytest.raises(svc.PostgresBackupError) as exc_info:
                svc._safe_backup_path(tmp_path, "erp_postgres_link.dump")
            assert exc_info.value.error_code == svc.ERR_RENAME_FAILURE
        finally:
            link.unlink(missing_ok=True)
            outside.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# 보존정책(retention)
# --------------------------------------------------------------------------


class TestApplyRetention:
    def _make_backup_trio(self, backup_dir: Path, stem: str) -> None:
        (backup_dir / f"{stem}.dump").write_bytes(b"x")
        (backup_dir / f"{stem}.dump.sha256").write_text("deadbeef")
        (backup_dir / f"{stem}.dump.manifest.json").write_text("{}")

    def test_keeps_newest_n_and_deletes_older_trio(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "postgres_backup_retention_count", 2)
        stems = [f"erp_postgres_2026010{n}_000000_UTC" for n in range(1, 5)]
        for stem in stems:
            self._make_backup_trio(tmp_path, stem)

        retained = svc._apply_retention(tmp_path)

        assert retained == 2
        remaining_dumps = sorted(p.name for p in tmp_path.glob("erp_postgres_*.dump"))
        assert remaining_dumps == ["erp_postgres_20260103_000000_UTC.dump", "erp_postgres_20260104_000000_UTC.dump"]
        # 삭제된 것들은 sidecar/manifest까지 짝으로 사라져야 한다.
        assert not (tmp_path / "erp_postgres_20260101_000000_UTC.dump.sha256").exists()
        assert not (tmp_path / "erp_postgres_20260101_000000_UTC.dump.manifest.json").exists()
        # 보존된 것들의 sidecar/manifest는 그대로 남아야 한다.
        assert (tmp_path / "erp_postgres_20260104_000000_UTC.dump.sha256").exists()

    def test_ignores_unrelated_files(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "postgres_backup_retention_count", 2)
        self._make_backup_trio(tmp_path, "erp_postgres_20260101_000000_UTC")
        self._make_backup_trio(tmp_path, "erp_postgres_20260102_000000_UTC")
        other = tmp_path / "erp_20260101_manual.dump"  # 이 기능이 만든 접두어가 아니다.
        other.write_bytes(b"manual backup - do not touch")

        svc._apply_retention(tmp_path)

        assert other.exists()

    def test_invalid_retention_count_fails_closed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "postgres_backup_retention_count", 1)
        self._make_backup_trio(tmp_path, "erp_postgres_20260101_000000_UTC")
        with pytest.raises(svc.PostgresBackupError) as exc_info:
            svc._apply_retention(tmp_path)
        assert exc_info.value.error_code == svc.ERR_RETENTION_FAILURE

    def test_no_deletion_needed_returns_total_count(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "postgres_backup_retention_count", 5)
        self._make_backup_trio(tmp_path, "erp_postgres_20260101_000000_UTC")
        assert svc._apply_retention(tmp_path) == 1


# --------------------------------------------------------------------------
# 전체 오케스트레이션(_run_backup_locked) - subprocess/DB기록은 mock
# --------------------------------------------------------------------------


class FakeSessionScope:
    """postgres_backup_service._record_history()가 쓰는 session_scope()를
    실제 DB 없이 대체한다 - 기록된 BackupHistory 객체를 그대로 리스트에 담는다."""

    def __init__(self):
        self.added: list = []

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeRepo:
    def __init__(self, records: list):
        self.records = records

    def add(self, obj):
        self.records.append(obj)
        return obj


@pytest.fixture()
def fake_history(monkeypatch):
    records: list = []
    monkeypatch.setattr(svc, "session_scope", FakeSessionScope())
    monkeypatch.setattr(svc, "BackupHistoryRepository", lambda db: FakeRepo(records))
    return records


def _fake_dump_success(dest: Path, payload: bytes = b"PGDMP-fake-dump-content"):
    def fake_run(cmd, **kwargs):
        if "--file" in cmd:
            Path(cmd[cmd.index("--file") + 1]).write_bytes(payload)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=b";\n1; table t\n", stderr=b"")

    return fake_run


class TestRunBackupLockedOrchestration:
    def _setup_common(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "postgres_backup_dir", tmp_path)
        monkeypatch.setattr(settings, "postgres_backup_retention_count", 5)
        monkeypatch.setattr(settings, "postgres_backup_timeout_seconds", 5)
        monkeypatch.setattr(settings, "database_url", "postgresql://erp_user:hunter2@dbhost/erp_db")
        monkeypatch.setattr(svc.shutil, "which", lambda name: f"/usr/bin/{name}")

    def test_success_creates_final_file_sidecar_manifest_and_success_history(self, monkeypatch, tmp_path, fake_history):
        self._setup_common(monkeypatch, tmp_path)
        monkeypatch.setattr(svc.subprocess, "run", _fake_dump_success(tmp_path))

        started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = svc._run_backup_locked(started_at)

        assert result["status"] == "SUCCESS"
        final_path = tmp_path / result["file_name"]
        assert final_path.exists()
        assert (tmp_path / (result["file_name"] + ".sha256")).exists()
        assert (tmp_path / (result["file_name"] + ".manifest.json")).exists()
        assert fake_history[0].status == "SUCCESS"
        assert fake_history[0].sha256 == result["sha256"]
        assert fake_history[0].engine == "POSTGRES"
        # tmp 파일은 rename으로 사라졌어야 한다 - 잔여 .tmp-* 없음.
        assert not any(p.name.endswith(".tmp") or ".tmp-" in p.name for p in tmp_path.iterdir())

    def test_existing_final_file_blocks_overwrite(self, monkeypatch, tmp_path, fake_history):
        self._setup_common(monkeypatch, tmp_path)
        started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        stem = f"erp_postgres_{started_at.strftime('%Y%m%d_%H%M%S')}_UTC"
        (tmp_path / f"{stem}.dump").write_bytes(b"pre-existing")

        called = {"n": 0}

        def fail_if_called(*a, **k):
            called["n"] += 1
            raise AssertionError("덮어쓰기 차단보다 먼저 pg_dump가 실행되면 안 됩니다.")

        monkeypatch.setattr(svc.subprocess, "run", fail_if_called)

        with pytest.raises(svc.PostgresBackupError) as exc_info:
            svc._run_backup_locked(started_at)

        assert exc_info.value.error_code == svc.ERR_RENAME_FAILURE
        assert called["n"] == 0
        assert (tmp_path / f"{stem}.dump").read_bytes() == b"pre-existing"  # 기존 파일 보존.

    def test_retention_failure_after_success_is_partial_success_not_failed(self, monkeypatch, tmp_path, fake_history):
        self._setup_common(monkeypatch, tmp_path)
        monkeypatch.setattr(svc.subprocess, "run", _fake_dump_success(tmp_path))

        def failing_retention(_backup_dir):
            raise svc.PostgresBackupError(svc.ERR_RETENTION_FAILURE, "정리 실패")

        monkeypatch.setattr(svc, "_apply_retention", failing_retention)

        started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = svc._run_backup_locked(started_at)  # 예외를 던지지 않아야 한다.

        assert result["status"] == "PARTIAL_SUCCESS"
        assert result["error_code"] == svc.ERR_RETENTION_FAILURE
        assert fake_history[0].status == "PARTIAL_SUCCESS"
        # 방금 만든 백업 파일 자체는 그대로 남아 있어야 한다(폐기 금지).
        final_path = tmp_path / result["file_name"]
        assert final_path.exists()

    def test_dump_failure_preserves_preexisting_good_backup(self, monkeypatch, tmp_path, fake_history):
        self._setup_common(monkeypatch, tmp_path)
        old_stem = "erp_postgres_20250101_000000_UTC"
        (tmp_path / f"{old_stem}.dump").write_bytes(b"good-old-backup")
        (tmp_path / f"{old_stem}.dump.sha256").write_text("abc")

        def failing_dump(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, returncode=1, stdout=b"", stderr=b"permission denied")

        monkeypatch.setattr(svc.subprocess, "run", failing_dump)

        started_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
        with pytest.raises(svc.PostgresBackupError) as exc_info:
            svc._run_backup_locked(started_at)

        assert exc_info.value.error_code == svc.ERR_DUMP_FAILURE
        assert (tmp_path / f"{old_stem}.dump").read_bytes() == b"good-old-backup"
        assert (tmp_path / f"{old_stem}.dump.sha256").exists()

    def test_pgpassfile_is_removed_even_on_failure(self, monkeypatch, tmp_path, fake_history):
        self._setup_common(monkeypatch, tmp_path)

        def failing_dump(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, returncode=1, stdout=b"", stderr=b"boom")

        monkeypatch.setattr(svc.subprocess, "run", failing_dump)

        started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with pytest.raises(svc.PostgresBackupError):
            svc._run_backup_locked(started_at)

        leftover = [p for p in tmp_path.iterdir() if p.name.startswith(".pgpass_")]
        assert leftover == []


# --------------------------------------------------------------------------
# run_backup_job() - lock 분기 (advisory lock 자체는 fake connection으로 대체)
# --------------------------------------------------------------------------


class FakeLockResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class FakeConnection:
    def __init__(self, lock_available: bool):
        self.lock_available = lock_available
        self.executed: list[tuple[str, object]] = []
        self.closed = False

    def execution_options(self, **kwargs):
        return self

    def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        if "pg_try_advisory_lock" in str(stmt):
            return FakeLockResult(self.lock_available)
        return FakeLockResult(None)

    def close(self):
        self.closed = True


class FakeEngine:
    def __init__(self, conn: FakeConnection):
        self._conn = conn

    def connect(self):
        return self._conn


class TestRunBackupJobLocking:
    def test_lock_busy_skips_without_raising_and_records_already_running(self, monkeypatch, fake_history):
        monkeypatch.setattr(settings, "postgres_backup_enabled", True)
        fake_conn = FakeConnection(lock_available=False)
        monkeypatch.setattr(svc, "engine", FakeEngine(fake_conn))

        def _fail_if_called(started_at):
            raise AssertionError("잠금 실패인데 실제 백업이 시작되면 안 됩니다.")

        monkeypatch.setattr(svc, "_run_backup_locked", _fail_if_called)

        result = svc.run_backup_job()

        assert result == {"status": "ALREADY_RUNNING"}
        assert fake_history[0].status == "ALREADY_RUNNING"
        assert fake_conn.closed is True

    def test_lock_acquired_runs_backup_and_releases_lock(self, monkeypatch, fake_history):
        monkeypatch.setattr(settings, "postgres_backup_enabled", True)
        fake_conn = FakeConnection(lock_available=True)
        monkeypatch.setattr(svc, "engine", FakeEngine(fake_conn))
        monkeypatch.setattr(svc, "_run_backup_locked", lambda started_at: {"status": "SUCCESS"})

        result = svc.run_backup_job()

        assert result == {"status": "SUCCESS"}
        unlock_calls = [e for e in fake_conn.executed if "pg_advisory_unlock" in e[0]]
        assert len(unlock_calls) == 1
        assert fake_conn.closed is True

    def test_backup_failure_records_failed_and_reraises(self, monkeypatch, fake_history):
        monkeypatch.setattr(settings, "postgres_backup_enabled", True)
        fake_conn = FakeConnection(lock_available=True)
        monkeypatch.setattr(svc, "engine", FakeEngine(fake_conn))

        def raise_dump_failure(started_at):
            raise svc.PostgresBackupError(svc.ERR_DUMP_FAILURE, "pg_dump가 실패했습니다.")

        monkeypatch.setattr(svc, "_run_backup_locked", raise_dump_failure)

        with pytest.raises(svc.PostgresBackupError) as exc_info:
            svc.run_backup_job()

        assert exc_info.value.error_code == svc.ERR_DUMP_FAILURE
        assert fake_history[0].status == "FAILED"
        assert fake_history[0].error_code == svc.ERR_DUMP_FAILURE
        assert fake_conn.closed is True  # 실패해도 lock 해제 후 커넥션을 반환한다.

    def test_connection_failure_records_failed_without_lock_attempt(self, monkeypatch, fake_history):
        monkeypatch.setattr(settings, "postgres_backup_enabled", True)

        class FailingEngine:
            def connect(self):
                raise OSError("연결 실패")

        monkeypatch.setattr(svc, "engine", FailingEngine())

        with pytest.raises(svc.PostgresBackupError) as exc_info:
            svc.run_backup_job()

        assert exc_info.value.error_code == svc.ERR_CONNECTION_FAILURE
        assert fake_history[0].status == "FAILED"
        assert fake_history[0].error_code == svc.ERR_CONNECTION_FAILURE
