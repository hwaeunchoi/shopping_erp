"""
core/security.py
------------------
비밀번호 해시(bcrypt) 및 JWT 토큰 발급/검증 유틸리티.
SRS FR-USER-01 (비밀번호는 해시로 저장) 대응.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from jose import JWTError, jwt
from passlib.context import CryptContext

from config.settings import settings

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain_password: str) -> str:
    """평문 비밀번호를 bcrypt로 해시한다."""
    return _pwd_context.hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    """평문 비밀번호가 저장된 해시와 일치하는지 검증한다."""
    return _pwd_context.verify(plain_password, password_hash)


def create_access_token(subject: str, extra_claims: Optional[dict[str, Any]] = None) -> str:
    """로그인 성공 시 발급하는 JWT 액세스 토큰."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    payload: dict[str, Any] = {"sub": subject, "exp": expire}
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> Optional[dict[str, Any]]:
    """JWT 토큰을 검증하고 payload를 반환한다. 검증 실패 시 None."""
    try:
        return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None
