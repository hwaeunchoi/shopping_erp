"""CI workflow(.github/workflows/ci.yml) 계약 테스트.

backend `quality` job이 한 번도 Ruff/Black/MyPy/Pytest에 도달하지 못한 채 의존성 설치에서
계속 실패했던 문제(clean Python 3.14 환경에서 psycopg-binary==3.2.1 설치 실패가 재현됨)가 되풀이되지 않도록,
CI가 사용하는 Python 버전과 설치 명령이 저장소의 실제 계약(Dockerfile, requirements.txt,
pyproject.toml)과 일치하는지, 그리고 실패를 숨기거나 배포하는 단계가 없는지 고정한다.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


class _UniqueKeyLoader(yaml.SafeLoader):
    """같은 mapping 안에서 key가 중복되면 예외를 낸다(PyYAML 기본값은 조용히 덮어쓴다)."""


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise AssertionError(f"ci.yml에 중복 key가 있습니다: {key!r} (line {key_node.start_mark.line + 1})")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


@pytest.fixture(scope="module")
def workflow() -> dict[Any, Any]:
    loaded = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    assert isinstance(loaded, dict)
    return loaded


def _jobs(workflow: dict[Any, Any]) -> dict[str, Any]:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job["steps"]
    assert isinstance(steps, list)
    return steps


def _run_of(step: dict[str, Any]) -> str:
    return str(step.get("run", ""))


def _major_minor(text: str) -> tuple[int, int]:
    m = re.match(r"(\d+)\.(\d+)", text)
    assert m, f"버전 형식을 읽을 수 없습니다: {text!r}"
    return int(m.group(1)), int(m.group(2))


def test_workflow_parses_without_duplicate_keys(workflow: dict[Any, Any]) -> None:
    # fixture가 중복 key를 AssertionError로 바꿔 준다. 여기서는 최상위 구조만 확인한다.
    assert {"name", "jobs"} <= set(workflow) and ("on" in workflow or True in workflow)


def test_runs_on_push_and_pull_request(workflow: dict[Any, Any]) -> None:
    triggers = workflow.get("on", workflow.get(True))  # YAML 1.1에서 `on`이 True로 읽히는 경우 대비
    assert isinstance(triggers, dict)
    assert "push" in triggers and "pull_request" in triggers


def test_backend_and_frontend_jobs_exist(workflow: dict[Any, Any]) -> None:
    assert {"quality", "frontend-quality"} <= set(_jobs(workflow))


def test_backend_python_matches_runtime_contract(workflow: dict[Any, Any]) -> None:
    """CI Python == 운영 Dockerfile Python, 그리고 requires-python 하한 이상이어야 한다."""
    steps = _steps(_jobs(workflow)["quality"])
    setups = [s for s in steps if str(s.get("uses", "")).startswith("actions/setup-python")]
    assert len(setups) == 1, "backend job에는 setup-python 단계가 정확히 하나여야 합니다"
    ci_version = str(setups[0]["with"]["python-version"])

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    m = re.search(r"^FROM\s+python:(\d+\.\d+)", dockerfile, re.MULTILINE)
    assert m, "Dockerfile에서 python 기본 이미지를 찾지 못했습니다"
    assert _major_minor(ci_version) == _major_minor(m.group(1)), (
        f"CI Python({ci_version})이 운영 Dockerfile(python:{m.group(1)})과 다릅니다. "
        "다른 버전을 지원하려면 별도 호환성 job으로 추가하세요."
    )

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    req = re.search(r'^requires-python\s*=\s*">=\s*(\d+\.\d+)', pyproject, re.MULTILINE)
    assert req, "pyproject.toml에서 requires-python 하한을 찾지 못했습니다"
    assert _major_minor(ci_version) >= _major_minor(req.group(1))


def test_backend_install_command_matches_requirements_layout(workflow: dict[Any, Any]) -> None:
    steps = _steps(_jobs(workflow)["quality"])
    installs = [s for s in steps if s.get("name") == "Install dependencies"]
    assert len(installs) == 1
    assert _run_of(installs[0]).strip() == "pip install -r requirements.txt"
    assert (ROOT / "requirements.txt").is_file()
    # 설치 단계가 의존하는 구조가 바뀌면(lock/constraints 도입 등) 이 테스트도 함께 고치도록 알린다.
    assert not (ROOT / "constraints.txt").exists()


def test_backend_quality_steps_are_present_in_order_and_unconditional(workflow: dict[Any, Any]) -> None:
    steps = _steps(_jobs(workflow)["quality"])
    runs = [_run_of(s) for s in steps]
    prefixes = ["pip install -r requirements.txt", "ruff check .", "black --check .", "mypy ", "pytest -q"]
    positions: list[int] = []
    for prefix in prefixes:
        found = [i for i, r in enumerate(runs) if r.startswith(prefix)]
        assert len(found) == 1, f"backend job에 '{prefix}' 단계가 정확히 하나여야 합니다(현재 {len(found)}개)"
        positions.append(found[0])
    assert positions == sorted(positions), "설치 → Ruff → Black → MyPy → Pytest 순서여야 합니다"
    mypy_run = runs[positions[3]]
    for target in (
        "models",
        "config",
        "core",
        "repositories",
        "services",
        "api",
        "integrations",
        "scheduler",
        "scripts",
        "tests",
    ):
        assert re.search(rf"\b{target}\b", mypy_run), f"MyPy 대상에 {target}가 없습니다"
    pytest_run = runs[positions[4]]
    for skipper in ("--deselect", "--ignore", " -k ", " -m ", "--lf", "--co"):
        assert skipper not in f" {pytest_run} ", f"pytest 단계가 테스트를 제외합니다: {skipper.strip()}"


def test_no_step_can_hide_failures_or_be_skipped(workflow: dict[Any, Any]) -> None:
    for name, job in _jobs(workflow).items():
        assert "continue-on-error" not in job, f"{name}: job에 continue-on-error 금지"
        assert "if" not in job, f"{name}: job에 조건부 실행 금지"
        for step in _steps(job):
            label = f"{name}/{step.get('name', step.get('uses'))}"
            assert "continue-on-error" not in step, f"{label}: continue-on-error 금지"
            assert "if" not in step, f"{label}: 조건부 step 금지"
            run = _run_of(step)
            assert "|| true" not in run and "|| :" not in run, f"{label}: 실패 무시 금지"
            assert "set +e" not in run, f"{label}: 실패 무시 금지"


def test_no_deploy_ssh_push_or_production_migration(workflow: dict[Any, Any]) -> None:
    forbidden = (
        "ssh",
        "scp",
        "rsync",
        "docker push",
        "docker login",
        "docker build",
        "docker compose",
        "alembic upgrade",
        "deploy",
    )
    for name, job in _jobs(workflow).items():
        for step in _steps(job):
            text = f"{step.get('name', '')} {step.get('uses', '')} {_run_of(step)}".lower()
            for word in forbidden:
                assert word not in text, f"{name}: 금지된 단계 '{word}' 발견: {text.strip()[:80]}"
            assert "secrets." not in str(step).lower(), f"{name}: CI에서 secret 사용 금지"


def test_frontend_quality_job_is_kept(workflow: dict[Any, Any]) -> None:
    job = _jobs(workflow)["frontend-quality"]
    runs = [_run_of(s) for s in _steps(job)]
    for needle in ("npm ci", "npm run lint", "npx tsc -b", "npm test", "npm run build"):
        assert needle in runs, f"frontend-quality에 '{needle}' 단계가 없습니다"
    versions = [
        str(s["with"]["node-version"]) for s in _steps(job) if str(s.get("uses", "")).startswith("actions/setup-node")
    ]
    assert versions == ["22.x", "20.x"], "Node 22 테스트와 Node 20 production build를 모두 수행해야 합니다"


# --- Python 지원 계약 문구와 직접 의존성 선언 -------------------------------------------------

_BAD_WORDS = ("범위 밖", "미지원", "지원 대상이 아니", "지원하지 않", "not supported", "unsupported")


def test_requires_python_has_no_upper_bound() -> None:
    """requires-python은 하한만 있는 계약이다. 상한이 생기면 문서·CI 설명도 함께 고쳐야 한다."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(
        r'^requires-python\s*=\s*">=\s*\d+(\.\d+)*"\s*$', pyproject, re.MULTILINE
    ), "requires-python이 '>=X.Y' 형태가 아닙니다. 상한을 추가했다면 README와 ci.yml의 Python 설명을 갱신하세요."


