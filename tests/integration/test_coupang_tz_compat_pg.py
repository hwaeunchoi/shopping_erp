"""
tests/integration/test_coupang_tz_compat_pg.py
----------------------------------------------------
fix/coupang-call-center-inquiry-persistence 병합 전 호환성 감사 - 운영과
동일한 PostgreSQL 16 + SQLAlchemy + psycopg3 조합으로, 수정 전 parser(KST-aware
반환)와 수정 후 parser(naive UTC 반환)가 timezone 없는 `DateTime` 컬럼에
실제로 같은 값을 저장하는지 실증한다(SQLite는 이 질문에 쓸 수 없다 - 격리
원칙은 tests/integration/test_cs_case_concurrency_pg.py와 동일).
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
RUNNER_SCRIPT_REL = "tests/integration/_coupang_tz_compat_runner.py"
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
    network = f"tzcompat_test_net_{suffix}"
    volume = f"tzcompat_test_vol_{suffix}"
    container = f"tzcompat_test_db_{suffix}"
    db_user = "tzcompat_test_user"
    db_password = f"tzcompat-{suffix}-pw"
    db_name = "tzcompat_test_db"

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


def _run(sandbox: dict, timeout: int = 90) -> dict[str, Any]:
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
            f"TZCOMPAT_DB_HOST={sandbox['container']}",
            "-e",
            f"TZCOMPAT_DB_USER={sandbox['db_user']}",
            "-e",
            f"TZCOMPAT_DB_PASSWORD={sandbox['db_password']}",
            "-e",
            f"TZCOMPAT_DB_NAME={sandbox['db_name']}",
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
    line = next(ln for ln in result.stdout.strip().splitlines() if ln.startswith("TZCOMPAT_RESULT_JSON:"))
    return json.loads(line[len("TZCOMPAT_RESULT_JSON:") :])


class TestLegacyVsNewWriteSemantics:
    """핵심 호환성 질문 - 수정 전 parser(tz-aware KST)와 수정 후 parser(naive
    UTC)가 같은 실제 순간을 저장할 때 DB에 쓰이는 실제 값이 동일한지."""

    def test_legacy_kst_aware_and_new_utc_naive_write_identical_bytes(self, pg_sandbox):
        payload = _run(pg_sandbox)
        assert payload["legacy_and_new_writes_identical"] is True, payload


class TestBoundaryComparisons:
    """8시간59분/9시간/9시간1분 경계 - naive-naive 비교라 TypeError 없이 전부
    비교 가능해야 한다."""

    @pytest.mark.parametrize("label", ["8h59m", "9h00m", "9h01m"])
    def test_boundary_is_comparable_without_typeerror(self, pg_sandbox, label):
        payload = _run(pg_sandbox)
        assert payload[f"boundary_{label}_comparable"] is True, payload
        assert payload[f"boundary_{label}_is_later"] is True, payload


class TestSameSessionFlushThenReread:
    """_upsert_one()이 실제로 타는 경로(같은 세션에서 flush 후 재조회) -
    수정 후에는 TypeError 없이 비교 가능해야 한다(원래 운영 버그의 핵심
    재현 조건)."""

    def test_reread_after_flush_is_comparable(self, pg_sandbox):
        payload = _run(pg_sandbox)
        assert payload["same_session_reread_compare_ok"] is True, payload


class TestOffsetIncludedVsExcluded:
    """응답에 오프셋이 포함된 경우와 없는 경우(둘 다 같은 실제 순간을 가리킴)
    모두 동일한 파싱 결과를 내야 한다 - Asia/Seoul은 DST가 없어 오프셋이
    상시 +09:00으로 고정이라는 전제가 깨지지 않는지 확인한다."""

    def test_offset_included_and_excluded_parse_identically(self, pg_sandbox):
        payload = _run(pg_sandbox)
        assert payload["offset_included_vs_excluded_identical"] is True, payload
