"""
tests/integration/test_alembic_percent_encoded_url_pg.py
------------------------------------------------------------
fix(migrations): support percent-encoded database URLs 검증.

migrations/env.py가 DATABASE_URL(percent-encoded 비밀번호 포함)을 실제
PostgreSQL에 대해 `alembic upgrade head`로 정상 처리하는지 - 운영
erp-postgres가 아니라 이 테스트 전용 1회성 컨테이너를 쓴다.

이 파일이 새로 만드는 것은 없다 - tests/integration/_pg_backup_runner.py의
기존 액션(migrate_and_seed: alembic upgrade head + scripts/init_db.py 시딩,
fingerprint: alembic revision/주요 테이블 안전 지문)을 그대로 재사용해,
DATABASE_URL에 percent-encoded 비밀번호(%40/%3A/%2F/%25 대표 조합 포함)를
넣어 호출한다. 두 번 연속 호출해 "이미 head인 DB에 다시 upgrade" +
"이미 데이터가 있는 DB에 다시 시딩"의 안전성(멱등성)도 함께 확인한다.

격리 원칙(tests/integration/test_postgres_backup_restore_pg.py와 동일):
- 고유 임시 docker network(--internal, 호스트 포트 미노출)/컨테이너/
  볼륨(uuid 접미사)만 사용한다.
- 테스트 자신도 host에서 이 임시 DB에 직접 접속하지 않는다 - 같은 network에
  붙은 "러너" 컨테이너 안에서 실제 alembic/init_db 로직을 돌리고, 그
  표준출력(마지막 줄 한 줄 JSON, 상태값·건수·해시만 - 비밀번호/DB URL 없음)만
  host에서 파싱한다.
- 러너 이미지는 현재 브랜치(이 저장소)의 Dockerfile로 이 테스트가 직접
  빌드한다.
- 합성(synthetic) 비밀번호·데이터만 사용한다 - 실제 운영 데이터/Secret은
  전혀 관여하지 않는다.
- 테스트 종료 후(성공/실패 무관) 컨테이너·network·볼륨·이미지를 전부 제거한다.
- 운영 컨테이너(erp-postgres)·운영 볼륨(erp_pg_data)에는 다른 이름/다른
  네트워크라 애초에 접근할 수 없고, 이 테스트는 그것들을 절대 참조하지 않는다.
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

# 이 브랜치의 alembic 코드 head - migrations/versions에 새 revision을 추가하지
# 않는 한 고정값이다(fix(migrations) 커밋 자체는 migration을 추가하지 않는다).
EXPECTED_ALEMBIC_HEAD = "54810f29bf6a"

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

    tag = f"shopping_erp_test/alembic_url_runner:{uuid.uuid4().hex[:10]}"
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
    network = f"alembicurl_test_net_{suffix}"
    container = f"alembicurl_test_db_{suffix}"
    pg_volume = f"alembicurl_test_data_{suffix}"
    db_user = "alembicurl_test_user"
    # 대표 percent-encoding 조합을 전부 포함하는 합성 비밀번호:
    # 원문 P@ss:w/rd%1 -> @  ->%40, : -> %3A, / -> %2F, % -> %25
    db_password_encoded = "P%40ss%3Aw%2Frd%251"
    db_name = "alembicurl_test_db"

    _docker("network", "create", "--internal", network)
    _docker("volume", "create", pg_volume)

    try:
        _docker(
            "run",
            "-d",
            "--name",
            container,
            "--network",
            network,
            "-e",
            f"POSTGRES_USER={db_user}",
            # 컨테이너 자체 인증에는 디코딩된 원문 비밀번호를 쓴다 - percent-encoding은
            # "DATABASE_URL 문자열 표현" 문제이지 실제 PostgreSQL 비밀번호 값
            # 자체의 문제가 아니다(원문에 @/:///% 같은 URL 예약문자가 섞여
            # 있을 때만 URL 안에서 encoding이 필요하다).
            "-e",
            "POSTGRES_PASSWORD=P@ss:w/rd%1",
            "-e",
            f"POSTGRES_DB={db_name}",
            "-v",
            f"{pg_volume}:/var/lib/postgresql/data",
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

        yield {
            "network": network,
            "container": container,
            "database_url": f"postgresql+psycopg://{db_user}:{db_password_encoded}@{container}:5432/{db_name}",
        }
    finally:
        _docker("rm", "-f", container, check=False)
        _docker("volume", "rm", "-f", pg_volume, check=False)
        _docker("network", "rm", network, check=False)


def _run_runner(runner_image: str, sandbox: dict, action: str, timeout: int = 120) -> dict[str, Any]:
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            sandbox["network"],
            "--user",
            "0:0",
            "--tmpfs",
            "/app/logs",
            "-v",
            f"{REPO_ROOT.as_posix()}:/app:ro",
            "-w",
            "/app",
            "-e",
            f"DATABASE_URL={sandbox['database_url']}",
            "-e",
            f"PGBACKUP_ACTION={action}",
            "-e",
            "JWT_SECRET_KEY=test-only-dummy-jwt-secret-DO-NOT-USE-IN-PRODUCTION-0000",
            "-e",
            "CREDENTIAL_ENCRYPTION_KEY=test-only-dummy-credential-secret-DO-NOT-USE-IN-PRODUCTION-1111",
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


class TestAlembicPercentEncodedDatabaseUrl:
    def test_migrate_and_seed_succeeds_with_percent_encoded_password(self, runner_image, pg_sandbox):
        """1) 새 빈 DB에서 alembic upgrade head. 2) 동일 URL로 scripts/init_db.py
        (애플리케이션의 core.database.SessionLocal 경로)가 정상 연결·시딩 -
        migrations/env.py와 앱의 일반 DB 연결 둘 다 같은 percent-encoded
        URL로 문제없이 동작함을 함께 확인한다."""
        result = _run_runner(runner_image, pg_sandbox, "migrate_and_seed")
        assert result["ok"] is True

    def test_alembic_current_equals_heads_after_upgrade(self, runner_image, pg_sandbox):
        _run_runner(runner_image, pg_sandbox, "migrate_and_seed")
        fp = _run_runner(runner_image, pg_sandbox, "fingerprint")
        assert fp["alembic_revision"] == EXPECTED_ALEMBIC_HEAD

    def test_rerun_on_already_migrated_db_with_existing_data_is_safe(self, runner_image, pg_sandbox):
        """3) 기존 데이터가 있는 DB에서 upgrade + 4) 동일 명령 재실행의 안전성을
        한 번에 검증한다 - 첫 실행으로 이미 head까지 올라가고 데이터가 시딩된
        DB에 정확히 같은 percent-encoded URL로 migrate_and_seed를 다시
        호출해도(alembic upgrade head는 이미 head라 no-op, init_db.py의 각
        시딩 단계는 이미 존재하면 SKIP) 에러 없이 끝나고 지문이 그대로여야
        한다(중복 삽입 없음)."""
        _run_runner(runner_image, pg_sandbox, "migrate_and_seed")
        fp_before = _run_runner(runner_image, pg_sandbox, "fingerprint")

        result_second = _run_runner(runner_image, pg_sandbox, "migrate_and_seed")
        assert result_second["ok"] is True

        fp_after = _run_runner(runner_image, pg_sandbox, "fingerprint")
        assert fp_after == fp_before  # 재실행으로 행이 중복되거나 값이 바뀌지 않았다.
