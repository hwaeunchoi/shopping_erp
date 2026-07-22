"""
tests/integration/test_api_ads.py
----------------------------------------
api/routers/ads.py 통합 테스트. SRS FR-AD-03(CPC/CPM/CTR/ROAS 조회 시 계산) 검증.
"""

from datetime import date

from models.ad import AdCampaign, AdPerformanceDaily


class TestCampaignPerformance:
    def test_returns_calculated_cpc_cpm_ctr_roas(self, client, auth_headers, api_session_factory):
        db = api_session_factory()
        try:
            campaign = AdCampaign(ad_platform_code="naver_search_ad", platform_campaign_id="CMP-1", is_active=True)
            db.add(campaign)
            db.flush()
            db.add(
                AdPerformanceDaily(
                    campaign_id=campaign.id,
                    stat_date=date(2026, 1, 1),
                    impressions=1000,
                    clicks=50,
                    cost=10000,
                    conversions=5,
                    conversion_amount=50000,
                )
            )
            db.commit()
            campaign_id = campaign.id
        finally:
            db.close()

        resp = client.get(
            f"/api/ad-campaigns/{campaign_id}/performance?start_date=2026-01-01&end_date=2026-01-02",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        row = body[0]
        assert row["cpc"] == 200.0  # 10000 / 50
        assert row["cpm"] == 10000.0  # 10000 / 1000 * 1000
        assert row["ctr"] == 5.0  # 50 / 1000 * 100
        assert row["roas"] == 500.0  # 50000 / 10000 * 100

    def test_zero_clicks_and_cost_avoid_division_by_zero(self, client, auth_headers, api_session_factory):
        db = api_session_factory()
        try:
            campaign = AdCampaign(ad_platform_code="naver_search_ad", platform_campaign_id="CMP-2", is_active=True)
            db.add(campaign)
            db.flush()
            db.add(
                AdPerformanceDaily(
                    campaign_id=campaign.id,
                    stat_date=date(2026, 1, 1),
                    impressions=0,
                    clicks=0,
                    cost=0,
                    conversions=0,
                    conversion_amount=0,
                )
            )
            db.commit()
            campaign_id = campaign.id
        finally:
            db.close()

        resp = client.get(
            f"/api/ad-campaigns/{campaign_id}/performance?start_date=2026-01-01&end_date=2026-01-02",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        row = resp.json()[0]
        assert row["cpc"] == 0.0
        assert row["cpm"] == 0.0
        assert row["ctr"] == 0.0
        assert row["roas"] == 0.0
