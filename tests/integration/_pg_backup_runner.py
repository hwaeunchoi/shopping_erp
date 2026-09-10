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


ACTIONS = {
    "migrate_and_seed": _action_migrate_and_seed,
    "run_backup": _action_run_backup,
    "lock_contention_check": _action_lock_contention_check,
    "fingerprint": _fingerprint,
    "source_unchanged": _fingerprint,
    "restore": _action_restore,
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
