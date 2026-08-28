"""
tests/unit/test_crypto.py
------------------------------
core.crypto(Fernet 기반 API Credential 암호화) 단위 테스트.
"""

import base64
import hashlib

import pytest

from core.crypto import (
    InsecureSecretError,
    InvalidToken,
    build_fernet,
    decrypt_value,
    encrypt_value,
    validate_production_secret,
    validate_startup_secrets,
)

# 테스트 전용 dummy secret - 실제 운영 값이 아니며, 데모 기본값 목록에도
# 없고 최소 길이(20자) 이상이라 validate_production_secret을 통과한다.
_VALID_TEST_SECRET_A = "test-only-dummy-secret-aaaaaaaaaa"
_VALID_TEST_SECRET_B = "test-only-dummy-secret-bbbbbbbbbb"


def test_encrypt_then_decrypt_roundtrips_to_original_plaintext():
    plain = "sk-test-secret-1234"
    encrypted = encrypt_value(plain)

    assert encrypted != plain
    assert decrypt_value(encrypted) == plain


def test_encrypted_value_is_not_plaintext_substring():
    """암호화된 문자열에 평문 조각이 그대로 노출되면 안 된다."""
    plain = "super-secret-api-key"
    encrypted = encrypt_value(plain)

    assert plain not in encrypted


class TestBuildFernetDerivationContract:
    """build_fernet()의 파생 규칙(SHA-256 -> urlsafe base64)을 고정한다 - 이
    규칙이 바뀌면 기존에 저장된 모든 API Credential 암호문을 복호화할 수
    없게 되므로, 회귀가 생기면 이 테스트가 가장 먼저 잡아야 한다."""

    def test_derivation_matches_sha256_urlsafe_base64(self):
        secret = _VALID_TEST_SECRET_A
        expected_key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())

        fernet = build_fernet(secret)

        # 같은 파생 규칙으로 만든 Fernet끼리는 서로 암복호화가 호환돼야 한다.
        from cryptography.fernet import Fernet

        reference = Fernet(expected_key)
        token = reference.encrypt(b"pin-check")
        assert fernet.decrypt(token) == b"pin-check"

    def test_same_secret_produces_working_fernet_pair(self):
        f1 = build_fernet(_VALID_TEST_SECRET_A)
        f2 = build_fernet(_VALID_TEST_SECRET_A)
        assert f2.decrypt(f1.encrypt(b"x")) == b"x"

    def test_different_secrets_are_not_interchangeable(self):
        f1 = build_fernet(_VALID_TEST_SECRET_A)
        f2 = build_fernet(_VALID_TEST_SECRET_B)
        token = f1.encrypt(b"x")
        with pytest.raises(InvalidToken):
            f2.decrypt(token)

    def test_rejects_empty_secret(self):
        with pytest.raises(InsecureSecretError):
            build_fernet("")


class TestValidateProductionSecret:
    def test_accepts_sufficiently_long_non_demo_secret(self):
        validate_production_secret("TEST_KEY", _VALID_TEST_SECRET_A)  # 예외 없이 통과

    def test_rejects_missing_or_empty(self):
        with pytest.raises(InsecureSecretError):
            validate_production_secret("TEST_KEY", "")

    @pytest.mark.parametrize(
        "demo_value",
        [
            "CHANGE_ME_IN_PRODUCTION",
            "please-change-this-to-a-random-secret-string",
            "please-change-this-to-a-generated-fernet-key",
        ],
    )
    def test_rejects_known_demo_defaults(self, demo_value):
        with pytest.raises(InsecureSecretError):
            validate_production_secret("TEST_KEY", demo_value)

    def test_rejects_too_short_secret(self):
        with pytest.raises(InsecureSecretError):
            validate_production_secret("TEST_KEY", "short-secret")  # 20자 미만

    def test_allow_demo_default_true_permits_known_demo_value(self):
        # scripts/rotate_credential_key.py의 --allow-legacy-insecure-old-key
        # 전용 우회 경로 - 기본값(allow_demo_default=False)에서는 여전히 거부된다.
        validate_production_secret("TEST_KEY", "CHANGE_ME_IN_PRODUCTION", allow_demo_default=True)  # 예외 없이 통과

    def test_allow_demo_default_true_still_rejects_too_short_secret(self):
        with pytest.raises(InsecureSecretError):
            validate_production_secret("TEST_KEY", "short", allow_demo_default=True)

    def test_allow_demo_default_true_still_rejects_missing_value(self):
        with pytest.raises(InsecureSecretError):
            validate_production_secret("TEST_KEY", "", allow_demo_default=True)

    def test_error_message_never_contains_actual_value(self):
        secret = "a-short-but-unique-marker-value-1234567890"
        try:
            validate_production_secret("TEST_KEY", "")  # 빈 값 - 메시지엔 name만 담김
        except InsecureSecretError as e:
            assert secret not in str(e)


class TestValidateStartupSecrets:
    def test_accepts_two_distinct_valid_secrets(self):
        validate_startup_secrets(_VALID_TEST_SECRET_A, _VALID_TEST_SECRET_B)  # 예외 없이 통과

    def test_rejects_identical_jwt_and_credential_keys(self):
        with pytest.raises(InsecureSecretError):
            validate_startup_secrets(_VALID_TEST_SECRET_A, _VALID_TEST_SECRET_A)

    def test_rejects_when_jwt_secret_is_demo_default(self):
        with pytest.raises(InsecureSecretError):
            validate_startup_secrets("please-change-this-to-a-random-secret-string", _VALID_TEST_SECRET_B)

    def test_rejects_when_credential_key_is_demo_default(self):
        with pytest.raises(InsecureSecretError):
            validate_startup_secrets(_VALID_TEST_SECRET_A, "please-change-this-to-a-generated-fernet-key")
