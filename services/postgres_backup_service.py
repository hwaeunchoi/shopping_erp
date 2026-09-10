"""
services/postgres_backup_service.py
--------------------------------------
PostgreSQL 환경에서 scheduler가 검증 가능한 custom-format(pg_dump --format=custom)
백업을 만든다(상용 ERP 확장, feature/postgres-scheduled-backup).

기본 비활성화: settings.postgres_backup_enabled(기본값 False)가 꺼져 있으면
run_backup_job()의 첫 줄에서 즉시 반환한다 - 그 아래 어떤 코드도(DB 세션/커넥션
생성, backup 디렉터리 접근, pg_dump 실행) 실행되지 않는다. 다른 상용 ERP 확장
잡(scheduler/jobs/outbox_dispatch_job.py 등)과 동일한 fail-closed 계약이다.

지켜야 하는 안전 원칙:
- 비밀번호를 어떤 CLI 인자에도 넣지 않는다. PGPASSFILE(권한 0600, 일회성 임시
  파일, 성공·실패와 무관하게 즉시 삭제)로만 인증 정보를 전달한다.
- DATABASE_URL 전체나 비밀번호를 로그·예외 메시지·BackupHistory.error_message·
  manifest 파일 어디에도 남기지 않는다.
- pg_dump/pg_restore의 stdout·stderr 원문은 그대로 로그·DB에 저장하지 않는다
  (연결 정보가 섞여 나올 수 있다) - 종료코드와 (dump 실패 분류를 위한) 알려진
  안전 문자열 패턴 매치 결과만 사용하고, 매치에 쓴 원문 자체는 어디에도 다시
  적지 않는다.
- subprocess는 항상 인자 배열로 호출한다(shell=True 금지, 쉘 문자열 조립 없음).
- 최종 백업 파일명은 이 모듈이 타임스탬프로만 생성한다(사용자 입력이 전혀
  섞이지 않는다). 그래도 경로 조작·symlink는 defense-in-depth로 차단한다
  (_safe_backup_path 참고).

동시 실행 방지: PostgreSQL 세션 수준 advisory lock(pg_try_advisory_lock, 논블로킹)을
쓴다 - repositories/integration_sync_repository.py의 acquire_target_lock()이 쓰는
트랜잭션 수준 lock(pg_advisory_xact_lock)과는 다른 메커니즘이다. 이 작업은
pg_dump를 별도 프로세스로 실행하는 동안 SQLAlchemy 트랜잭션을 전혀 열어두지
않으므로(외부 프로세스 실행은 트랜잭션 경계 밖의 일이다) xact lock을 걸 트랜잭션
자체가 없다 - 그래서 커넥션을 하나 명시적으로 체크아웃해 세션 수준 lock을 걸고,
작업이 끝나면(성공·실패·타임아웃 무관) 반드시 pg_advisory_unlock 후 커넥션을
반환한다(연결이 끊기면 PostgreSQL이 세션 lock을 자동 해제하므로 이중 안전망이다).
SQLite에는 advisory lock 개념이 없다 - 이 모듈 자체가 PostgreSQL 전용이므로
무관하다(SQLite 백업은 scheduler/jobs/backup_job.py의 _run_sqlite_backup이
그대로 담당하며 이 모듈과 완전히 독립이다).

오류 처리 계약(scheduler/scheduler.py의 _run_job과 맞물린다):
- ALREADY_RUNNING/DISABLED: 예외를 던지지 않고 정상 반환한다 - _run_job이
  TaskExecutionHistory.status=SUCCESS로 기록한다(잠금 경합이나 OFF 상태를
  "실패"로 과장하지 않는다).
- 백업 자체(덤프 생성~검증~원자적 전환)가 실패하면 PostgresBackupError를 던진다 -
  _run_job이 TaskExecutionHistory.status=FAILED로 기록한다(기존 SQLite 경로가
  NotImplementedError를 던져 FAILED로 표시되던 것과 동일한 관례).
- 백업 자체는 성공했는데 보존정책(retention) 정리만 실패하면 예외를 던지지
  않는다 - BackupHistory.status="PARTIAL_SUCCESS"(기존 코드베이스에서
  ImportExportJob/IntegrationStatus/CsChannelSyncService가 이미 쓰는
  "부분 성공" 표현을 그대로 재사용 - models/extra.py, services/
  cs_channel_sync_service.py 참고)로 남기고, _run_job에는 SUCCESS로 보인다.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import make_url

from config.settings import settings
from core.database import engine, session_scope
from models.system import BackupHistory
from repositories.system_repository import BackupHistoryRepository

logger = logging.getLogger(__name__)

FILE_PREFIX = "erp_postgres_"
FILE_SUFFIX = ".dump"
SIDECAR_SUFFIX = ".sha256"
MANIFEST_SUFFIX = ".manifest.json"

# acquire_target_lock()의 동적 classid(target_type 문자열의 crc32)와 절대 겹치지
# 않도록 이 모듈 전용 고정 상수를 쓴다. 'BKUP'을 4바이트 정수로 눌러쓴 값.
_ADVISORY_LOCK_CLASSID = 0x424B5550
_ADVISORY_LOCK_OBJID = 0

ERR_TOOL_MISSING = "TOOL_MISSING"
ERR_INVALID_CONFIGURATION = "INVALID_CONFIGURATION"
ERR_CONNECTION_FAILURE = "CONNECTION_FAILURE"
ERR_DUMP_TIMEOUT = "DUMP_TIMEOUT"
ERR_DUMP_FAILURE = "DUMP_FAILURE"
ERR_EMPTY_OUTPUT = "EMPTY_OUTPUT"
ERR_RESTORE_LIST_VALIDATION_FAILURE = "RESTORE_LIST_VALIDATION_FAILURE"
ERR_CHECKSUM_FAILURE = "CHECKSUM_FAILURE"
ERR_RENAME_FAILURE = "RENAME_FAILURE"
ERR_RETENTION_FAILURE = "RETENTION_FAILURE"

_CONNECTION_FAILURE_MARKERS = (
    b"could not connect",
    b"connection refused",
    b"could not translate host",
    b"authentication failed",
    b"password authentication failed",
    b"no pg_hba.conf entry",
    b"server closed the connection",
    b"timeout expired",
)


class PostgresBackupError(Exception):
    """PostgreSQL 백업 실패. error_code는 항상 위 ERR_* 상수 중 하나이고,
    safe_message는 DB URL·비밀번호·stderr 원문·PII를 절대 담지 않는다 -
    이 메시지가 그대로 BackupHistory.error_message와 TaskExecutionHistory.
    error_message(scheduler/scheduler.py의 _run_job이 str(e)로 기록)로
    저장되기 때문이다."""

    def __init__(self, error_code: str, safe_message: str) -> None:
        self.error_code = error_code
        self.safe_message = safe_message
        super().__init__(f"[{error_code}] {safe_message}")


@dataclass(frozen=True)
class ParsedPostgresUrl:
    host: str
    port: int
    database: str
    username: str
    password: str


def parse_postgres_url(database_url: str) -> ParsedPostgresUrl:
    """DATABASE_URL을 안전하게 파싱한다(percent-encoded 비밀번호 포함) - 직접
    문자열을 자르지 않고 SQLAlchemy의 검증된 URL 파서(make_url)를 재사용한다.
    postgresql 계열이 아니거나 host/database/username 중 하나라도 없으면
    INVALID_CONFIGURATION으로 명확히 거부한다 - 예외 메시지에는 원본 URL을
    절대 담지 않는다(비밀번호가 포함돼 있을 수 있다)."""
    try:
        url = make_url(database_url)
    except Exception as exc:
        raise PostgresBackupError(
            ERR_INVALID_CONFIGURATION, f"DATABASE_URL 형식이 올바르지 않습니다({type(exc).__name__})."
        ) from exc

    if url.get_backend_name() != "postgresql":
        raise PostgresBackupError(
            ERR_INVALID_CONFIGURATION, "PostgreSQL 백업은 database_url이 postgresql 계열일 때만 지원합니다."
        )
    if not url.host or not url.database or not url.username:
        raise PostgresBackupError(
            ERR_INVALID_CONFIGURATION, "DATABASE_URL에 host/database/username 중 누락된 값이 있습니다."
        )

    return ParsedPostgresUrl(
        host=url.host, port=url.port or 5432, database=url.database, username=url.username, password=url.password or ""
    )


def _pgpass_escape(value: str) -> str:
    """PGPASSFILE 필드 구분자(:)와 이스케이프 문자(\\)를 PostgreSQL 규격대로
    이스케이프한다(참고: https://www.postgresql.org/docs/current/libpq-pgpass.html)."""
    return value.replace("\\", "\\\\").replace(":", "\\:")


def _write_pgpassfile(parsed: ParsedPostgresUrl) -> Path:
    """권한 0600의 일회성 PGPASSFILE을 만든다. mkstemp()는 POSIX에서 이미 생성
    시점부터 0600으로 만들지만, 그 보장에만 기대지 않고 명시적으로 chmod한다
    (테스트에서도 권한을 직접 검증할 수 있도록)."""
    fd, path_str = tempfile.mkstemp(prefix=".pgpass_", suffix=".tmp")
    os.close(fd)
    path = Path(path_str)
    try:
        os.chmod(path, 0o600)
        line = ":".join(
            _pgpass_escape(v)
            for v in (parsed.host, str(parsed.port), parsed.database, parsed.username, parsed.password)
        )
        path.write_text(line + "\n", encoding="utf-8")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def _classify_dump_failure(stderr: bytes) -> str:
    """pg_dump stderr를 알려진 연결 실패 패턴과만 대조해 error_code를 고른다 -
    stderr 원문 자체는 이 함수 밖으로 반환하지 않는다(호출부는 반환된
    error_code만 사용하고 원문은 버린다)."""
    lowered = (stderr or b"").lower()
    if any(marker in lowered for marker in _CONNECTION_FAILURE_MARKERS):
        return ERR_CONNECTION_FAILURE
    return ERR_DUMP_FAILURE


def _run_pg_dump(parsed: ParsedPostgresUrl, pgpass_path: Path, tmp_dump_path: Path, timeout_seconds: int) -> None:
    pg_dump_bin = shutil.which("pg_dump")
    if pg_dump_bin is None:
        raise PostgresBackupError(ERR_TOOL_MISSING, "pg_dump 실행 파일을 찾을 수 없습니다.")

    cmd = [
        pg_dump_bin,
        "--format=custom",
        "--no-password",
        "--host",
        parsed.host,
        "--port",
        str(parsed.port),
        "--username",
        parsed.username,
        "--dbname",
        parsed.database,
        "--file",
        str(tmp_dump_path),
    ]
    env = dict(os.environ)
    env["PGPASSFILE"] = str(pgpass_path)

    try:
        result = subprocess.run(cmd, env=env, capture_output=True, timeout=timeout_seconds, check=False, shell=False)
    except subprocess.TimeoutExpired as exc:
        raise PostgresBackupError(
            ERR_DUMP_TIMEOUT, f"pg_dump가 제한시간({timeout_seconds}초)을 초과해 종료되었습니다."
        ) from exc

    if result.returncode != 0:
        error_code = _classify_dump_failure(result.stderr)
        stderr_len = len(result.stderr or b"")
        raise PostgresBackupError(
            error_code, f"pg_dump가 실패했습니다(exit={result.returncode}, stderr_bytes={stderr_len})."
        )


def _validate_with_restore_list(tmp_dump_path: Path, timeout_seconds: int) -> int:
    pg_restore_bin = shutil.which("pg_restore")
    if pg_restore_bin is None:
        raise PostgresBackupError(ERR_TOOL_MISSING, "pg_restore 실행 파일을 찾을 수 없습니다.")

    try:
        result = subprocess.run(
            [pg_restore_bin, "--list", str(tmp_dump_path)],
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PostgresBackupError(
            ERR_RESTORE_LIST_VALIDATION_FAILURE, "pg_restore --list 검증이 제한시간을 초과했습니다."
        ) from exc

    if result.returncode != 0:
        raise PostgresBackupError(
            ERR_RESTORE_LIST_VALIDATION_FAILURE, f"pg_restore --list 검증에 실패했습니다(exit={result.returncode})."
        )

    entry_count = sum(1 for line in result.stdout.splitlines() if line.strip() and not line.strip().startswith(b";"))
    if entry_count == 0:
        raise PostgresBackupError(
            ERR_RESTORE_LIST_VALIDATION_FAILURE, "pg_restore --list 결과에 유효한 항목이 없습니다."
        )
    return entry_count


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_backup_path(backup_dir: Path, filename: str) -> Path:
    """backup_dir 밖으로 벗어나거나 symlink를 경유하는 경로를 차단한다. filename은
    이 모듈이 타임스탬프만으로 생성한 값만 들어오지만(사용자 입력 없음), 방어적으로
    항상 이 검사를 거친다."""
    if not filename or "/" in filename or "\\" in filename or filename in (".", ".."):
        raise PostgresBackupError(ERR_RENAME_FAILURE, "백업 파일명에 허용되지 않는 문자가 포함되어 있습니다.")

    candidate = backup_dir / filename
    if candidate.is_symlink():
        raise PostgresBackupError(ERR_RENAME_FAILURE, "백업 대상 경로가 symlink입니다.")

    resolved_dir = backup_dir.resolve()
    resolved_candidate = (resolved_dir / filename).resolve()
    if resolved_candidate.parent != resolved_dir:
        raise PostgresBackupError(ERR_RENAME_FAILURE, "백업 경로가 backup 디렉터리를 벗어납니다.")
    return candidate


def _apply_retention(backup_dir: Path) -> int:
    """이 기능이 만든 erp_postgres_*.dump(+.sha256/+.manifest.json 짝)만 정리한다 -
    다른 임의 backup 파일은 절대 건드리지 않는다. 파일명에 YYYYMMDD_HHMMSS가 그대로
    들어있어 문자열 내림차순 정렬이 곧 최신순 정렬이다(파일시스템 mtime 정밀도나
    시계 이슈에 영향받지 않는다). 삭제 대상 목록을 먼저 전부 계산한 뒤에만
    지운다."""
    retention_count = settings.postgres_backup_retention_count
    if retention_count < 2:
        raise PostgresBackupError(
            ERR_RETENTION_FAILURE, "retention 설정값이 유효하지 않습니다(최소 2 이상이어야 합니다)."
        )

    dumps = sorted(backup_dir.glob(f"{FILE_PREFIX}*{FILE_SUFFIX}"), key=lambda p: p.name, reverse=True)
    to_delete = dumps[retention_count:]
    if not to_delete:
        return len(dumps)

    deletion_failed = False
    for dump_path in to_delete:
        for path in (dump_path, Path(str(dump_path) + SIDECAR_SUFFIX), Path(str(dump_path) + MANIFEST_SUFFIX)):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                deletion_failed = True

    if deletion_failed:
        raise PostgresBackupError(ERR_RETENTION_FAILURE, "일부 오래된 백업 파일 삭제에 실패했습니다.")
    return len(dumps) - len(to_delete)


def _record_history(
    *,
    started_at: datetime,
    status: str,
    file_path: str,
    file_size_bytes: Optional[int] = None,
    sha256: Optional[str] = None,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    retained_count: Optional[int] = None,
) -> None:
    with session_scope() as db:
        BackupHistoryRepository(db).add(
            BackupHistory(
                file_path=file_path,
                file_size_bytes=file_size_bytes,
                status=status,
                error_message=error_message[:2000] if error_message else None,
                created_at=started_at,
                engine="POSTGRES",
                sha256=sha256,
                error_code=error_code,
                retained_count=retained_count,
            )
        )


def _run_backup_locked(started_at: datetime) -> dict:
    parsed = parse_postgres_url(settings.database_url)

    backup_dir = settings.postgres_backup_dir
    backup_dir.mkdir(parents=True, exist_ok=True)
    # Windows 호스트 bind mount는 컨테이너 내부 chmod만으로 완전히 제어되지 않는다 -
    # 호스트 ACL은 운영자가 DEPLOYMENT.md 절차대로 별도 설정해야 한다.
    with contextlib.suppress(OSError):
        os.chmod(backup_dir, 0o700)

    file_stem = f"{FILE_PREFIX}{started_at.strftime('%Y%m%d_%H%M%S')}_UTC"
    final_dump_path = _safe_backup_path(backup_dir, file_stem + FILE_SUFFIX)
    if final_dump_path.exists():
        raise PostgresBackupError(ERR_RENAME_FAILURE, "동일한 이름의 백업 파일이 이미 존재합니다(덮어쓰기 금지).")

    tmp_dump_path = backup_dir / f".{file_stem}{FILE_SUFFIX}.tmp-{uuid.uuid4().hex[:8]}"
    pgpass_path = _write_pgpassfile(parsed)
    try:
        _run_pg_dump(parsed, pgpass_path, tmp_dump_path, settings.postgres_backup_timeout_seconds)

        if not tmp_dump_path.exists() or tmp_dump_path.stat().st_size == 0:
            raise PostgresBackupError(ERR_EMPTY_OUTPUT, "pg_dump 결과 파일이 비어 있습니다.")

        entry_count = _validate_with_restore_list(tmp_dump_path, settings.postgres_backup_timeout_seconds)

        try:
            sha256_hex = _sha256_file(tmp_dump_path)
        except OSError as exc:
            raise PostgresBackupError(
                ERR_CHECKSUM_FAILURE, f"SHA-256 계산에 실패했습니다({type(exc).__name__})."
            ) from exc

        file_size = tmp_dump_path.stat().st_size

        try:
            os.chmod(tmp_dump_path, 0o600)
            os.replace(tmp_dump_path, final_dump_path)
        except OSError as exc:
            raise PostgresBackupError(
                ERR_RENAME_FAILURE, f"백업 파일 원자적 전환에 실패했습니다({type(exc).__name__})."
            ) from exc

        sidecar_path = backup_dir / (file_stem + FILE_SUFFIX + SIDECAR_SUFFIX)
        manifest_path = backup_dir / (file_stem + FILE_SUFFIX + MANIFEST_SUFFIX)
        sidecar_path.write_text(f"{sha256_hex}  {final_dump_path.name}\n", encoding="utf-8")
        manifest_path.write_text(
            json.dumps(
                {
                    "file_name": final_dump_path.name,
                    "file_size_bytes": file_size,
                    "sha256": sha256_hex,
                    "restore_list_entry_count": entry_count,
                    "created_at_utc": started_at.isoformat(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    finally:
        pgpass_path.unlink(missing_ok=True)
        tmp_dump_path.unlink(missing_ok=True)  # 성공 시 이미 rename으로 사라져 no-op, 실패 시 잔여 tmp 정리.

    retained_count: Optional[int] = None
    retention_error_code: Optional[str] = None
    try:
        retained_count = _apply_retention(backup_dir)
    except PostgresBackupError as exc:
        # 백업 자체(덤프~검증~원자적 전환)는 이미 끝났다 - retention 실패만으로
        # 방금 만든 정상 백업을 FAILED로 폐기하지 않는다(모듈 docstring 참고).
        retention_error_code = exc.error_code
        logger.warning("PostgreSQL 백업 보존정책 정리 실패(백업 자체는 성공) - error_code=%s", exc.error_code)

    finished_status = "SUCCESS" if retention_error_code is None else "PARTIAL_SUCCESS"
    _record_history(
        started_at=started_at,
        status=finished_status,
        file_path=str(final_dump_path),
        file_size_bytes=file_size,
        sha256=sha256_hex,
        error_code=retention_error_code,
        error_message=None if retention_error_code is None else "보존정책(retention) 정리 중 오류가 발생했습니다.",
        retained_count=retained_count,
    )

    return {
        "status": finished_status,
        "file_name": final_dump_path.name,
        "file_size_bytes": file_size,
        "sha256": sha256_hex,
        "retained_count": retained_count,
        "error_code": retention_error_code,
    }


def run_backup_job() -> dict:
    """scheduler/jobs/backup_job.py에서 호출하는 진입점. database_url이
    PostgreSQL일 때만 호출된다(SQLite 판단은 backup_job.run()이 먼저 한다)."""
    if not settings.postgres_backup_enabled:
        logger.debug("PostgreSQL 예약 백업이 비활성화(OFF) 상태라 건너뜁니다.")
        return {"skipped_disabled": 1}

    started_at = datetime.now(timezone.utc)

    try:
        conn = engine.connect()
    except Exception as exc:
        error = PostgresBackupError(ERR_CONNECTION_FAILURE, f"PostgreSQL 연결에 실패했습니다({type(exc).__name__}).")
        _record_history(
            started_at=started_at,
            status="FAILED",
            file_path="-",
            error_code=error.error_code,
            error_message=error.safe_message,
        )
        raise error from exc

    lock_acquired = False
    try:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        lock_acquired = bool(
            conn.execute(
                text("SELECT pg_try_advisory_lock(:classid, :objid)"),
                {"classid": _ADVISORY_LOCK_CLASSID, "objid": _ADVISORY_LOCK_OBJID},
            ).scalar()
        )
        if not lock_acquired:
            logger.info("PostgreSQL 백업이 이미 실행 중이라 이번 회차는 건너뜁니다(advisory lock busy).")
            _record_history(started_at=started_at, status="ALREADY_RUNNING", file_path="-")
            return {"status": "ALREADY_RUNNING"}

        try:
            return _run_backup_locked(started_at)
        except PostgresBackupError as err:
            _record_history(
                started_at=started_at,
                status="FAILED",
                file_path="-",
                error_code=err.error_code,
                error_message=err.safe_message,
            )
            raise
    finally:
        if lock_acquired:
            try:
                conn.execute(
                    text("SELECT pg_advisory_unlock(:classid, :objid)"),
                    {"classid": _ADVISORY_LOCK_CLASSID, "objid": _ADVISORY_LOCK_OBJID},
                )
            except Exception:  # noqa: BLE001 - 연결이 이미 끊겼어도 무시한다(아래 close가 세션 lock을 어차피 해제).
                logger.warning("advisory unlock 호출에 실패했습니다 - 커넥션 종료로 세션 lock은 함께 해제됩니다.")
        conn.close()
