"""
tests/integration/test_api_startup_security.py
------------------------------------------------------
api/main.py의 lifespan에 연결된 validate_startup_secrets() fail-closed
검증을 실제 ASGI lifespan 경로(TestClient의 `with TestClient(app) as c:`)로
검증한다.

TestClient(app)에 진입하는 순간(우회 불가능 - uvicorn 실제 기동과 동일한
ASGI lifespan.startup 프로토콜) lifespan이 실행되고, 그 안에서 예외가
나면 `with TestClient(app):` 자체가 그 예외를 그대로 다시 던진다. 이
파일은 그 사실 자체를 이용해 "정말로 서버가 뜨지 못하는지"를 검증한다 -
validate_startup_secrets를 monkeypatch로 호출됐는지만 확인하는 게 아니라.

`settings`는 프로세스 전체에서 공유되는 단일 pydantic 객체이므로, 각
테스트는 monkeypatch로 settings.jwt_secret_key/credential_encryption_key를
바꿔서 시나리오를 재현하고 pytest가 자동으로 원복한다.
"""

import pytest
from fastapi.testclient import TestClient

from api.main import app
from config.settings import settings
from core.crypto import InsecureSecretError

# 이 파일 전용 dummy secret - 데모 기본값이 아니고 최소 길이(20자) 이상이며
# JWT/Credential 서로 다른 값이다.
_VALID_JWT = "startup-test-only-valid-jwt-secret-AAAAAAAAAA"
_VALID_CRED = "startup-test-only-valid-credential-secret-BBBBBBBBBB"


class TestApiLifespanFailClosed:
    def test_valid_secrets_allow_normal_startup(self, monkeypatch):
        monkeypatch.setattr(settings, "jwt_secret_key", _VALID_JWT)
        monkeypatch.setattr(settings, "credential_encryption_key", _VALID_CRED)

        with TestClient(app) as client:
            resp = client.get("/health")

        assert resp.status_code == 200

    def test_missing_jwt_secret_fails_startup(self, monkeypatch):
        monkeypatch.setattr(settings, "jwt_secret_key", "")
        monkeypatch.setattr(settings, "credential_encryption_key", _VALID_CRED)

        with pytest.raises(InsecureSecretError), TestClient(app):
            pass

    def test_missing_credential_secret_fails_startup(self, monkeypatch):
        monkeypatch.setattr(settings, "jwt_secret_key", _VALID_JWT)
        monkeypatch.setattr(settings, "credential_encryption_key", "")

        with pytest.raises(InsecureSecretError), TestClient(app):
            pass

    def test_jwt_demo_default_fails_startup(self, monkeypatch):
        monkeypatch.setattr(settings, "jwt_secret_key", "CHANGE_ME_IN_PRODUCTION")
        monkeypatch.setattr(settings, "credential_encryption_key", _VALID_CRED)

        with pytest.raises(InsecureSecretError), TestClient(app):
            pass

    def test_credential_demo_default_fails_startup(self, monkeypatch):
        monkeypatch.setattr(settings, "jwt_secret_key", _VALID_JWT)
        monkeypatch.setattr(settings, "credential_encryption_key", "CHANGE_ME_IN_PRODUCTION")

        with pytest.raises(InsecureSecretError), TestClient(app):
            pass

    def test_identical_jwt_and_credential_secrets_fail_startup(self, monkeypatch):
        monkeypatch.setattr(settings, "jwt_secret_key", _VALID_JWT)
        monkeypatch.setattr(settings, "credential_encryption_key", _VALID_JWT)

        with pytest.raises(InsecureSecretError), TestClient(app):
            pass

    def test_startup_failure_message_never_contains_secret_value(self, monkeypatch):
        monkeypatch.setattr(settings, "jwt_secret_key", "")
        monkeypatch.setattr(settings, "credential_encryption_key", _VALID_CRED)

        try:
            with TestClient(app):
                pass
        except InsecureSecretError as e:
            assert _VALID_CRED not in str(e)
        else:
            pytest.fail("InsecureSecretError를 기대했으나 발생하지 않았습니다.")


class TestApiAndSchedulerShareSameValidationPolicy:
    def test_api_and_scheduler_call_the_identical_validation_function(self):
        import api.main as api_main_mod
        import scheduler.scheduler as scheduler_mod
        from core.crypto import validate_startup_secrets

        assert api_main_mod.validate_startup_secrets is validate_startup_secrets
        assert scheduler_mod.validate_startup_secrets is validate_startup_secrets

    def test_same_inputs_produce_same_pass_fail_outcome_for_both_entry_points(self):
        from core.crypto import InsecureSecretError, validate_startup_secrets

        # 정상 케이스 - 둘 다 예외 없이 통과해야 한다(같은 함수이므로 자명하지만,
        # "동일한 정책"이라는 요구사항을 회귀 테스트로 고정해둔다).
        validate_startup_secrets(_VALID_JWT, _VALID_CRED)

        # 데모 기본값 케이스 - 둘 다 거부해야 한다.
        with pytest.raises(InsecureSecretError):
            validate_startup_secrets("CHANGE_ME_IN_PRODUCTION", _VALID_CRED)


class TestTestOnlySecretIsNotMistakenForProductionExample:
    def test_startup_test_dummy_secrets_are_not_in_env_example(self):
        from pathlib import Path

        env_example = Path(__file__).resolve().parent.parent.parent / ".env.example"
        text = env_example.read_text(encoding="utf-8")

        assert _VALID_JWT not in text
        assert _VALID_CRED not in text

    def test_conftest_injected_secrets_are_not_in_env_example(self):
        """tests/conftest.py가 os.environ에 주입하는 실제 값도 .env.example의
        운영 예시로 오인될 수 없어야 한다(별도 파일, 별도 값)."""
        import os
        from pathlib import Path

        env_example = Path(__file__).resolve().parent.parent.parent / ".env.example"
        text = env_example.read_text(encoding="utf-8")

        injected_jwt = os.environ.get("JWT_SECRET_KEY", "")
        injected_cred = os.environ.get("CREDENTIAL_ENCRYPTION_KEY", "")
        assert injected_jwt and injected_jwt not in text
        assert injected_cred and injected_cred not in text
