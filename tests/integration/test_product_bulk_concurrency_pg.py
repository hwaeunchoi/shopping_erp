"""
tests/integration/test_product_bulk_concurrency_pg.py
--------------------------------------------------------------
services.product_bulk_service.ProductBulkService의 대량 접수가 실제 동시 요청
(서로 다른 DB 커넥션/세션, 즉 "여러 worker 동시 실행")에서도 idempotency_key
경합으로 인해 예외를 흘리거나 중복 명령을 만들지 않는지 "진짜" PostgreSQL로
검증한다 - 운영 erp-postgres가 아니라 이 테스트 전용 1회성 컨테이너를 쓴다.

왜 필요한가: 두 worker가 정확히 같은 순간 같은 대상(product_platform_map_id)에
같은 목표값으로 재고 전송을 접수하면, 두 세션 모두 "기존 명령 없음"을 보고(TOCTOU)
동시에 INSERT를 시도할 수 있다 - idempotency_key 유니크 인덱스가 그중 하나를
IntegrityError로 막는데, services.product_bulk_service._submit_bulk가 이를
배치 중단으로 오인하지 않고 한 번 재시도해 기존 행을 정상적으로 찾아 반환하는지는
실제 두 커넥션이 진짜로 겹쳐 쓰기를 시도해야 확인할 수 있다(SQLite는 파일 단위
잠금으로 이 경합 자체를 재현하지 못한다 - 두 커넥션의 동시 쓰기가 직렬화된다).

격리 원칙(tests/integration/test_recent_view_concurrency_pg.py와 동일):
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
RUNNER_SCRIPT_REL = "tests/integration/_product_bulk_concurrency_runner.py"
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
    network = f"pbconc_test_net_{suffix}"
    volume = f"pbconc_test_vol_{suffix}"
    container = f"pbconc_test_db_{suffix}"
    db_user = "pbconc_test_user"
    db_password = f"pbconc-{suffix}-pw"
    db_name = "pbconc_test_db"

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
            f"PBCONC_DB_HOST={sandbox['container']}",
            "-e",
            f"PBCONC_DB_USER={sandbox['db_user']}",
            "-e",
            f"PBCONC_DB_PASSWORD={sandbox['db_password']}",
            "-e",
            f"PBCONC_DB_NAME={sandbox['db_name']}",
            "-e",
            f"PBCONC_SCENARIO={scenario}",
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


class TestConcurrentBulkSubmitSameTarget:
    def test_two_workers_same_target_same_value_leave_exactly_one_command_and_neither_errors(self, pg_sandbox):
        payload = _run_scenario(pg_sandbox, "concurrent_same_target_same_value")

        assert payload["a"]["exception"] is None, payload
        assert payload["b"]["exception"] is None, payload
        assert payload["a"]["aborted"] is False, payload
        assert payload["b"]["aborted"] is False, payload
        assert payload["row_count"] == 1, payload
        assert payload["a"]["command_id"] == payload["b"]["command_id"] == payload["command_ids"][0]
        assert payload["a"]["outcome"] in ("ACCEPTED", "DUPLICATE_OR_SUPERSEDED")
        assert payload["b"]["outcome"] in ("ACCEPTED", "DUPLICATE_OR_SUPERSEDED")