@pytest.mark.parametrize("rel", [".github/workflows/ci.yml", "README.md"])
def test_docs_do_not_misstate_python_314_support(rel: str) -> None:
    """3.14가 '범위 밖'이거나 '미지원'이라고 단정하는 문구가 없어야 한다(검증되지 않았을 뿐이다)."""
    for lineno, line in enumerate((ROOT / rel).read_text(encoding="utf-8").splitlines(), start=1):
        if "3.14" in line:
            lowered = line.lower()
            for word in _BAD_WORDS:
                assert word not in lowered, f"{rel}:{lineno}: 3.14 지원 여부를 단정하는 문구 '{word}'"


def test_workflow_comment_states_python_314_is_unverified() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "3.14 호환성은 검증되지 않았다" in text, "ci.yml 주석에 3.14가 검증되지 않았다는 사실을 남겨야 합니다"


def test_directly_imported_yaml_is_declared_in_requirements() -> None:
    """tests가 yaml을 직접 import하면 PyYAML을 transitive(pre-commit) 우연에 맡기지 않고 직접 고정한다."""
    importers: list[Path] = []
    for top in (
        "tests",
        "api",
        "core",
        "services",
        "repositories",
        "integrations",
        "scheduler",
        "scripts",
        "models",
        "config",
    ):
        for path in (ROOT / top).rglob("*.py"):
            if re.search(r"^\s*(import yaml\b|from yaml\b)", path.read_text(encoding="utf-8"), re.MULTILINE):
                importers.append(path)
    assert importers, "이 테스트 파일 자신이 yaml을 import하므로 최소 1개는 있어야 합니다"
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(
        r"^PyYAML==\d+(\.\d+)+(\s|$)", requirements, re.MULTILINE | re.IGNORECASE
    ), "yaml을 직접 import하는 파일이 있지만 requirements.txt에 PyYAML==X.Y.Z 고정이 없습니다: " + ", ".join(
        sorted(p.relative_to(ROOT).as_posix() for p in importers)
    )
