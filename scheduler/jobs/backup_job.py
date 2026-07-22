"""
scheduler/jobs/backup_job.py
---------------------------------
DB 파일을 backup_dir로 복사하고 backup_history에 기록한 뒤, 보관정책
(backup_retention_days/backup_max_count, config/settings.py)에 따라
오래된 백업을 정리한다.

현재는 SQLite 파일 복사 방식으로만 구현되어 있다. PostgreSQL로 전환하면
이 구현은 pg_dump 기반으로 교체해야 한다(설계 방침: core/database.py의
DATABASE_URL 교체만으로 나머지 계층은 그대로 두는 것과 동일한 원칙이지만,
백업은 DB 엔진에 종속적인 작업이라 이 잡만 별도로 교체가 필요하다).
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
    if not settings.database_url.startswith("sqlite"):
        raise NotImplementedError("SQLite 이외 DB의 백업은 아직 지원하지 않습니다 (pg_dump 등으로 교체 필요).")

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
            )
        )

    removed = _apply_retention()
    return {"status": status_, "file_path": str(backup_path), "removed_old_backups": removed}


def _apply_retention() -> int:
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
