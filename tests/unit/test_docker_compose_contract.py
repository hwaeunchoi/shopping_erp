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


class TestNoDemoDefaultsLeaked:
    def test_no_known_demo_secret_literal_in_file(self):
        text = _raw_text()
        for demo_value in _KNOWN_DEMO_SECRETS:
            assert demo_value not in text, f"docker-compose.yml에 데모 기본값 '{demo_value}'이(가) 남아있습니다."

    def test_db_service_password_uses_required_var_not_literal(self):
        compose = _load_compose()
        db_password = compose["services"]["db"]["environment"]["POSTGRES_PASSWORD"]
        assert db_password.startswith("${POSTGRES_PASSWORD:?")
