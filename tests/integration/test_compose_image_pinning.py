"""
tests/integration/test_compose_image_pinning.py
------------------------------------------------------------
docker-compose.yml의 API_IMAGE/SCHEDULER_IMAGE/WEB_IMAGE 이미지 고정 계약을
실제 `docker compose config`로 검증한다(2026-09-11 API 크래시 루프 - api가
build: .의 기본 이미지 이름으로 재기동되어 migration이 이미 진행된 운영 DB와
버전이 어긋난 사고 - 재발 방지).

정적 YAML 검사는 tests/unit/test_docker_compose_contract.py가 담당한다 - 이
파일은 실제 `docker compose config`가 환경변수 유무에 따라 무엇을 "최종
선택"하는지, 그리고 scripts/verify_deploy_images.py가 실제 로컬 이미지에
대해 정상 동작하는지를 검증한다. 컨테이너는 하나도 띄우지 않는다(`config`는
순수 렌더링, `--no-deps`/이미지 조회 부분은 이미 로컬에 있는 이미지만 조회).

운영 컨테이너(erp-api/erp-scheduler/erp-web)·운영 DB·운영 .env에는 전혀
접근하지 않는다 - 이 저장소 워킹 디렉터리의 docker-compose.yml을 이
프로세스의 임시 환경변수로만 렌더링한다.
"""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI가 없는 환경 - 통합 테스트 skip")

# 실제 운영에 존재하는 것으로 이전 라운드에서 이미 확인된 candidate 이미지 -
# 이 테스트가 이 이미지를 새로 만들지 않는다(존재하면 실 이미지로 상세 검증,
# 없으면 해당 검증만 skip - 순수 config 렌더링 검증은 이미지 존재와 무관하게
# 항상 실행된다).
_KNOWN_MATCHING_PAIR = {
    "api": "shopping_erp_candidate/api:20260910_041113-b5d9485",
    "scheduler": "shopping_erp_candidate/scheduler:20260910_041113-b5d9485",
}
_KNOWN_BACKEND_REVISION = "b5d94851b6c44a9e52feea3f3dd95351c9573d93"
_KNOWN_MISMATCHED_SCHEDULER = "shopping_erp_candidate/scheduler:20260909_110251-f87295b"
# 프론트 변경이 없어 backend보다 이전 커밋 이미지를 그대로 쓰는 실제 시나리오.
_KNOWN_WEB_IMAGE = "shopping_erp_candidate/web:20260909_054605-434a3d0"
_KNOWN_WEB_REVISION = "434a3d040259a022504299440474b85a6fc8dd64"


def _docker_available() -> bool:
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=10, check=True)
        return True
    except Exception:
        return False


def _image_exists(ref: str) -> bool:
    try:
        r = subprocess.run(["docker", "image", "inspect", ref], capture_output=True, timeout=10, check=False)
        return r.returncode == 0
    except Exception:
        return False


