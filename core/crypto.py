"""
core/crypto.py
------------------
API Credential(외부 플랫폼 API 인증정보) 등 민감정보의 애플리케이션 레벨
암호화/복호화 유틸리티(Fernet 대칭키).

config.settings.credential_encryption_key(운영 환경에서는 반드시 무작위값으로
교체해야 하는 마스터 키 문자열)를 SHA-256으로 해시해 Fernet이 요구하는
32바이트 urlsafe base64 키로 변환한다 - 설정값 형식을 바꾸지 않고도
Fernet 키 요구사항(정확히 32바이트)을 만족시키기 위함이다.
"""

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from config.settings import settings

__all__ = ["encrypt_value", "decrypt_value", "InvalidToken"]


@lru_cache
def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.credential_encryption_key.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_value(plain_value: str) -> str:
    """평문을 암호화해 저장 가능한 문자열로 반환한다."""
    return _fernet().encrypt(plain_value.encode("utf-8")).decode("utf-8")


def decrypt_value(encrypted_value: str) -> str:
    """암호화된 문자열을 평문으로 복호화한다. 실패 시 cryptography.fernet.InvalidToken을 던진다."""
    return _fernet().decrypt(encrypted_value.encode("utf-8")).decode("utf-8")
