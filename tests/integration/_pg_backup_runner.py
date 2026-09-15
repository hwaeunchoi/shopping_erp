"""
tests/integration/_pg_backup_runner.py
------------------------------------------
tests/integration/test_postgres_backup_restore_pg.py 전용 러너 스크립트.
격리된 테스트 PostgreSQL 컨테이너를 상대로, 실제 candidate 이미지(pg_dump/
pg_restore 포함) 컨테이너 "안에서" 최신 애플리케이션 코드를 그대로 실행한다.

표준출력은 항상 마지막 줄에 한 줄 JSON만 남긴다(상태값·건수·해시만 - 비밀번호나
DATABASE_URL 원문, 실제 행 데이터는 절대 포함하지 않는다). 실패해도 스택
트레이스를 그대로 흘리지 않고 {"ok": false, "error": "<type>: <짧은 메시지>"}
형태로만 보고한다.

PGBACKUP_ACTION 환경변수로 동작을 고른다:
- migrate_and_seed : alembic upgrade head 후 scripts/init_db.py의 기존 시딩
  루틴(합성 역할/권한/관리자/플랫폼/창고 데이터)을 그대로 실행한다.
- run_backup       : postgres_backup_service.run_backup_job()을 1회 호출한다.
- lock_contention_check : advisory lock을 직접 잡은 채로 run_backup_job()을
  호출해 ALREADY_RUNNING을 반환하는지 확인하고, lock을 푼 뒤 다시 호출해
  정상 진행되는지 확인한다.
- fingerprint      : 지정한 DB의 안전한 지문(alembic revision, 주요 테이블
  row count, 권한 코드 집합 해시, 플랫폼 code/is_active)만 계산한다.
- restore          : PGBACKUP_DUMP_PATH의 dump 파일을 pg_restore로 지정한
  TARGET DB에 복원한다.
- source_unchanged : SOURCE DB의 지문이 백업 전후로 그대로인지 fingerprint와
  동일한 계산을 한 번 더 수행해 호출부가 직접 비교하도록 한다(fingerprint와
  동작은 같지만 의도를 드러내는 별도 액션명).
- seed_backup_history : fix/postgres-backup-missed-run-recovery 검증용.
  PGBACKUP_SEED_CREATED_AT(ISO-8601 UTC, 예: "2026-09-11T03:00:05")와
  PGBACKUP_SEED_STATUS(기본 SUCCESS)로 backup_history에 과거 성공 기록을
  1건 직접 시딩한다("마지막 성공 시각을 과거로 시딩"하는 검증 1단계).
- run_catchup : postgres_backup_service.run_catchup_if_needed()를 1회 호출한다.
  PGBACKUP_CATCHUP_NOW(ISO-8601 UTC)가 있으면 그 시각을 now_fn으로 주입하고,
  없으면 실제 UTC 현재 시각을 쓴다. 여러 컨테이너에서 이 액션을 동시에 실행해
  "두 scheduler 프로세스 동시 시작"을 재현한다.
- backup_history_summary : backup_history 테이블에서 engine/status/
  trigger_type만 뽑아 목록으로 반환한다(민감정보 없음) - 정책대로 1건만
  CATCHUP으로 기록됐는지 검증한다.
- backup_dir_listing : POSTGRES_BACKUP_DIR 아래 실제 dump 파일 개수와
  잔존 임시(.tmp)/PGPASSFILE 개수만 보고한다(경로 문자열 자체도 비밀번호를
  담지 않으므로 그대로 반환 가능).
"""

import hashlib
import json
import os
import subprocess
import sys

sys.path.insert(0, "/app")


def _fail(exc: BaseException) -> None:
    print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}))
    sys.exit(1)


def _action_migrate_and_seed() -> dict:
    from alembic import command
    from alembic.config import Config

    cfg = Config("/app/alembic.ini")
    command.upgrade(cfg, "head")

    import scripts.init_db as init_db

    init_db.main()
    return {"ok": True}


def _action_run_backup() -> dict:
    from scheduler.jobs import backup_job

    result = backup_job.run()
    return {"ok": True, "result": result}


