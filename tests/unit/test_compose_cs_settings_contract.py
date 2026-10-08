"""
tests/unit/test_compose_cs_settings_contract.py
---------------------------------------------------
CS 문의 자동수집 설정이 docker-compose.yml을 거쳐 api/scheduler 컨테이너 환경으로 전달되는 계약.

배경: .env는 이미지에 들어가지 않으므로(.dockerignore) compose가 `environment:`로 전달하지 않은 값은 컨테이너 안에서
적용되지 않는다. CS_INQUIRY_SYNC_ENABLED가 전달되지 않아 .env를 true로 바꿔도 플래그가 켜지지 않던 사고의 재발 방지.

격리: 운영 .env를 읽지 않는다. 항상 임시 디렉터리에 compose 파일 사본과 (함정용) 가짜 .env를 두고, 명시한 --env-file과
정리된 자식 프로세스 환경으로 `docker compose config`를 실행한다. 합성 secret만 쓰고 실패 메시지에 compose 전체
출력을 싣지 않는다.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from config.settings import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"

# 환경변수 이름 -> Settings 필드. 이름 매핑을 추측하지 않고 필드에서 파생한다(필드명.upper()).
CS_FIELDS = [
    "cs_inquiry_sync_enabled",
    "cs_inquiry_sync_window_days",
    "cs_inquiry_sync_max_pages_per_query",
    "cs_inquiry_sync_max_retries_per_page",
    "cs_inquiry_sync_max_requests_per_run",
    "cs_inquiry_sync_interval_minutes",
    "task_stale_running_threshold_minutes",
]
ENV_NAMES = [f.upper() for f in CS_FIELDS]
PASSED_TO = ("api", "scheduler")
NOT_PASSED_TO = ("web", "db", "redis")

_BASE_ENV_FILE = (
    "POSTGRES_PASSWORD=synthetic-pw-000000\n"
    "DATABASE_URL=postgresql+psycopg://u:synthetic-pw-000000@db:5432/d\n"
    "JWT_SECRET_KEY=synthetic-jwt-0000000000000000000000000000\n"
    "CREDENTIAL_ENCRYPTION_KEY=synthetic-cred-00000000000000000000000\n"
)

needs_compose = pytest.mark.skipif(
    shutil.which("docker") is None
    or subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0,
    reason="docker compose CLI가 없는 환경 - 렌더링 테스트 skip(정적 테스트는 항상 실행)",
)


class _UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(None, None, f"duplicate key: {key!r}", key_node.start_mark)
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _load_compose_strict() -> dict:
    return yaml.load(
        COMPOSE_PATH.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader
    )  # noqa: S506 - SafeLoader 하위 클래스


def _clean_child_env() -> dict[str, str]:
    keep = (
        "PATH",
        "SYSTEMROOT",
        "SystemDrive",
        "USERPROFILE",
        "HOME",
        "APPDATA",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
        "COMSPEC",
    )
    return {k: v for k, v in os.environ.items() if k in keep}


def _render(tmp_path: Path, env_lines: str = "", decoy_dotenv: str | None = None) -> dict:
    """tmp_path에 compose 사본(+선택적 가짜 .env)을 두고 명시한 env-file로 렌더링한 결과(JSON)를 돌려준다."""
    work = tmp_path / "proj"
    work.mkdir()
    shutil.copyfile(COMPOSE_PATH, work / "docker-compose.yml")
    if decoy_dotenv is not None:
        (work / ".env").write_text(decoy_dotenv, encoding="utf-8")
    env_file = tmp_path / "explicit.env"
    env_file.write_text(_BASE_ENV_FILE + env_lines, encoding="utf-8")
    result = subprocess.run(
        ["docker", "compose", "--env-file", str(env_file), "-f", "docker-compose.yml", "config", "--format", "json"],
        cwd=work,
        env=_clean_child_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    # 실패해도 stdout/stderr(합성 secret 포함 가능)를 메시지에 싣지 않는다.
    assert result.returncode == 0, f"docker compose config 실패(exit={result.returncode})"
    return json.loads(result.stdout)


def _cs_env(rendered: dict, service: str) -> dict[str, str]:
    env = rendered["services"][service].get("environment", {}) or {}
    return {k: str(v) for k, v in env.items() if k in ENV_NAMES}


def _default_of(expr: str) -> str:
    m = re.fullmatch(r"\$\{([A-Z_]+):-([^}]*)\}", expr)
    assert m, f"예상한 ${{VAR:-default}} 형태가 아닙니다: {expr!r}"
    return m.group(2)


class TestStaticComposeContract:
    def test_yaml_has_no_duplicate_keys(self):
        _load_compose_strict()

    def test_every_variable_maps_to_an_existing_settings_field(self):
        for field in CS_FIELDS:
            assert field in Settings.model_fields, field

    def test_api_and_scheduler_pass_every_cs_setting_with_identical_expressions(self):
        services = _load_compose_strict()["services"]
        for name in ENV_NAMES:
            api_expr = services["api"]["environment"].get(name)
            scheduler_expr = services["scheduler"]["environment"].get(name)
            assert api_expr is not None, f"api에 {name} 전달이 없습니다."
            assert scheduler_expr is not None, f"scheduler에 {name} 전달이 없습니다."
            assert api_expr == scheduler_expr, f"{name} 표현식이 api/scheduler에서 다릅니다."

    def test_compose_fallbacks_equal_the_code_defaults(self):
        """compose의 `:-default`는 Settings 코드 기본값과 정확히 같아야 한다(어긋나면 .env 미설정 시 컨테이너 동작이 코드와 달라진다)."""
        services = _load_compose_strict()["services"]
        for field, name in zip(CS_FIELDS, ENV_NAMES, strict=True):
            fallback = _default_of(services["scheduler"]["environment"][name])
            code_default = Settings.model_fields[field].default
            if isinstance(code_default, bool):
                assert fallback == ("true" if code_default else "false"), name
            else:
                assert fallback == str(code_default), name

    def test_activation_flag_fallback_is_false(self):
        for service in PASSED_TO:
            expr = _load_compose_strict()["services"][service]["environment"]["CS_INQUIRY_SYNC_ENABLED"]
            assert expr == "${CS_INQUIRY_SYNC_ENABLED:-false}"

    def test_services_that_do_not_need_cs_settings_get_none(self):
        services = _load_compose_strict()["services"]
        for service in NOT_PASSED_TO:
            env = services[service].get("environment") or {}
            assert not (set(env) & set(ENV_NAMES)), service

    def test_no_secret_like_variable_is_added_for_cs_settings(self):
        for name in ENV_NAMES:
            assert not re.search(r"KEY|SECRET|PASSWORD|TOKEN|CREDENTIAL", name), name


@needs_compose
class TestRenderedEnvironment:
    def test_unset_environment_renders_safe_defaults_for_api_and_scheduler(self, tmp_path):
        rendered = _render(tmp_path)
        expected = {
            n: (
                "false" if isinstance(Settings.model_fields[f].default, bool) else str(Settings.model_fields[f].default)
            )
            for f, n in zip(CS_FIELDS, ENV_NAMES, strict=True)
        }
        for service in PASSED_TO:
            assert _cs_env(rendered, service) == expected, service
        assert expected["CS_INQUIRY_SYNC_ENABLED"] == "false"

    def test_true_flag_reaches_both_api_and_scheduler(self, tmp_path):
        rendered = _render(tmp_path, "CS_INQUIRY_SYNC_ENABLED=true\n")
        for service in PASSED_TO:
            assert _cs_env(rendered, service)["CS_INQUIRY_SYNC_ENABLED"] == "true", service

    def test_tuning_values_reach_exactly_api_and_scheduler(self, tmp_path):
        tuned = {
            "CS_INQUIRY_SYNC_WINDOW_DAYS": "2",
            "CS_INQUIRY_SYNC_MAX_PAGES_PER_QUERY": "5",
            "CS_INQUIRY_SYNC_MAX_RETRIES_PER_PAGE": "1",
            "CS_INQUIRY_SYNC_MAX_REQUESTS_PER_RUN": "30",
            "CS_INQUIRY_SYNC_INTERVAL_MINUTES": "10",
            "TASK_STALE_RUNNING_THRESHOLD_MINUTES": "120",
        }
        rendered = _render(tmp_path, "".join(f"{k}={v}\n" for k, v in tuned.items()))
        for service in PASSED_TO:
            env = _cs_env(rendered, service)
            for k, v in tuned.items():
                assert env[k] == v, (service, k)
            assert env["CS_INQUIRY_SYNC_ENABLED"] == "false"  # 튜닝값만 바꿔도 플래그는 그대로
        for service in NOT_PASSED_TO:
            assert _cs_env(rendered, service) == {}, service

    def test_empty_value_falls_back_to_the_default(self, tmp_path):
        rendered = _render(tmp_path, "CS_INQUIRY_SYNC_ENABLED=\nCS_INQUIRY_SYNC_MAX_REQUESTS_PER_RUN=\n")
        for service in PASSED_TO:
            env = _cs_env(rendered, service)
            assert env["CS_INQUIRY_SYNC_ENABLED"] == "false"
            assert env["CS_INQUIRY_SYNC_MAX_REQUESTS_PER_RUN"] == "45"

    def test_hostile_host_environment_cannot_change_the_explicit_env_file_result(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CS_INQUIRY_SYNC_ENABLED", "true")
        monkeypatch.setenv("CS_INQUIRY_SYNC_MAX_REQUESTS_PER_RUN", "99999")
        rendered = _render(tmp_path, "CS_INQUIRY_SYNC_ENABLED=false\nCS_INQUIRY_SYNC_MAX_REQUESTS_PER_RUN=45\n")
        for service in PASSED_TO:
            env = _cs_env(rendered, service)
            assert env["CS_INQUIRY_SYNC_ENABLED"] == "false"
            assert env["CS_INQUIRY_SYNC_MAX_REQUESTS_PER_RUN"] == "45"

    def test_a_dotenv_in_the_project_directory_is_not_auto_loaded_when_an_env_file_is_given(self, tmp_path):
        """운영 .env 자동 로딩에 의존하지 않는다는 증거: 프로젝트 디렉터리의 가짜 .env(true)는 무시된다."""
        rendered = _render(tmp_path, decoy_dotenv="CS_INQUIRY_SYNC_ENABLED=true\nCS_INQUIRY_SYNC_WINDOW_DAYS=30\n")
        for service in PASSED_TO:
            env = _cs_env(rendered, service)
            assert env["CS_INQUIRY_SYNC_ENABLED"] == "false"
            assert env["CS_INQUIRY_SYNC_WINDOW_DAYS"] == "1"

    def test_rendered_values_are_parsed_by_settings_into_the_intended_fields(self, tmp_path, monkeypatch):
        """렌더된 컨테이너 환경을 그대로 Settings에 넣으면 의도한 필드가 채워진다(이름 매핑 증명)."""
        tuned = "CS_INQUIRY_SYNC_ENABLED=true\nCS_INQUIRY_SYNC_WINDOW_DAYS=2\nCS_INQUIRY_SYNC_MAX_REQUESTS_PER_RUN=30\n"
        rendered = _render(tmp_path, tuned)
        for service in PASSED_TO:
            for name in ENV_NAMES:
                monkeypatch.delenv(name, raising=False)
            for name, value in _cs_env(rendered, service).items():
                monkeypatch.setenv(name, value)
            parsed = Settings(_env_file=None)  # type: ignore[call-arg]
            assert parsed.cs_inquiry_sync_enabled is True
            assert parsed.cs_inquiry_sync_window_days == 2
            assert parsed.cs_inquiry_sync_max_requests_per_run == 30
            assert parsed.cs_inquiry_sync_max_pages_per_query == 3
            assert parsed.cs_inquiry_sync_max_retries_per_page == 2
            assert parsed.cs_inquiry_sync_interval_minutes == 15
            assert parsed.task_stale_running_threshold_minutes == 360

    def test_compose_config_quiet_passes(self, tmp_path):
        work = tmp_path / "proj"
        work.mkdir()
        shutil.copyfile(COMPOSE_PATH, work / "docker-compose.yml")
        env_file = tmp_path / "explicit.env"
        env_file.write_text(_BASE_ENV_FILE, encoding="utf-8")
        result = subprocess.run(
            ["docker", "compose", "--env-file", str(env_file), "-f", "docker-compose.yml", "config", "--quiet"],
            cwd=work,
            env=_clean_child_env(),
            capture_output=True,
            timeout=60,
        )
        assert result.returncode == 0

    def test_rendered_output_secrets_are_not_part_of_any_cs_variable(self, tmp_path):
        rendered = _render(tmp_path, "CS_INQUIRY_SYNC_ENABLED=true\n")
        for service in PASSED_TO:
            for value in _cs_env(rendered, service).values():
                assert "synthetic" not in value
