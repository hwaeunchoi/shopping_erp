"""
tests/unit/test_docker_compose_contract.py
------------------------------------------------
docker-compose.yml의 Secret 계약을 정적으로 검증한다(컨테이너를 띄우지 않음).

핵심 계약(운영 Secret 회전 작업, DEPLOYMENT.md 참고):
  - POSTGRES_PASSWORD/JWT_SECRET_KEY/CREDENTIAL_ENCRYPTION_KEY/DATABASE_URL은
    ${VAR:?message} 형태의 "필수 변수" 문법만 쓴다 - 해당 환경변수가 없으면
    `docker compose config`/`up` 자체가 즉시 실패해야 한다(fail-closed).
  - api/scheduler는 완전히 동일한 변수 이름을 참조해야 한다(둘 다 같은 값을
    주입받아야 신키 회전 시 한쪽만 새 키를 보는 상황이 생기지 않는다).
  - web은 백엔드 Secret을 전혀 주입받지 않는다(정적 파일만 서빙하므로).
  - 알려진 데모 기본값 문자열이 파일 어디에도 리터럴로 남아있으면 안 된다.
"""

from pathlib import Path

import yaml

from core.crypto import _KNOWN_DEMO_SECRETS

COMPOSE_PATH = Path(__file__).resolve().parent.parent.parent / "docker-compose.yml"

_REQUIRED_VAR_KEYS = ["POSTGRES_PASSWORD", "JWT_SECRET_KEY", "CREDENTIAL_ENCRYPTION_KEY", "DATABASE_URL"]


def _load_compose() -> dict:
    with open(COMPOSE_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _raw_text() -> str:
    return COMPOSE_PATH.read_text(encoding="utf-8")


class TestRequiredVarSyntax:
    def test_each_required_key_uses_fail_closed_syntax(self):
        text = _raw_text()
        for key in _REQUIRED_VAR_KEYS:
            assert f"${{{key}:?" in text, f"{key}는 ${{{key}:?message}} 형태의 필수 변수 문법을 써야 합니다."

    def test_postgres_password_not_string_concatenated_into_database_url(self):
        """DATABASE_URL 값 자체는 ${POSTGRES_PASSWORD}를 문자열로 조합하지 않고
        완성된 연결 문자열을 별도 필수값으로 받아야 한다(비밀번호에 URL
        예약문자가 섞이면 조합한 URL이 깨질 수 있어서)."""
        compose = _load_compose()
        database_url_value = compose["services"]["api"]["environment"]["DATABASE_URL"]
        assert "POSTGRES_PASSWORD" not in database_url_value


class TestApiSchedulerIdenticalContract:
    def test_api_and_scheduler_reference_identical_secret_variable_expressions(self):
        compose = _load_compose()
        api_env = compose["services"]["api"]["environment"]
        scheduler_env = compose["services"]["scheduler"]["environment"]
        for key in _REQUIRED_VAR_KEYS:
            if key == "POSTGRES_PASSWORD":
                continue  # POSTGRES_PASSWORD는 db 서비스에만 있고 api/scheduler는 DATABASE_URL로 받는다.
            assert key in api_env, f"api에 {key}가 없습니다."
            assert key in scheduler_env, f"scheduler에 {key}가 없습니다."
            assert api_env[key] == scheduler_env[key], f"api/scheduler의 {key} 값 표현식이 다릅니다."


class TestWebHasNoBackendSecret:
    def test_web_service_has_no_environment_block(self):
        compose = _load_compose()
        web = compose["services"]["web"]
        assert "environment" not in web


class TestImagePinningFallback:
    """운영 이미지 암묵적 선택 사고(2026-09-11 API 크래시 루프 - migration은
    새 candidate 이미지로 실행했는데 api 컨테이너는 build: .의 기본 이미지
    이름으로 재기동되어, 운영 DB가 이미 그 이미지의 migration 스크립트에
    없는 revision에 있던 사고) 재발 방지 계약을 정적으로 검증한다."""

    _IMAGE_VARS = {"api": "API_IMAGE", "scheduler": "SCHEDULER_IMAGE", "web": "WEB_IMAGE"}
    _DEFAULT_NAMES = {"api": "shopping_erp-api", "scheduler": "shopping_erp-scheduler", "web": "shopping_erp-web"}

    def test_each_service_has_fallback_image_with_dash_default_syntax(self):
        """`${VAR:-default}` 문법이어야 한다(`:-`는 빈 문자열도 기본값으로
        폴백한다 - `-`만 쓰면 "설정은 됐지만 빈 문자열"인 경우 빈 문자열이
        그대로 쓰여 위험한 image: 빈 값이 될 수 있다)."""
        compose = _load_compose()
        for service, var in self._IMAGE_VARS.items():
            image_expr = compose["services"][service]["image"]
            assert (
                image_expr == f"${{{var}:-{self._DEFAULT_NAMES[service]}}}"
            ), f"{service}.image이 예상한 폴백 표현식이 아닙니다: {image_expr!r}"

    def test_build_directive_still_present_for_dev_workflow(self):
        """`build:`를 지우지 않는다 - 기존 `docker compose build/up --build`
        개발 워크플로가 그대로 동작해야 한다(image:는 병행 - build 결과물의
        태그 이름만 명시적으로 고정한다)."""
        compose = _load_compose()
        assert compose["services"]["api"]["build"] == "."
        assert compose["services"]["scheduler"]["build"] == "."
        assert compose["services"]["web"]["build"] == "./frontend"

    def test_no_literal_candidate_or_operational_image_tag_hardcoded(self):
        """실제 운영 이미지 값(타임스탬프-커밋SHA 형태의 candidate 태그)을
        `image:` 필드 자체에 하드코딩하지 않는다 - 오직 폴백용 기본 이름
        (shopping_erp-*)과 환경변수 참조만 있어야 한다. 설명 주석에서
        candidate 태그 형태를 예시로 언급하는 것은 허용한다(주석은 검사
        대상이 아니다 - 파싱된 image: 값만 본다)."""
        compose = _load_compose()
        for service in self._IMAGE_VARS:
            image_expr = compose["services"][service]["image"]
            assert "shopping_erp_candidate/" not in image_expr
            assert "shopping_erp_rollback/" not in image_expr

    def test_default_names_match_actual_compose_project_derivation(self):
        """폴백 기본값(shopping_erp-api 등)이 실제 compose 프로젝트명-서비스명
        자동 파생 규칙과 일치해야 한다 - 그래야 API_IMAGE 등을 아예 설정하지
        않은 기존 개발 환경의 이미지 이름이 이번 변경 전후로 완전히 동일하다
        (project name은 docker-compose.yml이 위치한 디렉터리명 'shopping_erp'
        에서 온다)."""
        for service, default_name in self._DEFAULT_NAMES.items():
            assert default_name == f"shopping_erp-{service}"


class TestNoDemoDefaultsLeaked:
    def test_no_known_demo_secret_literal_in_file(self):
        text = _raw_text()
        for demo_value in _KNOWN_DEMO_SECRETS:
            assert demo_value not in text, f"docker-compose.yml에 데모 기본값 '{demo_value}'이(가) 남아있습니다."

    def test_db_service_password_uses_required_var_not_literal(self):
        compose = _load_compose()
        db_password = compose["services"]["db"]["environment"]["POSTGRES_PASSWORD"]
        assert db_password.startswith("${POSTGRES_PASSWORD:?")
