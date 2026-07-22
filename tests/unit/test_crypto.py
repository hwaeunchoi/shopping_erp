"""
tests/unit/test_crypto.py
------------------------------
core.crypto(Fernet 기반 API Credential 암호화) 단위 테스트.
"""

from core.crypto import decrypt_value, encrypt_value


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
