"""
tests/integration/test_postgres_backup_restore_pg.py
------------------------------------------------------------
services/postgres_backup_service.py를 "진짜" PostgreSQL + 실제 pg_dump/
pg_restore 바이너리로 검증한다 - 운영 erp-postgres가 아니라 이 테스트 전용
1회성 컨테이너 두 개(SOURCE/TARGET)를 쓴다.

격리 원칙(tests/integration/test_rotate_postgres_password_pg.py와 동일):
- 고유 임시 docker network(--internal, 호스트 포트 미노출)/컨테이너/
  볼륨(모두 uuid 접미사)만 사용한다.
- 테스트 자신도 host에서 이 임시 DB들에 직접 접속하지 않는다 - 같은
  network에 붙은 "러너" 컨테이너 안에서 실제 백업/복원 로직을 돌리고, 그
  표준출력(마지막 줄 한 줄 JSON, 상태값·건수·해시만 - 비밀번호/DB URL/실제
  행 데이터 없음)만 host에서 파싱한다.
- 러너 이미지는 현재 저장소의 Dockerfile로 이 테스트가 직접 빌드한다(운영
  shopping_erp-api:latest를 재사용하지 않는다 - 이 테스트가 검증해야 하는
  대상 자체가 "Dockerfile에 pg_dump/pg_restore가 새로 추가됐는가"이므로).
- 합성(synthetic) 데이터만 사용한다(scripts/init_db.py의 기존 시딩 루틴 -
  역할/권한/관리자 계정/플랫폼/창고) - 실제 운영 데이터는 전혀 관여하지 않는다.
- 테스트 종료 후(성공/실패 무관) 컨테이너·network·volume·이미지를 전부 제거한다.
- 운영 컨테이너(erp-postgres)·운영 볼륨(erp_pg_data)·운영 backup에는 다른
  이름/다른 네트워크라 애초에 접근할 수 없고, 이 테스트는 그것들을 절대
  참조하지 않는다.
- Docker CLI가 없는 환경에서는 skip한다.
"""

import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RUNNER_SCRIPT_REL = "tests/integration/_pg_backup_runner.py"

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI가 없는 환경 - 통합 테스트 skip")


def _docker(*args: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=check,
    )


