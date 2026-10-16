"""PostgreSQL 통합 테스트가 clean 환경(예: GitHub runner)에서 성립하기 위한 Docker 전제 조건 계약.

CI에서 실패했던 두 가지가 다시 숨어들지 않도록 고정한다.
1. RUNNER_IMAGE(shopping_erp-api:latest)를 가정하는 테스트가, 이미지가 없을 때 Docker Hub pull 오류가 아니라
   원인과 빌드 명령을 알려 주는 실패를 낸다.
2. 저장소를 읽기 전용(/app:ro)으로 마운트한 러너가 쓰기 경로(/app/logs, /app/reports/generated)를 필요로 할 때
   /app 전체를 쓰기 가능으로 바꾸지 않고 그 경로만 tmpfs로 덮는다.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
INTEGRATION = ROOT / "tests" / "integration"


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_runner_image_under_test", INTEGRATION / "_runner_image.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = _load_helper()


class _Recorder:
    """subprocess.run 대역: 호출한 명령을 기록하고 미리 정한 returncode를 돌려준다."""

    def __init__(self, info_rc: int = 0, inspect_rc: int = 0, raises: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.info_rc = info_rc
        self.inspect_rc = inspect_rc
        self.raises = raises

    def __call__(self, cmd: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(cmd)
        if self.raises:
            raise OSError("docker unavailable")
        rc = self.info_rc if cmd[1] == "info" else self.inspect_rc
        return subprocess.CompletedProcess(cmd, rc, b"", b"")


def _docker_present(_: str) -> str:
    return "/usr/bin/docker"


def test_missing_runner_image_is_reported_with_the_build_command() -> None:
    rec = _Recorder(inspect_rc=1)
    message = helper.runner_image_problem("shopping_erp-api:latest", which=_docker_present, run=rec)
    assert message is not None
    assert "shopping_erp-api:latest" in message
    assert "docker build -t shopping_erp-api:latest ." in message
    assert "pull access denied" in message


def test_existing_runner_image_has_no_problem() -> None:
    assert helper.runner_image_problem("shopping_erp-api:latest", which=_docker_present, run=_Recorder()) is None


def test_the_check_only_inspects_locally_and_never_pulls_or_runs() -> None:
    rec = _Recorder(inspect_rc=1)
    helper.runner_image_problem("shopping_erp-api:latest", which=_docker_present, run=rec)
    assert rec.calls == [["docker", "info"], ["docker", "image", "inspect", "shopping_erp-api:latest"]]


@pytest.mark.parametrize(
    "which, run",
    [
        (lambda _: None, _Recorder()),  # Docker CLI 없음 - 각 테스트 파일이 스스로 skip한다
        (_docker_present, _Recorder(info_rc=1)),  # 데몬에 연결할 수 없음
        (_docker_present, _Recorder(raises=True)),  # 실행 자체가 실패
    ],
)
def test_unreachable_docker_is_left_to_the_tests_own_skip(which: Any, run: Any) -> None:
    assert helper.runner_image_problem("shopping_erp-api:latest", which=which, run=run) is None


def test_conftest_fails_instead_of_skipping_when_the_image_is_missing() -> None:
    text = (INTEGRATION / "conftest.py").read_text(encoding="utf-8")
    assert re.search(r"@pytest\.fixture\(autouse=True\)\s+def _runner_image_prerequisite", text)
    assert "runner_image_problem(" in text
    assert "pytest.fail(problem" in text  # 이미지 없이 통과한 것처럼 보이면 안 되므로 skip이 아니라 실패다


def test_repository_is_always_mounted_read_only_in_runner_containers() -> None:
    checked = 0
    for path in sorted(INTEGRATION.glob("test_*_pg.py")):
        for mount in re.findall(r'\{REPO_ROOT\.as_posix\(\)\}:/app[^"]*', path.read_text(encoding="utf-8")):
            checked += 1
            assert mount.endswith(":/app:ro"), f"{path.name}: 저장소를 읽기 전용이 아니게 마운트합니다: {mount}"
    assert checked >= 10, "러너의 저장소 마운트를 충분히 찾지 못했습니다(검사 대상 확인 필요)"


@pytest.mark.parametrize(
    "name",
    [
        "test_postgres_backup_catchup_pg.py",
        "test_postgres_backup_restore_pg.py",
        "test_alembic_percent_encoded_url_pg.py",
    ],
)
def test_runners_that_execute_init_db_overlay_only_the_write_paths(name: str) -> None:
    text = (INTEGRATION / name).read_text(encoding="utf-8")
    tmpfs = re.findall(r'"--tmpfs",\s*"([^"]+)"', text)
    assert sorted(tmpfs) == ["/app/logs", "/app/reports/generated"], tmpfs  # /app 전체를 덮거나 늘리지 않는다
    assert ":/app:ro" in text


def test_generated_reports_mountpoint_exists_in_a_clean_checkout() -> None:
    """읽기 전용 bind mount 안에는 runc가 마운트 지점을 만들 수 없으므로 디렉터리가 저장소에 있어야 한다."""
    assert (ROOT / "reports" / "generated" / ".gitkeep").is_file()
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "reports/generated/*" in gitignore  # 산출물 내용은 계속 무시
    assert gitignore.index("!reports/generated/.gitkeep") > gitignore.index("reports/generated/*")
