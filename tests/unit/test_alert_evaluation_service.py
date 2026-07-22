"""
tests/unit/test_alert_evaluation_service.py
------------------------------------------------------
AlertEvaluationService 단위 테스트. UI v1.0 알림센터의 규칙 판정 로직.
"""

from datetime import datetime, timezone

from models.ad import AdCampaign, AdPerformanceDaily
from models.extra import IntegrationStatus
from models.order import Order, Return
from models.system import AlertRule, BackupHistory, Notification
from services.alert_evaluation_service import AlertEvaluationService


def _make_rule(db_session, metric, operator="GT", threshold=0.0, is_enabled=True, name=None):
    rule = AlertRule(
        name=name or f"{metric}-rule",
        metric=metric,
        operator=operator,
        threshold_value=threshold,
        check_frequency="HOURLY",
        is_enabled=is_enabled,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(rule)
    db_session.flush()
    return rule


def _make_order(db_session, platform, customer, status="NEW", order_no="ALERT-ORDER"):
    order = Order(
        platform_id=platform.id,
        platform_order_no=order_no,
        customer_id=customer.id,
        status=status,
        order_date=datetime.now(timezone.utc),
        total_amount=10000,
        discount_amount=0,
    )
    db_session.add(order)
    db_session.flush()
    return order


class TestEvaluateAll:
    def test_disabled_rule_is_skipped(self, db_session):
        _make_rule(db_session, metric="ORDER_COUNT_TODAY", operator="GTE", threshold=0.0, is_enabled=False)

        created = AlertEvaluationService(db_session).evaluate_all()

        assert created == []

    def test_order_count_today_triggers_notification(self, db_session, platform, customer):
        _make_order(db_session, platform, customer)
        rule = _make_rule(db_session, metric="ORDER_COUNT_TODAY", operator="GTE", threshold=1.0)

        created = AlertEvaluationService(db_session).evaluate_all()

        assert len(created) == 1
        notification = db_session.get(Notification, created[0])
        assert notification.rule_id == rule.id
        assert notification.severity == "WARNING"

    def test_does_not_duplicate_when_unread_notification_exists(self, db_session, platform, customer):
        _make_order(db_session, platform, customer)
        _make_rule(db_session, metric="ORDER_COUNT_TODAY", operator="GTE", threshold=1.0)
        service = AlertEvaluationService(db_session)

        first = service.evaluate_all()
        second = service.evaluate_all()

        assert len(first) == 1
        assert second == []

    def test_unshipped_days_metric(self, db_session, platform, customer):
        _make_order(db_session, platform, customer, status="NEW", order_no="UNSHIPPED-1")
        _make_order(db_session, platform, customer, status="PREPARING", order_no="UNSHIPPED-2")
        _make_order(db_session, platform, customer, status="DELIVERED", order_no="SHIPPED-1")
        _make_rule(db_session, metric="UNSHIPPED_DAYS", operator="GTE", threshold=2.0)

        created = AlertEvaluationService(db_session).evaluate_all()

        assert len(created) == 1

    def test_return_rate_metric(self, db_session, platform, customer):
        order = _make_order(db_session, platform, customer, status="RETURNED", order_no="RETURN-1")
        db_session.add(
            Return(order_id=order.id, status="REQUESTED", reason="단순변심", requested_at=datetime.now(timezone.utc))
        )
        _make_rule(db_session, metric="RETURN_RATE", operator="GT", threshold=50.0)

        created = AlertEvaluationService(db_session).evaluate_all()

        assert len(created) == 1  # 반품 1건 / 주문 1건 = 100% > 50%

    def test_api_failure_metric(self, db_session):
        db_session.add(
            IntegrationStatus(
                integration_type="MALL",
                integration_code="coupang",
                status="ERROR",
                updated_at=datetime.now(timezone.utc),
            )
        )
        _make_rule(db_session, metric="API_FAILURE", operator="GTE", threshold=1.0)

        created = AlertEvaluationService(db_session).evaluate_all()

        assert len(created) == 1
        notification = db_session.get(Notification, created[0])
        assert notification.severity == "CRITICAL"

    def test_backup_failure_metric(self, db_session):
        db_session.add(
            BackupHistory(
                file_path="/tmp/x.db", status="FAILED", error_message="disk full", created_at=datetime.now(timezone.utc)
            )
        )
        _make_rule(db_session, metric="BACKUP_FAILURE", operator="EQ", threshold=1.0)

        created = AlertEvaluationService(db_session).evaluate_all()

        assert len(created) == 1

    def test_ad_cost_and_roas_metrics(self, db_session, product_option):
        campaign = AdCampaign(
            ad_platform_code="naver_search_ad",
            platform_campaign_id="ALERT-CAMPAIGN",
            product_option_id=product_option.id,
            is_active=True,
        )
        db_session.add(campaign)
        db_session.flush()
        db_session.add(
            AdPerformanceDaily(
                campaign_id=campaign.id,
                stat_date=datetime.now(timezone.utc).date(),
                cost=10000,
                conversion_amount=5000,
                impressions=100,
                clicks=10,
            )
        )
        _make_rule(db_session, metric="AD_COST", operator="GTE", threshold=5000.0, name="ad-cost-rule")
        _make_rule(db_session, metric="ROAS", operator="LT", threshold=100.0, name="roas-rule")

        created = AlertEvaluationService(db_session).evaluate_all()

        assert len(created) == 2  # 광고비 10000 >= 5000, ROAS 50% < 100%

    def test_rule_below_threshold_does_not_trigger(self, db_session, platform, customer):
        _make_rule(db_session, metric="ORDER_COUNT_TODAY", operator="GTE", threshold=999.0)

        created = AlertEvaluationService(db_session).evaluate_all()

        assert created == []
