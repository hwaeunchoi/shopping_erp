"""
tests/unit/test_ai_assistant_service.py
------------------------------------------------
AIAssistantService(ERP AI Assistant) 단위 테스트. UI 와이어프레임 v1.1 8장
대응 - 빠른 질문 3종은 실데이터 기반, 그 외는 "준비 중" 안내.
"""

from datetime import date, datetime, timedelta, timezone

from models.ad import AdCampaign, AdPerformanceDaily
from models.analytics import ProfitLossSummary
from models.order import Order, Return
from services.ai_assistant_service import FALLBACK_MESSAGE, AIAssistantService


class TestAnswer:
    def test_unrecognized_question_returns_fallback(self, db_session):
        answer = AIAssistantService(db_session).answer("오늘 날씨 어때?")

        assert answer == FALLBACK_MESSAGE

    def test_weekly_revenue_summary_with_no_data(self, db_session):
        answer = AIAssistantService(db_session).answer("이번주 매출 요약해줘")

        assert "집계된 매출 데이터가 없습니다" in answer

    def test_weekly_revenue_summary_with_data(self, db_session):
        today = date.today()
        db_session.add(
            ProfitLossSummary(
                period_type="DAILY",
                basis_type="ORDER_DATE",
                period_key=today.isoformat(),
                gross_revenue=10000,
                net_revenue=10000,
                order_count=2,
                net_profit=3000,
                generated_at=datetime.now(timezone.utc),
            )
        )
        db_session.flush()

        answer = AIAssistantService(db_session).answer("이번주 매출 요약")

        assert "10,000" in answer
        assert "3,000" in answer

    def test_low_roas_campaigns_found(self, db_session, product_option):
        campaign = AdCampaign(
            ad_platform_code="naver_search_ad",
            platform_campaign_id="AI-CAMPAIGN-1",
            name="저효율캠페인",
            product_option_id=product_option.id,
            is_active=True,
        )
        db_session.add(campaign)
        db_session.flush()
        db_session.add(
            AdPerformanceDaily(
                campaign_id=campaign.id,
                stat_date=date.today(),
                cost=10000,
                conversion_amount=2000,
                impressions=100,
                clicks=10,
            )
        )
        db_session.flush()

        answer = AIAssistantService(db_session).answer("저ROAS 캠페인 찾아줘")

        assert "저효율캠페인" in answer

    def test_low_roas_campaigns_none_found(self, db_session):
        answer = AIAssistantService(db_session).answer("저ROAS 캠페인 찾기")

        assert "저효율 캠페인은 없습니다" in answer

    def test_return_spike_check_no_history(self, db_session):
        answer = AIAssistantService(db_session).answer("반품 급증 원인이 뭐야")

        assert "반품 0건" in answer or "반품 이력이 없어" in answer

    def test_return_spike_check_with_increase(self, db_session, platform, customer):
        order = Order(
            platform_id=platform.id,
            platform_order_no="AI-RETURN-ORDER",
            customer_id=customer.id,
            status="RETURNED",
            order_date=datetime.now(timezone.utc),
            total_amount=10000,
            discount_amount=0,
        )
        db_session.add(order)
        db_session.flush()
        db_session.add(
            Return(order_id=order.id, status="REQUESTED", reason="단순변심", requested_at=datetime.now(timezone.utc))
        )
        db_session.add(
            Return(
                order_id=order.id,
                status="REQUESTED",
                reason="단순변심",
                requested_at=datetime.now(timezone.utc) - timedelta(days=14),
            )
        )
        db_session.flush()

        answer = AIAssistantService(db_session).answer("반품 급증 원인")

        assert "반품" in answer
