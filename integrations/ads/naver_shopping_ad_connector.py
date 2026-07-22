"""
integrations/ads/naver_shopping_ad_connector.py
-----------------------------------------------------
네이버 쇼핑검색광고 더미 커넥터 (AdCampaign.ad_platform_code == "naver_shopping_ad"와 매핑).
"""

from datetime import date
from typing import Any

from integrations.ads.base_ad_connector import BaseAdConnector


class NaverShoppingAdConnector(BaseAdConnector):
    ad_platform_code = "naver_shopping_ad"

    def fetch_campaigns(self) -> list[dict[str, Any]]:
        return self._dummy_campaigns()

    def fetch_daily_performance(
        self, platform_campaign_id: str, start_date: date, end_date: date
    ) -> list[dict[str, Any]]:
        return self._dummy_daily_performance(platform_campaign_id, start_date, end_date)