def _docker_available() -> bool:
    try:
        _docker("info", check=True, timeout=10)
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def runner_image():
    if not _docker_available():
        pytest.skip("Docker 데몬에 연결할 수 없어 통합 테스트를 skip합니다.")

    tag = f"shopping_erp_test/pgbackup_runner:{uuid.uuid4().hex[:10]}"
    build = subprocess.run(
        ["docker", "build", "-t", tag, "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
    )
    if build.returncode != 0:
        pytest.fail(f"러너 이미지 빌드 실패(exit={build.returncode}). stderr 마지막 2000자:\n{build.stderr[-2000:]}")
    try:
        yield tag
    finally:
        _docker("rmi", "-f", tag, check=False)


@pytest.fixture()
def pg_sandbox(runner_image):
    suffix = uuid.uuid4().hex[:10]
    network = f"pgbackup_test_net_{suffix}"
    source_container = f"pgbackup_test_src_{suffix}"
    target_container = f"pgbackup_test_dst_{suffix}"
    backup_volume = f"pgbackup_test_vol_{suffix}"
    source_pg_volume = f"pgbackup_test_srcdata_{suffix}"
    target_pg_volume = f"pgbackup_test_dstdata_{suffix}"
    db_user = "pgbackup_test_user"
    db_password = f"pgbackup-test-{suffix}-0000000000000000"
    db_name = "pgbackup_test_db"

    _docker("network", "create", "--internal", network)
    _docker("volume", "create", backup_volume)
    _docker("volume", "create", source_pg_volume)
    _docker("volume", "create", target_pg_volume)

    def _start_pg(container: str, volume: str) -> None:
        _docker(
            "run",
            "-d",
            "--name",
            container,
            "--network",
            network,
            "-e",
            f"POSTGRES_USER={db_user}",
            "-e",
            f"POSTGRES_PASSWORD={db_password}",
            "-e",
            f"POSTGRES_DB={db_name}",
            "-v",
            f"{volume}:/var/lib/postgresql/data",
            "postgres:16-alpine",
        )
        deadline = time.time() + 60
        ready = False
        while time.time() < deadline:
            r = _docker("exec", container, "pg_isready", "-U", db_user, check=False)
            if r.returncode == 0:
                ready = True
                break
            time.sleep(1)
        if not ready:
            raise RuntimeError(f"임시 PostgreSQL 컨테이너({container})가 제한 시간 내에 준비되지 않았습니다.")

    try:
        _start_pg(source_container, source_pg_volume)
        _start_pg(target_container, target_pg_volume)

        yield {
            "network": network,
            "source_container": source_container,
            "target_container": target_container,
            "backup_volume": backup_volume,
            "db_user": db_user,
            "db_password": db_password,
            "db_name": db_name,
            "source_url": f"postgresql+psycopg://{db_user}:{db_password}@{source_container}:5432/{db_name}",
            "target_url": f"postgresql+psycopg://{db_user}:{db_password}@{target_container}:5432/{db_name}",
        }
    finally:
        _docker("rm", "-f", source_container, check=False)
        _docker("rm", "-f", target_container, check=False)
        _docker("volume", "rm", "-f", backup_volume, check=False)
        _docker("volume", "rm", "-f", source_pg_volume, check=False)
        _docker("volume", "rm", "-f", target_pg_volume, check=False)
        _docker("network", "rm", network, check=False)


def _run_runner(
    runner_image: str,
    sandbox: dict,
    action: str,
    database_url: str,
    extra_env: dict[str, str] | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    env_args: list[str] = [
        "-e",
        f"DATABASE_URL={database_url}",
        "-e",
        f"PGBACKUP_ACTION={action}",
        "-e",
        "JWT_SECRET_KEY=test-only-dummy-jwt-secret-DO-NOT-USE-IN-PRODUCTION-0000",
        "-e",
        "CREDENTIAL_ENCRYPTION_KEY=test-only-dummy-credential-secret-DO-NOT-USE-IN-PRODUCTION-1111",
    ]
    for k, v in (extra_env or {}).items():
        env_args += ["-e", f"{k}={v}"]

    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            sandbox["network"],
            "--user",
            "0:0",  # /pgbackup 볼륨 초기 소유자가 root라 테스트에서만 root로 실행한다(운영 이미지의 USER appuser는 그대로다).
            "--tmpfs",
            "/app/logs",  # 저장소를 :ro로 마운트하므로 scripts/init_db.py의 setup_logging()이 쓸 곳이 필요하다 - 저장소 파일은 건드리지 않는다.
            "-v",
            f"{REPO_ROOT.as_posix()}:/app:ro",
            "-v",
            f"{sandbox['backup_volume']}:/pgbackup",
            "-w",
            "/app",
            *env_args,
            "-e",
            "MSYS_NO_PATHCONV=1",
            runner_image,
            "python",
            RUNNER_SCRIPT_REL,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if result.returncode != 0 and not result.stdout.strip():
        pytest.fail(
            f"러너 실행 자체가 실패했습니다(exit={result.returncode}). stderr 마지막 2000자:\n{result.stderr[-2000:]}"
        )

    last_line = result.stdout.strip().splitlines()[-1]
    payload = json.loads(last_line)
    if not payload.get("ok"):
        pytest.fail(f"러너 액션 '{action}'이 실패했습니다: {payload.get('error')}")
    return payload


@pytest.fixture()
def seeded_source(runner_image, pg_sandbox):
    _run_runner(runner_image, pg_sandbox, "migrate_and_seed", pg_sandbox["source_url"])
    return pg_sandbox


class TestPostgresBackupAndRestore:
    def test_backup_creates_verified_dump_and_restore_matches_source(self, runner_image, seeded_source):
        sandbox = seeded_source

        before_fp = _run_runner(runner_image, sandbox, "fingerprint", sandbox["source_url"])

        backup_result = _run_runner(
            runner_image,
            sandbox,
            "run_backup",
            sandbox["source_url"],
            extra_env={
                "POSTGRES_BACKUP_ENABLED": "true",
                "POSTGRES_BACKUP_DIR": "/pgbackup",
                "POSTGRES_BACKUP_RETENTION_COUNT": "5",
                "POSTGRES_BACKUP_TIMEOUT_SECONDS": "120",
            },
        )
        assert backup_result["result"]["status"] == "SUCCESS"
        file_name = backup_result["result"]["file_name"]
        assert file_name
        assert backup_result["result"]["file_size_bytes"] > 0
        assert backup_result["result"]["sha256"]

        # 백업 직후 SOURCE DB가 변경되지 않았는지 확인(읽기 전용 dump여야 한다).
        after_backup_fp = _run_runner(runner_image, sandbox, "source_unchanged", sandbox["source_url"])
        assert after_backup_fp == before_fp

        # pg_restore --list 구조 검증은 이미 postgres_backup_service.run_backup_job()
        # 내부에서 통과했어야 한다(그렇지 않았다면 status가 SUCCESS가 아니었을 것) -
        # 여기서는 별도 컨테이너의 빈 TARGET DB로 실제 복원까지 수행해 재검증한다.
        restore_result = _run_runner(
            runner_image,
            sandbox,
            "restore",
            sandbox["source_url"],  # 이 액션 자체는 DATABASE_URL을 쓰지 않지만 인자 계약상 채워둔다.
            extra_env={
                "PGBACKUP_DUMP_PATH": f"/pgbackup/{file_name}",
                "PGBACKUP_TARGET_DATABASE_URL": sandbox["target_url"],
            },
        )
        # pg_restore가 첫 복원에서 일부 non-fatal 경고로 0이 아닌 exit을 낼 수 있으므로
        # (예: 소유자 권한 관련 NOTICE) exit code 자체를 단정하지 않고, 실제 데이터가
        # 제대로 들어갔는지는 아래 fingerprint 비교로 판단한다.
        assert isinstance(restore_result["returncode"], int)

        target_fp = _run_runner(runner_image, sandbox, "fingerprint", sandbox["target_url"])

        assert target_fp["alembic_revision"] == before_fp["alembic_revision"]
        assert target_fp["table_count"] == before_fp["table_count"]
        assert target_fp["role_count"] == before_fp["role_count"]
        assert target_fp["user_count"] == before_fp["user_count"]
        assert target_fp["warehouse_count"] == before_fp["warehouse_count"]
        assert target_fp["permission_count"] == before_fp["permission_count"]
        assert target_fp["permission_code_sha256"] == before_fp["permission_code_sha256"]
        assert target_fp["platform_sha256"] == before_fp["platform_sha256"]
        assert target_fp["role_permission_count"] == before_fp["role_permission_count"]

    def test_concurrent_run_is_blocked_by_advisory_lock(self, runner_image, seeded_source):
        sandbox = seeded_source

        result = _run_runner(
            runner_image,
            sandbox,
            "lock_contention_check",
            sandbox["source_url"],
            extra_env={
                "POSTGRES_BACKUP_ENABLED": "true",
                "POSTGRES_BACKUP_DIR": "/pgbackup",
                "POSTGRES_BACKUP_RETENTION_COUNT": "5",
                "POSTGRES_BACKUP_TIMEOUT_SECONDS": "120",
            },
        )
        assert result["busy_result"] == {"status": "ALREADY_RUNNING"}
        assert result["after_release_result"]["status"] == "SUCCESS"

    def test_disabled_flag_makes_no_changes_to_source(self, runner_image, seeded_source):
        sandbox = seeded_source
        before_fp = _run_runner(runner_image, sandbox, "fingerprint", sandbox["source_url"])

        result = _run_runner(
            runner_image, sandbox, "run_backup", sandbox["source_url"], extra_env={"POSTGRES_BACKUP_ENABLED": "false"}
        )
        assert result["result"] == {"skipped_disabled": 1}

        after_fp = _run_runner(runner_image, sandbox, "fingerprint", sandbox["source_url"])
        assert after_fp == before_fp
