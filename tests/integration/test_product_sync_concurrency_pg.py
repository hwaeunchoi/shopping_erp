"""
tests/integration/test_product_sync_concurrency_pg.py
--------------------------------------------------------------
services.product_sync_dispatch_service.ProductSyncDispatchService.execute_command()의
"외부 대상 단위 배타 실행"이 실제 동시 worker(서로 다른 DB 커넥션/세션) 환경에서도
보장되는지 "진짜" PostgreSQL로 검증한다 - 운영 erp-postgres가 아니라 이 테스트
전용 1회성 컨테이너를 쓴다.

왜 필요한가: claim()의 행 단위 UPDATE...WHERE는 "같은 명령 행"의 중복 처리만
막는다. 서로 다른 명령 행(형제 매핑, 또는 같은 매핑의 재고/판매상태 명령 쌍)이
같은 외부 대상을 가리키는 경우의 상호 배제는 exists_other_running_for_targets 같은
평범한 SELECT에 의존했는데, execute_command()는 claim()부터 채널 호출·최종 상태
기록까지 커밋 하나 없이 한 트랜잭션으로 진행한다(commit은 호출부(scheduler.jobs.
product_sync_dispatch_job)가 처리를 마친 뒤에야 한다). READ COMMITTED 하에서
그 SELECT는 다른 트랜잭션이 "이미 커밋한" 변경만 보므로, 두 worker가 각자 다른
명령 행을 claim()한 뒤 서로 커밋하기 전에 그 SELECT를 실행하면 TOCTOU로 둘 다
채널을 동시 호출할 수 있다 - 이 위험은 이론적 검토만으로는 확정할 수 없어
실제 두 스레드·두 DB 커넥션·barrier로 시점을 겹쳐 검증한다
(repositories.integration_sync_repository.ExternalCommandRepository.
acquire_target_lock의 PostgreSQL advisory lock이 이 공백을 메운다).

격리 원칙(tests/integration/test_rotate_postgres_password_pg.py와 동일):
- 고유 임시 docker network(--internal, 호스트 포트 미노출)/컨테이너/볼륨(uuid
  접미사)만 사용한다.
- 테스트 자신도 host에서 이 임시 DB에 직접 접속하지 않는다 - 같은 network에
  붙은 "러너" 컨테이너(기존 shopping_erp-api 이미지 재사용, 레포를 읽기전용으로
  마운트해 최신 코드를 그대로 실행) 안에서 실제 동시성 시나리오를 돌리고, 그
  표준출력(한 줄 JSON)만 host에서 파싱한다.
- 합성(synthetic) 데이터만 사용한다 - 실제 Secret/운영 데이터는 전혀 관여하지 않는다.
- 채널 호출은 스텁으로 대체한다 - 실제 네이버/쿠팡 API를 호출하지 않는다.
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
RUNNER_SCRIPT_REL = "tests/integration/_product_sync_concurrency_runner.py"
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
    network = f"pgconc_test_net_{suffix}"
    volume = f"pgconc_test_vol_{suffix}"
    container = f"pgconc_test_db_{suffix}"
    db_user = "pgconc_test_user"
    db_password = f"pgconc-{suffix}-pw"
    db_name = "pgconc_test_db"

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
            f"PGCONC_DB_HOST={sandbox['container']}",
            "-e",
            f"PGCONC_DB_USER={sandbox['db_user']}",
            "-e",
            f"PGCONC_DB_PASSWORD={sandbox['db_password']}",
            "-e",
            f"PGCONC_DB_NAME={sandbox['db_name']}",
            "-e",
            f"PGCONC_SCENARIO={scenario}",
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


class TestSiblingMappingInventorySerialization:
    def test_sibling_mappings_never_call_channel_concurrently(self, pg_sandbox):
        payload = _run_scenario(pg_sandbox, "sibling_inventory")

        assert payload["worker_a_started"] is True, payload
        # worker-B(형제 매핑, 같은 외부 대상)는 worker-A가 아직 채널 호출 도중(release
        # 전)이면 advisory lock에서 대기해야 한다 - 미리 끝나 있으면 직렬화 실패.
        assert payload["b_finished_before_a_released"] is False
        assert payload["max_active"] == 1, f"동시 활성 채널 호출이 1을 넘었습니다: {payload['call_log']}"
        assert payload["a"]["status"] == "SUCCESS"
        assert payload["b"]["status"] == "SUCCESS"
        # B의 채널 호출은 A의 채널 호출이 끝난 뒤에만 시작되어야 한다.
        log_a = next(c for c in payload["call_log"] if c["thread"] == "worker-A")
        log_b = next(c for c in payload["call_log"] if c["thread"] == "worker-B")
        assert log_b["start"] >= log_a["end"], "형제 매핑의 두 채널 호출이 겹쳤습니다."


class TestCrossTypeSameTargetSerialization:
    def test_inventory_and_sale_status_never_run_concurrently_and_neither_is_cancelled(self, pg_sandbox):
        payload = _run_scenario(pg_sandbox, "cross_type_same_target")

        assert payload["worker_inv_started"] is True
        assert payload["status_finished_before_inv_released"] is False
        assert payload["max_active"] == 1, f"동시 활성 채널 호출이 1을 넘었습니다: {payload['call_log']}"
        # 재고 명령과 판매상태 명령은 서로 다른 의도이므로 상대방이 실행 중이라고
        # 해서 CANCELLED로 잘못 처리되면 안 되고, 둘 다 결국 성공해야 한다.
        assert payload["inv"]["status"] == "SUCCESS"
        assert payload["status"]["status"] == "SUCCESS"
        log_inv = next(c for c in payload["call_log"] if c["thread"] == "worker-inv")
        log_status = next(c for c in payload["call_log"] if c["thread"] == "worker-status")
        assert log_status["start"] >= log_inv["end"], "재고/판매상태 채널 호출이 겹쳤습니다."


class TestUnrelatedTargetsAreNotGloballySerialized:
    def test_unrelated_target_does_not_wait_for_unrelated_lock(self, pg_sandbox):
        payload = _run_scenario(pg_sandbox, "unrelated_targets_not_serialized")

        assert payload["worker_x_started"] is True
        # 무관한 대상 Y는 X가 채널 호출 도중(release 전)이어도 기다리지 않고 끝나야 한다.
        assert payload["y_finished_without_waiting_for_x"] is True
        assert payload["x"]["status"] == "SUCCESS"
        assert payload["y"]["status"] == "SUCCESS"


class TestUnknownPredecessorBlocksSuccessor:
    def test_successor_never_calls_channel_while_predecessor_is_unknown(self, pg_sandbox):
        payload = _run_scenario(pg_sandbox, "predecessor_unknown_blocks_successor")

        assert payload["first"]["status"] == "UNKNOWN"
        # 두 번째(더 최신) 명령은 채널을 호출하지 않고 PENDING을 유지해야 한다 -
        # connector 호출 횟수가 늘지 않아야 한다(첫 명령의 시도 1회만 남아야 함).
        assert payload["second"]["status"] == "PENDING"
        assert payload["connector_calls_after_second_attempt"] == payload["connector_calls_before_second_attempt"]