def _action_lock_contention_check() -> dict:
    from sqlalchemy import text

    from core.database import engine
    from services import postgres_backup_service as svc

    holder = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    got = holder.execute(
        text("SELECT pg_try_advisory_lock(:c, :o)"), {"c": svc._ADVISORY_LOCK_CLASSID, "o": svc._ADVISORY_LOCK_OBJID}
    ).scalar()
    if not got:
        holder.close()
        raise RuntimeError("사전 조건 실패: 테스트 시작 시점에 advisory lock을 잡지 못했습니다.")

    busy_result = svc.run_backup_job()

    holder.execute(
        text("SELECT pg_advisory_unlock(:c, :o)"), {"c": svc._ADVISORY_LOCK_CLASSID, "o": svc._ADVISORY_LOCK_OBJID}
    )
    holder.close()

    after_release_result = svc.run_backup_job()

    return {"ok": True, "busy_result": busy_result, "after_release_result": after_release_result}


def _fingerprint() -> dict:
    from sqlalchemy import text

    from core.database import engine

    with engine.connect() as conn:
        revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        table_count = conn.execute(
            text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
        ).scalar()
        role_count = conn.execute(text("SELECT count(*) FROM roles")).scalar()
        user_count = conn.execute(text("SELECT count(*) FROM users")).scalar()
        warehouse_count = conn.execute(text("SELECT count(*) FROM warehouses")).scalar()
        permission_codes = [r[0] for r in conn.execute(text("SELECT code FROM permissions ORDER BY code"))]
        permission_code_sha256 = hashlib.sha256("|".join(permission_codes).encode()).hexdigest()
        platform_rows = [
            f"{r[0]}:{r[1]}" for r in conn.execute(text("SELECT code, is_active FROM platforms ORDER BY code"))
        ]
        platform_sha256 = hashlib.sha256("|".join(platform_rows).encode()).hexdigest()
        role_permission_count = conn.execute(text("SELECT count(*) FROM role_permissions")).scalar()

    return {
        "ok": True,
        "alembic_revision": revision,
        "table_count": table_count,
        "role_count": role_count,
        "user_count": user_count,
        "warehouse_count": warehouse_count,
        "permission_count": len(permission_codes),
        "permission_code_sha256": permission_code_sha256,
        "platform_sha256": platform_sha256,
        "role_permission_count": role_permission_count,
    }


def _action_restore() -> dict:
    dump_path = os.environ["PGBACKUP_DUMP_PATH"]
    target_url = os.environ["PGBACKUP_TARGET_DATABASE_URL"]

    # 이미 검증된 파서/이스케이프 로직(services/postgres_backup_service.py)을
    # 그대로 재사용한다 - host/port/username/database가 전부 str로 보장된다
    # (parse_postgres_url이 없으면 INVALID_CONFIGURATION을 던진다).
    from services.postgres_backup_service import _pgpass_escape, parse_postgres_url

    parsed = parse_postgres_url(target_url)

    pgpass_line = ":".join(
        _pgpass_escape(v) for v in (parsed.host, str(parsed.port), parsed.database, parsed.username, parsed.password)
    )
    pgpass_path = "/tmp/.pgpass_restore_test"
    with open(pgpass_path, "w", encoding="utf-8") as fh:
        fh.write(pgpass_line + "\n")
    os.chmod(pgpass_path, 0o600)

    env = dict(os.environ)
    env["PGPASSFILE"] = pgpass_path
    cmd = [
        "pg_restore",
        "--no-password",
        "--host",
        parsed.host,
        "--port",
        str(parsed.port),
        "--username",
        parsed.username,
        "--dbname",
        parsed.database,
        dump_path,
    ]
    result = subprocess.run(cmd, env=env, capture_output=True, timeout=120, check=False, shell=False)
    os.remove(pgpass_path)

    # pg_restore가 --clean 없이 빈 DB에 복원할 때 이미 존재하는 시퀀스/확장 관련
    # 경고(exit code 1이지만 실질적으로 데이터는 전부 들어간 경우)가 흔하므로,
    # 실제 검증은 이 액션 뒤에 별도로 fingerprint 액션을 호출해 판단한다 - 여기서는
    # pg_restore 자체의 종료코드와 stderr 바이트 길이만 보고한다(원문 미포함).
    return {"ok": True, "returncode": result.returncode, "stderr_bytes": len(result.stderr or b"")}


