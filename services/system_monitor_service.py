"""
services/system_monitor_service.py
--------------------------------------
UI v1.1 4장 시스템 모니터링(연동상태/작업이력/시스템상태) 중 "시스템상태" 탭을
계산한다. DB/로그 용량은 실시간 OS 조회, 백업 상태는 backup_history 최신
레코드 기준이다(와이어프레임 4.3절).

uptime은 프로세스가 실제로 얼마나 오래 떠 있었는지를 반영해야 하므로, 이
모듈이 최초로 import되는 시점(=API 프로세스 기동 시점)을 기준으로 잰다.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config.settings import settings
from repositories.system_repository import BackupHistoryRepository

_PROCESS_STARTED_AT = datetime.now(timezone.utc)


@dataclass
class LatestBackupInfo:
    created_at: datetime
    status: str
    file_size_bytes: Optional[int]


@dataclass
class SystemStatus:
    db_size_bytes: int
    log_dir_size_bytes: int
    latest_backup: Optional[LatestBackupInfo]
    uptime_seconds: float


def _dir_size_bytes(directory: Path) -> int:
    if not directory.exists():
        return 0
    return sum(f.stat().st_size for f in directory.rglob("*") if f.is_file())


class SystemMonitorService:
    def __init__(self, session) -> None:
        self.backup_repo = BackupHistoryRepository(session)

    def get_system_status(self) -> SystemStatus:
        db_size_bytes = 0
        if settings.database_url.startswith("sqlite"):
            db_path = Path(settings.database_url.replace("sqlite:///", "", 1))
            if db_path.exists():
                db_size_bytes = db_path.stat().st_size

        log_dir_size_bytes = _dir_size_bytes(settings.logs_dir)

        latest = self.backup_repo.list_recent(limit=1)
        latest_backup = (
            LatestBackupInfo(
                created_at=latest[0].created_at, status=latest[0].status, file_size_bytes=latest[0].file_size_bytes
            )
            if latest
            else None
        )

        uptime_seconds = (datetime.now(timezone.utc) - _PROCESS_STARTED_AT).total_seconds()

        return SystemStatus(
            db_size_bytes=db_size_bytes,
            log_dir_size_bytes=log_dir_size_bytes,
            latest_backup=latest_backup,
            uptime_seconds=uptime_seconds,
        )
