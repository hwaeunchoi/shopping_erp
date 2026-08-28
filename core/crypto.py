"""
core/crypto.py
------------------
API Credential(외부 플랫폼 API 인증정보) 등 민감정보의 애플리케이션 레벨
암호화/복호화 유틸리티(Fernet 대칭키).

config.settings.credential_encryption_key(운영 환경에서는 반드시 무작위값으로
교체해야 하는 마스터 키 문자열)를 SHA-256으로 해시해 Fernet이 요구하는
32바이트 urlsafe base64 키로 변환한다 - 설정값 형식을 바꾸지 않고도 Fernet
키 요구사항(정확히 32바이트)을 만족시키기 위함이다.

build_fernet()이 이 파생 규칙의 유일한 구현체다. 앱의 캐시된 _fernet()과
scripts/rotate_credential_key.py(구키·신키 Fernet을 동시에 만들어야 하는
회전 도구)가 반드시 이 함수 하나만 거쳐야 기존 암호문과의 호환이 보장된다.
raw secret을 Fernet()에 직접 넘기면(파생 과정을 건너뛰면) 기존에 저장된
모든 암호문을 복호화할 수 없게 되므로 절대 그렇게 하지 않는다.
"""

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from config.settings import settings

__all__ = [
    "encrypt_value",
    "decrypt_value",
    "InvalidToken",
    "build_fernet",
    "InsecureSecretError",
    "validate_production_secret",
    "validate_startup_secrets",
]


class InsecureSecretError(ValueError):
    """운영 환경에 안전하지 않은 Secret 값이 설정됐을 때 발생한다."""


# 알려진 데모/placeholder 기본값 - 운영 환경에서 이 값이 그대로 쓰이면 반드시
# 거부한다. config.settings.Settings의 파이썬 레벨 기본값과 docker-compose.yml/
# .env.example에 예시로 적혀 있던 값을 전부 포함한다(어느 것이든 그대로 쓰이면
# 안전하지 않다는 사실은 동일하다).
_KNOWN_DEMO_SECRETS = frozenset(
    {
        "CHANGE_ME_IN_PRODUCTION",
        "please-change-this-to-a-random-secret-string",
        "please-change-this-to-a-generated-fernet-key",
    }
)

# raw secret(해시 전) 최소 길이. SHA-256으로 해시되긴 하지만 원문 자체가 너무
# 짧으면 실질 엔트로피가 부족하다(공격자가 짧은 사전으로 원문을 추정 후 같은
# 파생을 재현할 수 있음). secrets.token_urlsafe(32)/Fernet.generate_key() 등
# 권장 생성법은 전부 40자를 훌쩍 넘으므로, 기존 기본값 "CHANGE_ME_IN_PRODUCTION"
# (23자)보다 조금 더 긴 20자를 최소선으로 삼아 명백히 짧은 값만 걸러낸다 -
# 이보다 더 크게 잡으면 이 저장소의 기존 로컬 개발 관행과 충돌할 여지가 있어
# "명백히 안전하지 않은 값만 차단"하는 최소한의 기준으로 잡았다.
_MIN_SECRET_LENGTH = 20


def build_fernet(secret: str) -> Fernet:
    """raw secret 문자열로부터 Fernet 객체를 만든다.

    앱(_fernet())과 회전 도구가 공유하는 유일한 파생 규칙(SHA-256 -> urlsafe
    base64)이다 - 이 함수 밖에서 별도로 Fernet 키를 파생하면 기존 암호문과
    호환이 깨진다."""
    if not secret:
        raise InsecureSecretError("암호화 키가 비어 있습니다.")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def validate_production_secret(name: str, value: str, *, allow_demo_default: bool = False) -> None:
    """운영 기동 시 호출하는 순수 값 검증. name은 오류 메시지에만 쓰이고,
    value(실제 Secret)는 예외 메시지에 절대 포함하지 않는다.

    allow_demo_default는 기본 False(기존과 동일하게 항상 거부)다. 이 저장소의
    모든 앱 기동 경로(api/main.py lifespan, scheduler/scheduler.py)는 이
    인자를 넘기지 않으므로 동작이 전혀 바뀌지 않는다. True는 오직
    scripts/rotate_credential_key.py가 안전하지 않은 기존(레거시) 구키에서
    벗어나는 일회성 마이그레이션을 허용하기 위해 구키에 한해서만 사용한다 -
    신키 검증에는 절대 쓰지 않는다."""
    if not value:
        raise InsecureSecretError(f"{name}이(가) 설정되지 않았습니다.")
    if not allow_demo_default and value in _KNOWN_DEMO_SECRETS:
        raise InsecureSecretError(f"{name}이(가) 공개된 데모 기본값입니다. 운영 값으로 교체하세요.")
    if len(value) < _MIN_SECRET_LENGTH:
        raise InsecureSecretError(f"{name}이(가) 너무 짧습니다(최소 {_MIN_SECRET_LENGTH}자 이상 필요).")


def validate_startup_secrets(jwt_secret_key: str, credential_encryption_key: str) -> None:
    """프로세스 시작 시 호출한다 - JWT/Credential 키 각각의 안전 기준과, 두 값이
    서로 다른지까지 확인한다(DB 접속 전에 가능한 순수 검증만 수행 - 기존 credential
    암호문 전체를 복호화해보는 무거운 검증은 여기 넣지 않는다. 시작 경로에 넣으면
    Secret 설정 문제와 DB/데이터 문제가 뒤섞여 장애 범위가 넓어지므로, 그 검증은
    배포 전 scripts/rotate_credential_key.py 같은 별도 도구에서 수행한다)."""
    validate_production_secret("JWT_SECRET_KEY", jwt_secret_key)
    validate_production_secret("CREDENTIAL_ENCRYPTION_KEY", credential_encryption_key)
    if jwt_secret_key == credential_encryption_key:
        raise InsecureSecretError("JWT_SECRET_KEY와 CREDENTIAL_ENCRYPTION_KEY가 동일한 값입니다.")


@lru_cache
def _fernet() -> Fernet:
    return build_fernet(settings.credential_encryption_key)


def encrypt_value(plain_value: str) -> str:
    """평문을 암호화해 저장 가능한 문자열로 반환한다."""
    return _fernet().encrypt(plain_value.encode("utf-8")).decode("utf-8")


def decrypt_value(encrypted_value: str) -> str:
    """암호화된 문자열을 평문으로 복호화한다. 실패 시 cryptography.fernet.InvalidToken을 던진다."""
    return _fernet().decrypt(encrypted_value.encode("utf-8")).decode("utf-8")