def _action_seed_backup_history() -> dict:
    from datetime import datetime

    from core.database import session_scope
    from models.system import BackupHistory
    from repositories.system_repository import BackupHistoryRepository

    created_at = datetime.fromisoformat(os.environ["PGBACKUP_SEED_CREATED_AT"])
    status = os.environ.get("PGBACKUP_SEED_STATUS", "SUCCESS")

    with session_scope() as db:
        BackupHistoryRepository(db).add(
            BackupHistory(
                file_path="/pgbackup/seeded-for-catchup-test.dump",
                file_size_bytes=1,
                status=status,
                created_at=created_at,
                engine="POSTGRES",
                trigger_type="SCHEDULE",
            )
        )
    return {"ok": True}


def _action_run_catchup() -> dict:
    from datetime import datetime, timezone

    from services import postgres_backup_service as svc

    now_override = os.environ.get("PGBACKUP_CATCHUP_NOW")
    if now_override:
        fixed_now = datetime.fromisoformat(now_override).replace(tzinfo=timezone.utc)
        result = svc.run_catchup_if_needed(now_fn=lambda: fixed_now)
    else:
        result = svc.run_catchup_if_needed()
    return {"ok": True, "result": result}


def _action_backup_history_summary() -> dict:
    from sqlalchemy import text

    from core.database import engine

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT engine, status, trigger_type FROM backup_history "
                "WHERE engine = 'POSTGRES' ORDER BY created_at"
            )
        ).all()
    return {"ok": True, "rows": [list(r) for r in rows]}


def _action_backup_dir_listing() -> dict:
    import tempfile
    from pathlib import Path

    backup_dir = Path(os.environ.get("POSTGRES_BACKUP_DIR", "/pgbackup"))
    all_files = [p.name for p in backup_dir.iterdir()] if backup_dir.exists() else []
    dump_files = [n for n in all_files if n.endswith(".dump")]
    tmp_files = [n for n in all_files if ".tmp" in n]

    # PGPASSFILE은 backup_dir이 아니라 tempfile.mkstemp() 기본 임시 디렉터리(컨테이너
    # 안에서는 보통 /tmp)에 ".pgpass_*.tmp"로 만들어졌다가 finally에서 unlink된다 -
    # 두 위치 모두 잔존 여부를 확인해야 "PGPASSFILE 잔존 0건"을 실제로 증명한다.
    tmp_root = Path(tempfile.gettempdir())
    pgpass_files = [p.name for p in tmp_root.glob(".pgpass_*")] if tmp_root.exists() else []

    return {
        "ok": True,
        "dump_file_count": len(dump_files),
        "tmp_file_count": len(tmp_files),
        "pgpass_file_count": len(pgpass_files),
    }


ACTIONS = {
    "migrate_and_seed": _action_migrate_and_seed,
    "run_backup": _action_run_backup,
    "lock_contention_check": _action_lock_contention_check,
    "fingerprint": _fingerprint,
    "source_unchanged": _fingerprint,
    "restore": _action_restore,
    "seed_backup_history": _action_seed_backup_history,
    "run_catchup": _action_run_catchup,
    "backup_history_summary": _action_backup_history_summary,
    "backup_dir_listing": _action_backup_dir_listing,
}


def main() -> None:
    action_name = os.environ.get("PGBACKUP_ACTION", "")
    action = ACTIONS.get(action_name)
    if action is None:
        print(json.dumps({"ok": False, "error": f"알 수 없는 PGBACKUP_ACTION: {action_name}"}))
        sys.exit(1)

    try:
        output = action()
    except BaseException as exc:  # noqa: BLE001 - 러너는 항상 안전한 JSON 한 줄만 남긴다.
        _fail(exc)
        return

    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
