"""
tests/integration/test_cs_case_concurrency_pg.py
--------------------------------------------------------------
services.cs_case_service.CsCaseService/services.cs_channel_sync_service.
CsChannelSyncService의 두 동시성 지점(같은 케이스 동시 담당자배정 / 같은
외부문의ID 동시 최초수집)을 실제 PostgreSQL 두 커넥션 경합으로 검증한다 -
운영 erp-postgres가 아니라 이 테스트 전용 1회성 컨테이너를 쓴다.

격리 원칙(tests/integration/test_fulfillment_concurrency_pg.py와 동일):
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
RUNNER_SCRIPT_REL = "tests/integration/_cs_case_concurrency_runner.py"
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
    network = f"csconc_test_net_{suffix}"
    volume = f"csconc_test_vol_{suffix}"
    container = f"csconc_test_db_{suffix}"
    db_user = "csconc_test_user"
    db_password = f"csconc-{suffix}-pw"
    db_name = "csconc_test_db"

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
            f"CSCONC_DB_HOST={sandbox['container']}",
            "-e",
            f"CSCONC_DB_USER={sandbox['db_user']}",
            "-e",
            f"CSCONC_DB_PASSWORD={sandbox['db_password']}",
            "-e",
            f"CSCONC_DB_NAME={sandbox['db_name']}",
            "-e",
            f"CSCONC_SCENARIO={scenario}",
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


class TestConcurrentAssignmentSameCase:
    def test_two_workers_same_case_exactly_one_wins(self, pg_sandbox):
        payload = _run_scenario(pg_sandbox, "concurrent_assignment_same_case")

        assert payload["a"]["exception"] is None, payload
        assert payload["b"]["exception"] is None, payload
        outcomes = {payload["a"]["outcome"], payload["b"]["outcome"]}
        assert outcomes == {"ACCEPTED", "REJECTED"}, payload
        assert payload["final_assignee_id"] in (payload["agent_a"], payload["agent_b"]), payload


class TestConcurrentDuplicateExternalInquiry:
    def test_two_workers_same_external_id_leave_exactly_one_case(self, pg_sandbox):
        payload = _run_scenario(pg_sandbox, "concurrent_duplicate_external_inquiry")

        assert payload["a"]["exception"] is None, payload
        assert payload["b"]["exception"] is None, payload
        assert payload["row_count"] == 1, payload
        # 정확히 하나는 created=1(SUCCESS), 다른 하나는 유니크 제약 위반을
        # SAVEPOINT로 흡수해 failed=1(PARTIAL_SUCCESS 또는 FAILED)이어야 한다 -
        # 둘 다 created=1이면 유니크 제약이 실제로 경합을 못 막은 것이다.
        created_counts = sorted([payload["a"]["result"]["created"], payload["b"]["result"]["created"]])
        assert created_counts == [0, 1], payload
