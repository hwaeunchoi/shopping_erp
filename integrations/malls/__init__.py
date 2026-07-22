"""쇼핑몰 커넥터 플러그인 모음 (네이버 스마트스토어/쿠팡/ESM/11번가/카카오쇼핑).

Platform.connector_class 값(예: "CoupangConnector")으로 실제 커넥터 클래스를
동적으로 조회할 때는 이 모듈의 get_mall_connector()를 사용한다.
"""

from typing import Any, Optional

from integrations.malls.base_mall_connector import BaseMallConnector  # noqa: F401
from integrations.malls.coupang_connector import CoupangConnector  # noqa: F401
from integrations.malls.elevenst_connector import ElevenstConnector  # noqa: F401
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


def get_mall_connector(
    connector_class: str, session: Any = None, platform_id: Optional[int] = None
) -> BaseMallConnector:
    """Platform.connector_class 문자열로 커넥터 인스턴스를 생성한다.

    session/platform_id를 넘기면 실제 API 연동을 지원하는 커넥터(예: 네이버)가
    api_credentials에서 실제 키를 찾아 사용을 시도한다(없으면 더미로 폴백).
    """
    cls = MALL_CONNECTORS.get(connector_class)
    if cls is None:
        raise ValueError(f"등록되지 않은 쇼핑몰 커넥터입니다: {connector_class}")
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
