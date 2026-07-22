"""
scheduler/jobs/ad_collect_job.py
------------------------------------
전체 광고 플랫폼의 캠페인/일별 성과를 수집한다.

캠페인은 (ad_platform_code, platform_campaign_id) 기준으로 없으면 생성하고,
일별 성과는 (campaign_id, stat_date) 기준으로 있으면 갱신, 없으면 생성한다
(스키마의 UniqueConstraint와 동일한 키).

UI v1.1 4.1절(연동상태 탭)을 위해 광고 플랫폼별 수집 성공/실패를
integration_status에도 반영한다(integration_type="AD"). SRS FR-MALL-07
(최종 실패 시 화면에 알림 표시)에 따라 실패 시 notifications에도 남긴다.
"""

from datetime import date, timedelta

from core.database import session_scope
from integrations.ads import AD_CONNECTORS, get_ad_connector
from models.ad import AdCampaign, AdPerformanceDaily
from repositories.ad_repository import AdCampaignRepository, AdPerformanceRepository
from repositories.extra_repository import IntegrationStatusRepository
from services.notification_service import NotificationService

PERFORMANCE_WINDOW_DAYS = 3


def run() -> dict[str, dict]:
    results: dict[str, dict] = {}
    with session_scope() as db:
        campaign_repo = AdCampaignRepository(db)
        perf_repo = AdPerformanceRepository(db)
        integration_status_repo = IntegrationStatusRepository(db)
        notification_service = NotificationService(db)
        end_date = date.today()
        start_date = end_date - timedelta(days=PERFORMANCE_WINDOW_DAYS)

        for ad_platform_code in AD_CONNECTORS:
            connector = get_ad_connector(ad_platform_code)
            new_campaigns = new_perf = updated_perf = 0

            try:
                for raw_campaign in connector.fetch_campaigns():
                    campaign = campaign_repo.get_by_platform_campaign_id(
                        ad_platform_code, raw_campaign["platform_campaign_id"]
                    )
                    if campaign is None:
                        campaign = campaign_repo.add(
                            AdCampaign(
                                ad_platform_code=ad_platform_code,
                                platform_campaign_id=raw_campaign["platform_campaign_id"],
                                name=raw_campaign.get("name"),
                                is_active=raw_campaign.get("is_active", True),
                            )
                        )
                        new_campaigns += 1

                    performances = connector.fetch_daily_performance(
                        raw_campaign["platform_campaign_id"], start_date, end_date
                    )
                    for perf in performances:
                        existing = perf_repo.get_by_campaign_and_date(campaign.id, perf["stat_date"])
                        if existing:
                            existing.impressions = perf["impressions"]
                            existing.clicks = perf["clicks"]
                            existing.cost = perf["cost"]
                            existing.conversions = perf["conversions"]
                            existing.conversion_amount = perf["conversion_amount"]
                            updated_perf += 1
                        else:
                            perf_repo.add(AdPerformanceDaily(campaign_id=campaign.id, **perf))
                            new_perf += 1

                results[ad_platform_code] = {
                    "new_campaigns": new_campaigns,
                    "new_performance_rows": new_perf,
                    "updated_performance_rows": updated_perf,
                }
                integration_status_repo.upsert_success("AD", ad_platform_code)
                db.commit()
            except Exception as e:
                integration_status_repo.upsert_error("AD", ad_platform_code, str(e))
                notification_service.notify(
                    type_="API_FAILURE",
                    severity="CRITICAL",
                    message=f"광고 수집 실패: ad_platform={ad_platform_code}, 오류={e}",
                )
                db.commit()
                raise
    return results
