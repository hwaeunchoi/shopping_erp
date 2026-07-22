"""
integrations/ads/base_ad_connector.py
------------------------------------------
광고 플랫폼 커넥터 공통 인터페이스.

Scheduler/Service 계층은 구체 커넥터 클래스가 아니라 이 인터페이스에만
의존한다. AdCampaign.ad_platform_code 값(예: "coupang_ad")으로 실제 구현체를
동적으로 로딩할 때는 integrations.ads.get_ad_connector()를 사용한다.

fetch_campaigns() 반환 항목:
    {"platform_campaign_id": str, "name": str, "is_active": bool}

fetch_daily_performance() 반환 항목 (models.ad.AdPerformanceDaily와 1:1 대응):
    {
        "stat_date": date, "impressions": int, "clicks": int,
        "cost": float, "conversions": int, "conversion_amount": float,
    }

현재는 실제 API 키가 없는 개발 단계이므로 각 구현체는 위 형식에 맞는
더미(가짜) 데이터를 생성한다. 실제 API 연동 시에는 각 구현체 내부의
HTTP 호출 부분만 교체하면 되고, 이 인터페이스와 반환 형식은 그대로 유지된다.
"""

import random
from abc import ABC, abstractmethod
from datetime import date, timedelta
from typing import Any


class BaseAdConnector(ABC):
    """모든 광고 플랫폼 커넥터가 구현해야 하는 공통 인터페이스."""

    ad_platform_code: str  # models.ad.AdCampaign.ad_platform_code와 일치해야 한다.

    @abstractmethod
    def fetch_campaigns(self) -> list[dict[str, Any]]:
        """운영 중인 캠페인 목록을 조회한다."""
        raise NotImplementedError

    @abstractmethod
    def fetch_daily_performance(
        self, platform_campaign_id: str, start_date: date, end_date: date
    ) -> list[dict[str, Any]]:
        """캠페인의 기간별 일별 성과를 조회한다."""
        raise NotImplementedError

    def _dummy_campaigns(self, count: int = 3) -> list[dict[str, Any]]:
        rng = random.Random(f"{self.ad_platform_code}:campaigns")
        return [
            {
                "platform_campaign_id": f"{self.ad_platform_code.upper()}-{rng.randint(1000, 9999)}",
                "name": f"{self.ad_platform_code} 캠페인 {i + 1}",
                "is_active": True,
            }
            for i in range(count)
        ]

    def _dummy_daily_performance(
        self, platform_campaign_id: str, start_date: date, end_date: date
    ) -> list[dict[str, Any]]:
        rng = random.Random(f"{self.ad_platform_code}:{platform_campaign_id}")
        results = []
        cursor = start_date
        while cursor < end_date:
            impressions = rng.randint(300, 2500)
            clicks = rng.randint(int(impressions * 0.01), int(impressions * 0.05))
            cost = round(clicks * rng.uniform(100, 250), 2)
            conversions = rng.randint(0, max(int(clicks * 0.08), 1))
            conversion_amount = round(conversions * rng.uniform(15000, 40000), 2)
            results.append(
                {
                    "stat_date": cursor,
                    "impressions": impressions,
                    "clicks": clicks,
                    "cost": cost,
                    "conversions": conversions,
                    "conversion_amount": conversion_amount,
                }
            )
            cursor += timedelta(days=1)
        return results
