"""쇼핑몰 커넥터 플러그인 모음 (네이버 스마트스토어/쿠팡/ESM/11번가/카카오쇼핑).

Platform.connector_class 값(예: "CoupangConnector")으로 실제 커넥터 클래스를
동적으로 조회할 때는 이 모듈의 get_mall_connector()를 사용한다.
"""

from typing import Any, Optional

from integrations.malls.base_mall_connector import BaseMallConnector  # noqa: F401
from integrations.malls.coupang_connector import CoupangConnector  # noqa: F401
from integrations.malls.elevenst_connector import ElevenstConnector  # noqa: F401
from integrations.malls.errors import MarketplaceCapabilityUnsupportedError
from integrations.malls.esm_connector import EsmConnector  # noqa: F401
from integrations.malls.kakao_shopping_connector import KakaoShoppingConnector  # noqa: F401
from integrations.malls.naver_smartstore_connector import NaverSmartstoreConnector  # noqa: F401

MALL_CONNECTORS: dict[str, type[BaseMallConnector]] = {
    "NaverSmartstoreConnector": NaverSmartstoreConnector,
    "CoupangConnector": CoupangConnector,
    "EsmConnector": EsmConnector,
    "ElevenstConnector": ElevenstConnector,
    "KakaoShoppingConnector": KakaoShoppingConnector,
}

# 런타임에서 실 API 연동이 검증된 채널만 인스턴스화를 허용한다. ESM·카카오·11번가는
# 공식 API 미검증이라 여기 포함하지 않는다 - 등록된 커넥터라도 이 집합에 없으면
# 더미/기본 커넥터로 폴백하지 않고 명시적 미지원 오류를 던진다. 클래스 참조로 관리해
# 문자열 오타에 안전하다.
SUPPORTED_CONNECTORS: frozenset[type[BaseMallConnector]] = frozenset({NaverSmartstoreConnector, CoupangConnector})


def get_mall_connector(
    connector_class: str, session: Any = None, platform_id: Optional[int] = None
) -> BaseMallConnector:
    """Platform.connector_class 문자열로 커넥터 인스턴스를 생성한다.

    실 API 연동이 검증된 채널(SUPPORTED_CONNECTORS)만 생성한다. 미검증 채널·미등록
    커넥터·잘못된 설정은 더미/기본 커넥터로 폴백하지 않고
    MarketplaceCapabilityUnsupportedError를 던진다(운영 경로에 더미 유입 차단).
    """
    cls = MALL_CONNECTORS.get(connector_class)
    if cls is None or cls not in SUPPORTED_CONNECTORS:
        raise MarketplaceCapabilityUnsupportedError(connector_class, "runtime-connector")
    return cls(session=session, platform_id=platform_id)


__all__ = [
    "BaseMallConnector",
    "MALL_CONNECTORS",
    "get_mall_connector",
    "NaverSmartstoreConnector",
    "CoupangConnector",
    "EsmConnector",
    "ElevenstConnector",
    "KakaoShoppingConnector",
]
