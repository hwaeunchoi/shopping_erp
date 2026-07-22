"""광고 플랫폼 커넥터 플러그인 모음 (네이버 검색광고/쇼핑검색광고/쿠팡광고).

AdCampaign.ad_platform_code 값(예: "coupang_ad")으로 실제 커넥터 클래스를
동적으로 조회할 때는 이 모듈의 get_ad_connector()를 사용한다.
"""

from integrations.ads.base_ad_connector import BaseAdConnector  # noqa: F401
from integrations.ads.coupang_ad_connector import CoupangAdConnector  # noqa: F401
from integrations.ads.naver_search_ad_connector import NaverSearchAdConnector  # noqa: F401
from integrations.ads.naver_shopping_ad_connector import NaverShoppingAdConnector  # noqa: F401

AD_CONNECTORS: dict[str, type[BaseAdConnector]] = {
    "naver_search_ad": NaverSearchAdConnector,
    "naver_shopping_ad": NaverShoppingAdConnector,
    "coupang_ad": CoupangAdConnector,
}


def get_ad_connector(ad_platform_code: str) -> BaseAdConnector:
    """AdCampaign.ad_platform_code 문자열로 커넥터 인스턴스를 생성한다."""
    cls = AD_CONNECTORS.get(ad_platform_code)
    if cls is None:
        raise ValueError(f"등록되지 않은 광고 플랫폼 커넥터입니다: {ad_platform_code}")
    return cls()


__all__ = [
    "BaseAdConnector",
    "AD_CONNECTORS",
    "get_ad_connector",
    "NaverSearchAdConnector",
    "NaverShoppingAdConnector",
    "CoupangAdConnector",
]
