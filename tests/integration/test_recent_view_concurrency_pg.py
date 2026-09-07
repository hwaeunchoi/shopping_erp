"""
tests/integration/test_recent_view_concurrency_pg.py
--------------------------------------------------------------
repositories.extra_repository.RecentViewRepository.touch()의 원자적 UPSERT
(INSERT ... ON CONFLICT DO UPDATE)가 실제 동시 요청(서로 다른 DB 커넥션/세션)
환경에서도 정확히 한 행만 남기고 예외 없이 끝나는지 "진짜" PostgreSQL로
검증한다 - 운영 erp-postgres가 아니라 이 테스트 전용 1회성 컨테이너를 쓴다.

왜 필요한가: 예전 구현("조회 후 없으면 INSERT")은 두 요청이 동시에 같은
(user_id, target_type, target_id)를 처음 기록하려 하면 조회 시점엔 둘 다
"없음"으로 보여(TOCTOU) 하나가 uq_recent_view 유니크 제약 위반
(IntegrityError -> 처리 안 하면 500)을 던졌다(실제 발견된 결함). SQLite
(단위테스트 기본 DB)는 파일 단위 잠금으로 이 경합을 그대로 재현하지 못해
(두 커넥션이 진짜로 동시에 쓰기를 시도하는 상황 자체가 SQLite에서는 직렬화
되어 버린다), 실제 두 커넥션·barrier로 시점을 겹쳐 검증하려면 다중 연결
동시쓰기를 실제로 지원하는 DB가 필요하다.

격리 원칙(tests/integration/test_product_sync_concurrency_pg.py와 동일):
- 고유 임시 docker network(--internal, 호스트 포트 미노출)/컨테이너/볼륨(uuid
  접미사)만 사용한다.
- 테스트 자신도 host에서 이 임시 DB에 직접 접속하지 않는다 - 같은 network에
  붙은 "러너" 컨테이너(기존 shopping_erp-api 이미지 재사용, 레포를 읽기전용으로
  마운트해 최신 코드를 그대로 실행) 안에서 실제 동시성 시나리오를 돌리고, 그
  표준출력(한 줄 JSON)만 host에서 파싱한다.
- 합성(synthetic) 데이터만 사용한다 - 실제 Secret/운영 데이터는 전혀 관여하지 않는다.
- 테스트 종료 후(성공/실패 무관) 컨테이너·network·volume을 전부 제거한다.
- 운영 컨테이너(erp-postgres)·운영 볼륨(erp_pg_data)에는 다른 이름/다른 네트워크라
  애초에 접근할 수 없다.
- Docker CLI가 없는 환경에서는 skip한다.
- 각 docker 호출에 timeout을 둬 테스트가 무한정 멈추지 않는다.
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
RUNNER_SCRIPT_REL = "tests/integration/_recent_view_concurrency_runner.py"
RUNNER_IMAGE = "shopping_erp-api:latest"

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI가 없는 환경 - 통합 테스트 skip")


def _docker(*args: str, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess:
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


@pytest.fixture()
def pg_sandbox():
    if not _docker_available():
        pytest.skip("Docker 데몬에 연결할 수 없어 통합 테스트를 skip합니다.")

    suffix = uuid.uuid4().hex[:10]
    network = f"rvconc_test_net_{suffix}"
    volume = f"rvconc_test_vol_{suffix}"
    container = f"rvconc_test_db_{suffix}"
    db_user = "rvconc_test_user"
    db_password = f"rvconc-{suffix}-pw"
    db_name = "rvconc_test_db"

    _docker("network", "create", "--internal", network)
    _docker("volume", "create", volume)

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
            raise RuntimeError("임시 PostgreSQL 컨테이너가 제한 시간 내에 준비되지 않았습니다.")

        yield {
            "network": network,
            "container": container,
            "db_user": db_user,
            "db_name": db_name,
            "db_password": db_password,
        }
    finally:
        _docker("rm", "-f", container, check=False)
        _docker("volume", "rm", "-f", volume, check=False)
        _docker("network", "rm", network, check=False)


def _run_scenario(sandbox: dict, scenario: str, timeout: int = 90) -> dict[str, Any]:
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            sandbox["network"],
            "-v",
            f"{REPO_ROOT.as_posix()}:/app:ro",
            "-w",
            "/app",
            "-e",
            f"RVCONC_DB_HOST={sandbox['container']}",
            "-e",
            f"RVCONC_DB_USER={sandbox['db_user']}",
            "-e",
            f"RVCONC_DB_PASSWORD={sandbox['db_password']}",
            "-e",
            f"RVCONC_DB_NAME={sandbox['db_name']}",
            "-e",
            f"RVCONC_SCENARIO={scenario}",
            "-e",
            "MSYS_NO_PATHCONV=1",
            RUNNER_IMAGE,
            "python",
            RUNNER_SCRIPT_REL,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    assert result.returncode == 0, (
        f"runner 컨테이너가 비정상 종료(exit={result.returncode})했습니다.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    last_line = result.stdout.strip().splitlines()[-1]
    return json.loads(last_line)


class TestConcurrentFirstInsert:
    def test_two_concurrent_first_touches_leave_exactly_one_row_and_neither_errors(self, pg_sandbox):
        payload = _run_scenario(pg_sandbox, "concurrent_first_insert")

        assert payload["a"]["error"] is None, payload
        assert payload["b"]["error"] is None, payload
        assert payload["row_count"] == 1
        assert payload["a"]["id"] == payload["b"]["id"]
        # 충돌 처리 때문에 호출자의 관계없는 변경까지 되돌아가면 안 된다.
        assert payload["a"]["unrelated_committed"] is True
        assert payload["b"]["unrelated_committed"] is True


class TestConcurrentUpdateExisting:
    def test_two_concurrent_touches_on_existing_row_stay_single_row(self, pg_sandbox):
        payload = _run_scenario(pg_sandbox, "concurrent_update_existing")

        assert payload["a"]["error"] is None, payload
        assert payload["b"]["error"] is None, payload
        assert payload["row_count"] == 1
        assert payload["a"]["id"] == payload["b"]["id"]
        assert payload["a"]["unrelated_committed"] is True
        assert payload["b"]["unrelated_committed"] is True
