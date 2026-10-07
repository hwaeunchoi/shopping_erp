"""
tests/integration/test_cs_sync_lock_pg.py
--------------------------------------------------
CS 문의 동기화 (platform, source) advisory lock과 이를 쓰는 catch-up 서비스·stale 작업 정리·수동
sync 경로를 실제 PostgreSQL 두 커넥션 경합으로 검증한다 - 운영 erp-postgres가 아니라 이 테스트
전용 1회성 컨테이너를 쓴다.

격리 원칙(tests/integration/test_cs_case_concurrency_pg.py와 동일): 고유 임시 docker network
(--internal)/컨테이너/볼륨(uuid 접미사)만 사용하고, 같은 network의 "러너" 컨테이너(shopping_erp-api
이미지, 레포 읽기전용 마운트)에서 시나리오를 실행해 JSON 한 줄만 host에서 파싱한다. 합성 데이터만
사용하고, 종료 후(성공/실패 무관) 전부 제거한다. Docker CLI가 없으면 skip한다.
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
RUNNER_SCRIPT_REL = "tests/integration/_cs_sync_lock_runner.py"
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
    network = f"cslock_test_net_{suffix}"
    volume = f"cslock_test_vol_{suffix}"
    container = f"cslock_test_db_{suffix}"
    db_user = "cslock_test_user"
    db_password = f"cslock-{suffix}-pw"
    db_name = "cslock_test_db"

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


def _run_scenario(sandbox: dict, scenario: str, timeout: int = 120) -> dict[str, Any]:
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
            f"CSLOCK_DB_HOST={sandbox['container']}",
            "-e",
            f"CSLOCK_DB_USER={sandbox['db_user']}",
            "-e",
            f"CSLOCK_DB_PASSWORD={sandbox['db_password']}",
            "-e",
            f"CSLOCK_DB_NAME={sandbox['db_name']}",
            "-e",
            f"CSLOCK_SCENARIO={scenario}",
            "-e",
            f"JWT_SECRET_KEY=cslock-test-jwt-{uuid.uuid4().hex}{uuid.uuid4().hex}",
            "-e",
            f"CREDENTIAL_ENCRYPTION_KEY=cslock-test-cred-{uuid.uuid4().hex}{uuid.uuid4().hex}",
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
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestAdvisoryLockContention:
    def test_same_platform_and_source_is_exclusive_others_are_independent(self, pg_sandbox):
        p = _run_scenario(pg_sandbox, "lock_contention")

        assert p["first_acquired"] is True, p
        assert p["same_source_second_acquired"] is False, p  # 같은 (platform, source)는 하나만
        assert p["other_source_acquired"] is True, p  # 다른 source는 막지 않는다
        assert p["other_platform_acquired"] is True, p  # 다른 platform도 막지 않는다
        assert p["reacquired_after_release"] is True, p  # 정상 해제 후 다시 잡힌다

    def test_lock_is_freed_when_the_holder_connection_dies(self, pg_sandbox):
        p = _run_scenario(pg_sandbox, "lock_contention")

        assert p["raw_held"] is True, p
        assert p["acquired_while_raw_connection_holds_it"] is False, p
        assert p["acquired_after_holder_connection_closed"] is True, p  # 크래시 후 자동 해제


class TestConcurrentCatchupRuns:
    def test_two_simultaneous_runs_execute_each_source_exactly_once(self, pg_sandbox):
        p = _run_scenario(pg_sandbox, "concurrent_sync_platform")

        assert p["a"]["exception"] is None, p
        assert p["b"]["exception"] is None, p
        assert p["attempts"] == 4, p  # 두 worker x 두 source의 잠금 시도가 모두 fetch 전에 끝났다(결정적 겹침)
        for source in ("COUPANG_CALL_CENTER", "COUPANG_PRODUCT_INQUIRY"):
            statuses = sorted([p["a"]["by_source"][source], p["b"]["by_source"][source]])
            assert statuses == ["ALREADY_RUNNING", "SUCCESS"], (source, p)
            fetches = p["a"]["calls"].count(source) + p["b"]["calls"].count(source)
            assert fetches == 1, (source, p)  # 외부 호출은 승자 한 쪽만
        # DB: source마다 case 정확히 1건, checkpoint도 source마다 1행(진행 위치 있음).
        assert p["case_counts"] == {"COUPANG_CALL_CENTER": 1, "COUPANG_PRODUCT_INQUIRY": 1}, p
        assert len(p["checkpoint_codes"]) == 2, p
        assert p["checkpoints_all_have_progress"] is True, p


class TestStaleRecoveryRespectsRealLocks:
    def test_running_row_is_kept_while_lock_held_and_recovered_after(self, pg_sandbox):
        p = _run_scenario(pg_sandbox, "stale_probe_sees_real_lock")

        assert p["lock_acquired"] is True, p
        assert p["during_lock"] == {"recovered": 0, "skipped_active": 1}, p
        assert p["status_during_lock"] == "RUNNING", p
        assert p["after_release"] == {"recovered": 1, "skipped_active": 0}, p
        assert p["final_status"] == "FAILED", p
        assert p["final_error_message"] == "PROCESS_INTERRUPTED", p


class TestManualSyncVersusAutomaticRun:
    def test_manual_style_all_source_lock_is_refused_while_automatic_run_is_in_progress(self, pg_sandbox):
        p = _run_scenario(pg_sandbox, "manual_vs_running_sync")

        assert p["auto"] == "SUCCESS", p
        assert p["manual_all_acquired"] is False, p
        assert p["manual_acquired_per_source"] == [False, False], p
        assert p["manual_all_acquired_after_auto_finished"] is True, p


class TestSegmentCommitAtomicityOnPostgres:
    def test_commit_failure_leaves_no_partial_state_and_rerun_converges(self, pg_sandbox):
        p = _run_scenario(pg_sandbox, "atomic_segment_commit_failure")

        # 첫 구간(콜센터)의 commit이 실패 -> 그 source의 case/history/checkpoint는 하나도 남지 않는다.
        assert p["first_by_source"]["COUPANG_CALL_CENTER"] == "FAILED", p
        assert p["first_reason_codes"]["COUPANG_CALL_CENTER"] == "COMMIT_FAILED:RuntimeError", p
        # 다른 source(상품별)는 영향 없이 정상 commit - case 1, history 1, checkpoint 1.
        assert p["first_by_source"]["COUPANG_PRODUCT_INQUIRY"] == "SUCCESS", p
        assert p["after_first"] == {"cases": 1, "history": 1, "success_checkpoints": 1}, p
        # 재실행하면 실패했던 source도 따라와 source마다 정확히 1건(중복 없음)으로 수렴한다.
        assert p["second_status"] == "SUCCESS", p
        assert p["after_second"]["cases"] == 2, p
        assert p["after_second"]["success_checkpoints"] == 2, p
