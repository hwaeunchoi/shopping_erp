"""
integrations/malls/errors.py
------------------------------
마켓플레이스(쇼핑몰) 연동 전용 예외.

목적: 운영 수집 경로에서 "인증정보 없음/미지원 기능/외부 API 실패"를 더미 데이터로
숨기지 않고 명시적으로 실패시키기 위한 오류 타입을 제공한다. 서비스·스케줄러·API가
이 타입들을 잡아 안전하게(개인정보·시크릿·원본응답 노출 없이) 처리한다.

노출 금지 원칙: 예외 메시지·속성에는 Secret/Authorization/HMAC 서명/원본 응답 본문/
요청 URL 전체/vendorId/sellerId/주문번호/개인정보를 담지 않는다. 안전한 짧은
reason_code와 (있으면) 숫자 HTTP 상태 코드만 담는다.
"""

from contextlib import contextmanager
from typing import Iterator, Optional

import httpx


class MarketplaceError(Exception):
    """마켓플레이스 연동 공통 예외 베이스."""


class MarketplaceCredentialMissingError(MarketplaceError):
    """인증정보가 없거나 불완전/사용 불가하여 실 API를 호출할 수 없다.

    전체/일부 누락, 세션·platform_id 없음, 복호화·서명키 형식 오류 등 "연결정보를
    확인해야 하는" 상황. 사용자 조치(설정 화면에서 인증정보 확인)가 필요하다.
    """

    def __init__(self, marketplace_code: str) -> None:
        self.marketplace_code = marketplace_code
        super().__init__(f"{marketplace_code}: 쇼핑몰 연결정보가 없거나 사용할 수 없습니다.")


class MarketplaceCapabilityUnsupportedError(MarketplaceError):
    """해당 채널/기능을 아직 지원하지 않는다(미구현·미검증).

    "지원하지만 결과 0건"과 명확히 구분된다 - 0건은 정상 성공([]), 미지원은 이 예외.
    """

    def __init__(self, marketplace_code: str, capability: str) -> None:
        self.marketplace_code = marketplace_code
        self.capability = capability
        super().__init__(f"{marketplace_code}: '{capability}' 기능은 아직 지원하지 않습니다.")


class MarketplaceValidationError(MarketplaceError, ValueError):
    """이 모듈/연동 커넥터가 필드명·정적 문구만으로 직접 구성한, 노출해도 안전한
    검증 오류 전용 타입이다.

    services.product_publish_service/product_sync_dispatch_service의
    _describe_exception()은 이 타입의 메시지만 ExternalCommand.error_code에
    그대로 담는다(그 외 ValueError를 포함한 다른 모든 예외는 str(exc)를 신뢰하지
    않고 일반 오류 코드로 대체한다) - "우리가 의도적으로 안전하게 작성한 메시지"와
    "예상치 못한 예외(라이브러리 내부 오류·버그 등)의 원문"을 타입으로 구분해,
    후자가 실수로 안전하다고 오인되어 DB/API/로그에 그대로 남는 것을 막기
    위함이다. 이 타입을 사용하는 쪽은 메시지에 필드명/정적 문구/이미 운영자에게
    공개된 값(예: 채널이 확인해 준 옵션 후보 id)만 담아야 한다 - 원본 응답
    본문·요청 URL·Secret·PII는 여전히 금지(모듈 docstring 참고)."""


class MarketplaceExternalAPIError(MarketplaceError):
    """외부 API 호출 실패(인증 거부/HTTP 오류/timeout/연결 실패/응답 파싱 실패 등).

    메시지·속성에 원본 응답/URL/Secret/PII를 담지 않는다. 안전한 reason_code,
    재시도 가능 여부, 마켓 코드, (있으면) 숫자 HTTP 상태만 보관한다.
    """

    def __init__(
        self, marketplace_code: str, reason_code: str, retryable: bool, http_status: Optional[int] = None
    ) -> None:
        self.marketplace_code = marketplace_code
        self.reason_code = reason_code
        self.retryable = retryable
        self.http_status = http_status
        super().__init__(f"{marketplace_code}: 외부 API 오류(reason={reason_code}).")


def raise_for_status(marketplace_code: str, status_code: int) -> None:
    """비정상 HTTP 상태를 안전한 MarketplaceExternalAPIError로 변환한다(응답 본문 미포함).

    - 401/403 -> AUTH_FAILED (retryable=False)
    - 429     -> RATE_LIMITED (retryable=True)
    - 5xx     -> SERVER_ERROR (retryable=True)
    - 그 외    -> BAD_RESPONSE (retryable=False)
    """
    if status_code == 200:
        return
    if status_code in (401, 403):
        reason, retryable = "AUTH_FAILED", False
    elif status_code == 429:
        reason, retryable = "RATE_LIMITED", True
    elif 500 <= status_code < 600:
        reason, retryable = "SERVER_ERROR", True
    else:
        reason, retryable = "BAD_RESPONSE", False
    raise MarketplaceExternalAPIError(marketplace_code, reason, retryable, http_status=status_code)


@contextmanager
def external_call(marketplace_code: str) -> Iterator[None]:
    """외부 HTTP 호출/응답 파싱 블록을 감싸 httpx·파싱 예외를 안전하게 변환한다.

    - timeout          -> TIMEOUT (retryable=True)
    - 연결 실패          -> CONNECT_FAILED (retryable=True)
    - 기타 httpx 오류    -> TRANSPORT_ERROR (retryable=True)
    - JSON/구조 파싱 실패 -> PARSE_FAILED (retryable=False)
    원본 예외 문자열(응답 본문·URL 포함 가능)은 재-raise 메시지에 담지 않는다.
    """
    try:
        yield
    except MarketplaceError:
        raise
    except httpx.TimeoutException as e:
        raise MarketplaceExternalAPIError(marketplace_code, "TIMEOUT", True) from e
    except httpx.ConnectError as e:
        raise MarketplaceExternalAPIError(marketplace_code, "CONNECT_FAILED", True) from e
    except httpx.HTTPError as e:
        raise MarketplaceExternalAPIError(marketplace_code, "TRANSPORT_ERROR", True) from e
    except (ValueError, KeyError, TypeError) as e:
        # response.json() 실패(ValueError/JSONDecodeError) 및 예상 응답 구조 누락(KeyError 등).
        raise MarketplaceExternalAPIError(marketplace_code, "PARSE_FAILED", False) from e
