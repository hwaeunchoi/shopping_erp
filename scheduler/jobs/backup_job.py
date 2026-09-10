"""
scheduler/jobs/backup_job.py
---------------------------------
DB 종류에 따라 백업 구현을 분기하는 얇은 진입점.

- SQLite: DB 파일을 backup_dir로 복사하고 backup_history에 기록한 뒤, 보관정책
  (backup_retention_days/backup_max_count, config/settings.py)에 따라 오래된
  백업을 정리한다(기존 구현 그대로 - 상용 ERP 확장(PostgreSQL 예약 백업)
  작업으로 동작을 바꾸지 않았다).
- PostgreSQL: services/postgres_backup_service.py로 위임한다(pg_dump
  --format=custom + pg_restore --list 구조 검증 + SHA-256 + 원자적 전환 +
  보존정책 + advisory lock 기반 동시 실행 방지). settings.postgres_backup_enabled가
  기본 False라 별도 활성화 전에는 이 분기도 즉시 skipped_disabled를 반환한다
  (import도 postgres_backup_service.run_backup_job() 안에서 첫 줄에 플래그를
  확인하므로, OFF 상태에서 DB 세션·pg_dump 실행·backup 디렉터리 접근이 전혀
  없다는 보장은 그 모듈이 그대로 진다).
"""

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from config.settings import settings
from core.database import session_scope
from models.system import BackupHistory
from repositories.system_repository import BackupHistoryRepository


def run() -> dict:
    if settings.database_url.startswith("sqlite"):
        return _run_sqlite_backup()

    if settings.database_url.startswith(("postgresql", "postgres")):
        from services import postgres_backup_service

        return postgres_backup_service.run_backup_job()

    raise NotImplementedError(
        f"지원하지 않는 DB 엔진입니다: {settings.database_url.split(':', 1)[0]} (SQLite/PostgreSQL만 지원)."
    )


def _run_sqlite_backup() -> dict:
    db_path = Path(settings.database_url.replace("sqlite:///", "", 1))
    now = datetime.now(timezone.utc)
    # .gitignore의 backup/*/ 패턴(하위 디렉터리만 무시)에 맞춰 날짜별 하위 디렉터리에 저장한다.
    day_dir = settings.backup_dir / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    backup_path = day_dir / f"erp_{now.strftime('%H%M%S')}.db"

    status_ = "SUCCESS"
    error_message: Optional[str] = None
    file_size: Optional[int] = None
    try:
        shutil.copy2(db_path, backup_path)
        file_size = backup_path.stat().st_size
    except OSError as e:
        status_ = "FAILED"
        error_message = str(e)

    with session_scope() as db:
        BackupHistoryRepository(db).add(
            BackupHistory(
                file_path=str(backup_path),
                file_size_bytes=file_size,
                status=status_,
                error_message=error_message,
                created_at=now,
                engine="SQLITE",
            )
        )

    removed = _apply_sqlite_retention()
    return {"status": status_, "file_path": str(backup_path), "removed_old_backups": removed}


def _apply_sqlite_retention() -> int:
    """backup_retention_days보다 오래됐거나 backup_max_count를 초과하는 백업 파일을 삭제한다."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.backup_retention_days)
    backups = sorted(settings.backup_dir.glob("*/erp_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)

    removed = 0
    for idx, path in enumerate(backups):
        too_old = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) < cutoff
        over_max_count = idx >= settings.backup_max_count
        if too_old or over_max_count:
            path.unlink(missing_ok=True)
            removed += 1
    return removed
