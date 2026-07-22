"""
services/ai_assistant_service.py
------------------------------------
UI v1.1 8장 ERP AI Assistant. 설계 방침대로 1차 범위에서는 실제 AI 모델을
호출하지 않고, 기존 집계 테이블(profit_loss_summary/ad_performance_daily/
returns)을 요약해 보여주는 간단한 규칙 기반 응답으로 시작한다("패널 UI는
그대로 두고 백엔드 응답 로직만 교체" 가능하도록 이 서비스 하나만 교체하면
됨).

빠른 질문 3종(이번주 매출 요약/저ROAS 캠페인 찾기/반품 급증 원인)은 키워드
매칭으로 실제 데이터 기반 답변을 만들고, 그 외 자유 질문은 "준비 중" 안내로
대체한다.
"""

from datetime import date, timedelta

from repositories.ad_repository import AdCampaignRepository, AdPerformanceRepository
from repositories.analytics_repository import ProfitLossSummaryRepository
from repositories.order_repository import ReturnRepository

LOW_ROAS_THRESHOLD = 100.0  # ROAS 100% 미만이면 광고비만큼 회수 못 하는 저효율 캠페인으로 간주한다.

FALLBACK_MESSAGE = (
    "아직 자유 질문에 대한 AI 분석은 준비 중입니다. 아래 빠른 질문을 이용해보세요: "
    "이번주 매출 요약 / 저ROAS 캠페인 찾기 / 반품 급증 원인"
)


class AIAssistantService:
    def __init__(self, session) -> None:
        self.summary_repo = ProfitLossSummaryRepository(session)
        self.ad_campaign_repo = AdCampaignRepository(session)
        self.ad_perf_repo = AdPerformanceRepository(session)
        self.return_repo = ReturnRepository(session)

    def answer(self, question: str) -> str:
        q = question.strip()
        if "매출" in q and ("이번주" in q or "이번 주" in q or "요약" in q):
            return self._weekly_revenue_summary()
        if "roas" in q.lower() or "저효율" in q:
            return self._low_roas_campaigns()
        if "반품" in q:
            return self._return_spike_check()
        return FALLBACK_MESSAGE

    def _weekly_revenue_summary(self) -> str:
        summaries = [
            s for s in self.summary_repo.list_by_period("DAILY", "ORDER_DATE") if s.period_key >= _n_days_ago(7)
        ]
        if not summaries:
            return "최근 7일간 집계된 매출 데이터가 없습니다. 손익 계산(profit_loss_summary)을 먼저 실행해주세요."
        revenue = sum(float(s.net_revenue) for s in summaries)
        profit = sum(float(s.net_profit) for s in summaries)
        orders = sum(s.order_count for s in summaries)
        return (
            f"최근 7일 순매출은 {revenue:,.0f}원, 순이익은 {profit:,.0f}원, 주문 {orders}건입니다 "
            f"(집계일수: {len(summaries)}일)."
        )

    def _low_roas_campaigns(self) -> str:
        today = date.today()
        month_start = today.replace(day=1)
        low_roas: list[tuple[str, float]] = []
        for campaign in self.ad_campaign_repo.list_active():
            perf = self.ad_perf_repo.list_by_campaign_and_range(campaign.id, month_start, today + timedelta(days=1))
            cost = sum(float(p.cost) for p in perf)
            conversion_amount = sum(float(p.conversion_amount) for p in perf)
            if cost <= 0:
                continue
            roas = conversion_amount / cost * 100
            if roas < LOW_ROAS_THRESHOLD:
                low_roas.append((campaign.name or campaign.platform_campaign_id, round(roas, 1)))

        if not low_roas:
            return f"이번 달 활성 캠페인 중 ROAS {LOW_ROAS_THRESHOLD:.0f}% 미만인 저효율 캠페인은 없습니다."
        low_roas.sort(key=lambda x: x[1])
        lines = ", ".join(f"{name}(ROAS {roas}%)" for name, roas in low_roas[:5])
        return f"이번 달 ROAS {LOW_ROAS_THRESHOLD:.0f}% 미만 저효율 캠페인: {lines}"

    def _return_spike_check(self) -> str:
        today = date.today()
        this_week_start = today - timedelta(days=7)
        last_week_start = today - timedelta(days=14)

        this_week = self.return_repo.count_filtered(start_date=this_week_start, end_date=today)
        last_week = self.return_repo.count_filtered(start_date=last_week_start, end_date=this_week_start)

        if last_week == 0:
            return f"최근 7일 반품 {this_week}건 (직전 7일에는 반품 이력이 없어 증감률을 계산할 수 없습니다)."
        change_rate = round((this_week - last_week) / last_week * 100, 1)
        trend = "증가" if change_rate > 0 else "감소" if change_rate < 0 else "변동없음"
        return f"최근 7일 반품 {this_week}건, 직전 7일 {last_week}건으로 {abs(change_rate)}% {trend}했습니다."


def _n_days_ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()