def _run_compose(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    """저장소 .env를 읽지 않도록 빈 --env-file을 명시해 `docker compose`를 실행한다.

    compose는 기본적으로 프로젝트 디렉터리의 .env를 자동 로드하고 이 값이 프로세스
    환경변수로 지우지 못한 변수를 채운다 - 로컬 운영 .env에 이미지가 고정돼 있으면
    "변수 미설정" 전제 테스트가 실제 .env 내용에 따라 달라진다. 임시 빈 env 파일을
    --env-file로 넘기면 .env 자동 로드가 대체돼 테스트가 이 프로세스의 env dict만으로
    결정된다(임시 파일은 실행 직후 자동 삭제)."""
    with tempfile.TemporaryDirectory(prefix="compose_isolated_env_") as tmp:
        empty_env_file = Path(tmp) / "isolated.env"
        empty_env_file.write_text("", encoding="utf-8")
        return subprocess.run(
            ["docker", "compose", "--env-file", str(empty_env_file), "-f", str(COMPOSE_FILE), *args],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )


def _compose_config(env: dict[str, str]) -> dict:
    result = _run_compose(env, "config")
    assert result.returncode == 0, f"docker compose config 실패:\n{result.stderr}"
    return yaml.safe_load(result.stdout)


def _compose_images(env: dict[str, str]) -> dict[str, str]:
    cfg = _compose_config(env)
    return {name: svc["image"] for name, svc in cfg["services"].items()}


# 필수 Secret 계약(${VAR:?...})을 만족시키기 위한 최소 dummy 값 - 실제 값이
# 아니며 이 프로세스 환경변수로만 존재하고 어디에도 저장되지 않는다.
_BASE_ENV = {
    "DATABASE_URL": "sqlite:///./_compose_config_test_only.db",
    "JWT_SECRET_KEY": "compose-config-test-only-dummy-jwt-secret",
    "CREDENTIAL_ENCRYPTION_KEY": "compose-config-test-only-dummy-cred-secret",
    "POSTGRES_PASSWORD": "compose-config-test-only-dummy-pw",
}


def _compose_referenced_vars() -> set[str]:
    return set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", COMPOSE_FILE.read_text(encoding="utf-8")))


def _env(**overrides: str) -> dict[str, str]:
    env = dict(os.environ)
    # docker-compose.yml이 ${VAR}로 참조하는 모든 변수를 호스트 환경에서 제거한다 -
    # 호스트 셸에 남은 실제 값(이미지 고정·백업 경로·플래그 등)이 테스트 결과에
    # 섞이지 않게 하고, 매 테스트가 원하는 조합만 명시적으로 설정한다.
    for key in _compose_referenced_vars():
        env.pop(key, None)
    env.update(_BASE_ENV)
    env.update(overrides)
    return env


@pytest.fixture(scope="module")
def docker_ready():
    if not _docker_available():
        pytest.skip("Docker 데몬에 연결할 수 없어 통합 테스트를 skip합니다.")


class TestDefaultFallback:
    def test_no_image_vars_selects_existing_default_names(self, docker_ready):
        """이미지 변수 미설정 시 기존 개발용 기본 이미지 3개를 그대로 선택한다."""
        images = _compose_images(_env())
        assert images["api"] == "shopping_erp-api"
        assert images["scheduler"] == "shopping_erp-scheduler"
        assert images["web"] == "shopping_erp-web"
        # db/redis는 이번 변경과 무관하게 그대로여야 한다.
        assert images["db"] == "postgres:16-alpine"
        assert images["redis"] == "redis:7-alpine"

    def test_empty_string_image_var_falls_back_to_default(self, docker_ready):
        """빈 문자열도 미설정과 동일하게 기본값으로 폴백한다(`:-` 문법)."""
        images = _compose_images(_env(API_IMAGE=""))
        assert images["api"] == "shopping_erp-api"


class TestExplicitOverrideSelected:
    def test_all_three_vars_select_exact_candidate_tags(self, docker_ready):
        images = _compose_images(
            _env(
                API_IMAGE="shopping_erp_candidate/api:TESTONLY-abc123",
                SCHEDULER_IMAGE="shopping_erp_candidate/scheduler:TESTONLY-abc123",
                WEB_IMAGE="shopping_erp_candidate/web:TESTONLY-abc123",
            )
        )
        assert images["api"] == "shopping_erp_candidate/api:TESTONLY-abc123"
        assert images["scheduler"] == "shopping_erp_candidate/scheduler:TESTONLY-abc123"
        assert images["web"] == "shopping_erp_candidate/web:TESTONLY-abc123"

    def test_api_only_override_does_not_change_scheduler_or_web_selection(self, docker_ready):
        """api-only 배포 시나리오 - API_IMAGE만 지정해도 scheduler/web은 여전히
        기본값을 선택한다(실제 `up --no-deps api`가 그 서비스만 건드리는 것과
        일관됨 - config 렌더링 단계에서부터 다른 서비스는 영향받지 않는다)."""
        images = _compose_images(_env(API_IMAGE="shopping_erp_candidate/api:TESTONLY-onlyapi"))
        assert images["api"] == "shopping_erp_candidate/api:TESTONLY-onlyapi"
        assert images["scheduler"] == "shopping_erp-scheduler"
        assert images["web"] == "shopping_erp-web"

    def test_scheduler_only_override_does_not_change_api_or_web_selection(self, docker_ready):
        images = _compose_images(_env(SCHEDULER_IMAGE="shopping_erp_candidate/scheduler:TESTONLY-onlysched"))
        assert images["scheduler"] == "shopping_erp_candidate/scheduler:TESTONLY-onlysched"
        assert images["api"] == "shopping_erp-api"
        assert images["web"] == "shopping_erp-web"

    def test_base_compose_alone_reapplies_pinned_image_without_override_file(self, docker_ready):
        """요구사항 6 - override 파일 없이 base compose(docker-compose.yml)
        하나만 다시 적용해도, .env(여기서는 프로세스 환경변수로 대체)에 이미
        고정해 둔 이미지가 그대로 선택되어야 한다."""
        env = _env(
            API_IMAGE="shopping_erp_candidate/api:TESTONLY-reapply",
            SCHEDULER_IMAGE="shopping_erp_candidate/scheduler:TESTONLY-reapply",
        )
        first = _compose_images(env)
        second = _compose_images(env)  # 같은 base 파일을 그대로 두 번 더 적용해도 동일해야 한다.
        assert first["api"] == second["api"] == "shopping_erp_candidate/api:TESTONLY-reapply"
        assert first["scheduler"] == second["scheduler"] == "shopping_erp_candidate/scheduler:TESTONLY-reapply"


class TestConfigQuietAndSecretSafety:
    def test_config_quiet_succeeds_with_pinned_images(self, docker_ready):
        env = _env(
            API_IMAGE="shopping_erp_candidate/api:TESTONLY-quiet",
            SCHEDULER_IMAGE="shopping_erp_candidate/scheduler:TESTONLY-quiet",
        )
        result = _run_compose(env, "config", "--quiet")
        assert result.returncode == 0

    def test_rendered_config_does_not_echo_secret_values(self, docker_ready):
        """config --images 결과에는 이미지 이름만 담겨야 한다 - Secret 값이
        섞여 나오지 않는지 대표적으로 확인한다."""
        env = _env(API_IMAGE="shopping_erp_candidate/api:TESTONLY-secretcheck")
        result = _run_compose(env, "config", "--images")
        assert _BASE_ENV["JWT_SECRET_KEY"] not in result.stdout
        assert _BASE_ENV["CREDENTIAL_ENCRYPTION_KEY"] not in result.stdout
        assert _BASE_ENV["POSTGRES_PASSWORD"] not in result.stdout


class TestCatchupFlagComposeWiring:
    """fix/postgres-backup-missed-run-recovery 후속: POSTGRES_BACKUP_CATCHUP_ENABLED가
    config/settings.py에만 있고 docker-compose.yml의 scheduler.environment에
    전달되지 않으면, 운영 .env에 값을 넣어도 컨테이너 안에서는 항상 기본값
    (false)만 보여 기능이 절대 켜지지 않는다 - 정적 YAML 파싱이 아니라 실제
    `docker compose config`가 최종 렌더링한 값을 검증해야 이 배선 누락을
    확실히 잡을 수 있다."""

    def test_unset_env_var_selects_false_on_scheduler_only(self, docker_ready):
        cfg = _compose_config(_env())
        scheduler_env = cfg["services"]["scheduler"]["environment"]
        assert scheduler_env["POSTGRES_BACKUP_CATCHUP_ENABLED"] == "false"

    def test_true_override_propagates_to_scheduler(self, docker_ready):
        cfg = _compose_config(_env(POSTGRES_BACKUP_CATCHUP_ENABLED="true"))
        scheduler_env = cfg["services"]["scheduler"]["environment"]
        assert scheduler_env["POSTGRES_BACKUP_CATCHUP_ENABLED"] == "true"

    def test_false_override_still_renders_as_false(self, docker_ready):
        cfg = _compose_config(_env(POSTGRES_BACKUP_CATCHUP_ENABLED="false"))
        scheduler_env = cfg["services"]["scheduler"]["environment"]
        assert scheduler_env["POSTGRES_BACKUP_CATCHUP_ENABLED"] == "false"

    def test_other_services_never_receive_the_catchup_flag(self, docker_ready):
        """백업(및 그 보충 실행)은 scheduler만 수행한다 - api/web/db/redis에는
        이 변수 자체가 존재하면 안 된다(값이 false인 것과 "아예 없음"은
        다르다 - 실수로 다른 서비스에 잘못 옮겨붙는 회귀를 잡는다)."""
        cfg = _compose_config(_env(POSTGRES_BACKUP_CATCHUP_ENABLED="true"))
        for service in ("api", "web", "db", "redis"):
            env = cfg["services"][service].get("environment") or {}
            assert "POSTGRES_BACKUP_CATCHUP_ENABLED" not in env, f"{service}에 catch-up 플래그가 전달되면 안 됩니다."

    def test_existing_backup_settings_and_image_pinning_preserved(self, docker_ready):
        """catch-up 배선을 추가하면서 기존 POSTGRES_BACKUP_* 설정이나 이미지
        고정(${SCHEDULER_IMAGE:-...}) 계약을 깨지 않았는지 함께 확인한다."""
        cfg = _compose_config(
            _env(
                POSTGRES_BACKUP_CATCHUP_ENABLED="true",
                POSTGRES_BACKUP_ENABLED="true",
                POSTGRES_BACKUP_RETENTION_COUNT="7",
                SCHEDULER_IMAGE="shopping_erp_candidate/scheduler:TESTONLY-catchupwiring",
            )
        )
        scheduler = cfg["services"]["scheduler"]
        assert scheduler["environment"]["POSTGRES_BACKUP_ENABLED"] == "true"
        assert scheduler["environment"]["POSTGRES_BACKUP_RETENTION_COUNT"] == "7"
        assert scheduler["image"] == "shopping_erp_candidate/scheduler:TESTONLY-catchupwiring"


class TestRealCandidateImageRevisionVerification:
    """이전 라운드에서 실제로 만들어 둔 candidate 이미지가 로컬에 남아있을
    때만 실행되는 실제(mock 아닌) revision 검증 - 없으면 skip한다(이
    테스트가 새 이미지를 만들지 않는다는 요구사항 때문)."""

    def _require_images(self, *refs: str) -> None:
        missing = [r for r in refs if not _image_exists(r)]
        if missing:
            pytest.skip(f"이 테스트에 필요한 로컬 이미지가 없어 skip합니다: {missing}")

    def _run_script(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["python", "scripts/verify_deploy_images.py", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env={**__import__("os").environ, "PYTHONUTF8": "1"},
        )

    def test_known_matching_pair_passes_verification_script(self, docker_ready):
        """--expected-backend-revision을 명시한다 - 생략하면 git HEAD를 자동
        추론하는데, 이 저장소의 HEAD는 이미지가 빌드된 시점 이후로 계속
        앞서가므로(커밋마다 값이 달라짐) 이 테스트는 "그 시점의 특정 커밋
        쌍"을 검증하는 것이지 "지금 이 순간의 HEAD"를 검증하는 게 아니다."""
        self._require_images(*_KNOWN_MATCHING_PAIR.values())
        result = self._run_script(
            [
                "--api-image",
                _KNOWN_MATCHING_PAIR["api"],
                "--scheduler-image",
                _KNOWN_MATCHING_PAIR["scheduler"],
                "--expected-backend-revision",
                _KNOWN_BACKEND_REVISION,
            ]
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "[PASS]" in result.stdout

    def test_known_mismatched_pair_fails_verification_script(self, docker_ready):
        self._require_images(_KNOWN_MATCHING_PAIR["api"], _KNOWN_MISMATCHED_SCHEDULER)
        result = self._run_script(
            ["--api-image", _KNOWN_MATCHING_PAIR["api"], "--scheduler-image", _KNOWN_MISMATCHED_SCHEDULER]
        )
        assert result.returncode == 1
        assert "다른 커밋" in result.stdout

    def test_known_backend_and_web_from_different_commits_both_pass(self, docker_ready):
        """실제 운영 시나리오: backend는 새 커밋(b5d9485)으로 배포하지만
        web은 프론트 변경이 없어 이전 커밋(434a3d0) 이미지를 그대로 쓴다 -
        서로 다른 커밋이어도 각자 자신의 기대값과 일치하면 통과해야 한다."""
        self._require_images(*_KNOWN_MATCHING_PAIR.values(), _KNOWN_WEB_IMAGE)
        result = self._run_script(
            [
                "--api-image",
                _KNOWN_MATCHING_PAIR["api"],
                "--scheduler-image",
                _KNOWN_MATCHING_PAIR["scheduler"],
                "--web-image",
                _KNOWN_WEB_IMAGE,
                "--expected-backend-revision",
                _KNOWN_BACKEND_REVISION,
                "--expected-web-revision",
                _KNOWN_WEB_REVISION,
            ]
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "[PASS]" in result.stdout

    def test_known_web_revision_mismatch_fails(self, docker_ready):
        self._require_images(*_KNOWN_MATCHING_PAIR.values(), _KNOWN_WEB_IMAGE)
        result = self._run_script(
            [
                "--api-image",
                _KNOWN_MATCHING_PAIR["api"],
                "--scheduler-image",
                _KNOWN_MATCHING_PAIR["scheduler"],
                "--web-image",
                _KNOWN_WEB_IMAGE,
                "--expected-backend-revision",
                _KNOWN_BACKEND_REVISION,
                "--expected-web-revision",
                "0000000000000000000000000000000000000000",
            ]
        )
        assert result.returncode == 1
        assert "web 이미지의 revision" in result.stdout

    def test_web_image_missing_fails(self, docker_ready):
        self._require_images(*_KNOWN_MATCHING_PAIR.values())
        result = self._run_script(
            [
                "--api-image",
                _KNOWN_MATCHING_PAIR["api"],
                "--scheduler-image",
                _KNOWN_MATCHING_PAIR["scheduler"],
                "--web-image",
                "shopping_erp_candidate/web:TESTONLY-does-not-exist",
                "--expected-backend-revision",
                _KNOWN_BACKEND_REVISION,
                "--expected-web-revision",
                _KNOWN_WEB_REVISION,
            ]
        )
        assert result.returncode == 1
        assert "web 이미지가 로컬에 없습니다" in result.stdout

    def test_api_and_scheduler_share_identical_image_id(self, docker_ready):
        """api/scheduler는 완전히 동일한 Dockerfile로 빌드되므로, 같은
        candidate 태그 쌍은 Image ID까지 동일해야 한다(한 번만 빌드하고
        태그만 추가하는 배포 관례를 그대로 검증)."""
        self._require_images(*_KNOWN_MATCHING_PAIR.values())

        def _id(ref: str) -> str:
            r = subprocess.run(
                ["docker", "image", "inspect", "-f", "{{.Id}}", ref], capture_output=True, text=True, timeout=10
            )
            return r.stdout.strip()

        assert _id(_KNOWN_MATCHING_PAIR["api"]) == _id(_KNOWN_MATCHING_PAIR["scheduler"])
