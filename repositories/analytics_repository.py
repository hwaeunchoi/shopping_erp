"""
repositories/analytics_repository.py
----------------------------------------
ERD 2.9 매출/손익/KPI 그룹(profit_loss_summary, product_performance_summary,
kpi_targets)에 대한 Repository. 세 테이블 모두 배치가 채우는 집계 테이블이므로
조회 위주의 메서드만 제공한다.
"""

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.analytics import KpiTarget, ProductPerformanceSummary, ProfitLossSummary
from repositories.base_repository import BaseRepository


class ProfitLossSummaryRepository(BaseRepository[ProfitLossSummary]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, ProfitLossSummary)

    def list_by_period(
        self, period_type: str, basis_type: str, platform_id: Optional[int] = None
    ) -> list[ProfitLossSummary]:
        """SRS FR-PROFIT-02: 플랫폼 단위 세분화 조회. platform_id를 지정하지 않으면 "전체" 집계만,
        지정하면 해당 플랫폼의 집계만 반환한다(둘은 서로 다른 행이라 섞이지 않는다)."""
        stmt = select(ProfitLossSummary).where(
            ProfitLossSummary.period_type == period_type, ProfitLossSummary.basis_type == basis_type
        )
        if platform_id is None:
            stmt = stmt.where(ProfitLossSummary.platform_id.is_(None))
        else:
            stmt = stmt.where(ProfitLossSummary.platform_id == platform_id)
        return list(self.session.execute(stmt).scalars().all())

    def get_by_key(
        self,
        period_type: str,
        basis_type: str,
        period_key: str,
        platform_id: Optional[int],
        product_option_id: Optional[int],
    ) -> Optional[ProfitLossSummary]:
        """UniqueConstraint(period_type, basis_type, period_key, platform_id,
        product_option_id) 기준으로 기존 집계 행을 조회한다 (계산엔진의 upsert용)."""
        stmt = select(ProfitLossSummary).where(
            ProfitLossSummary.period_type == period_type,
            ProfitLossSummary.basis_type == basis_type,
            ProfitLossSummary.period_key == period_key,
            (
                ProfitLossSummary.platform_id.is_(platform_id)
                if platform_id is None
                else ProfitLossSummary.platform_id == platform_id
            ),
            (
                ProfitLossSummary.product_option_id.is_(product_option_id)
                if product_option_id is None
                else ProfitLossSummary.product_option_id == product_option_id
            ),
        )
        return self.session.execute(stmt).scalar_one_or_none()


class ProductPerformanceSummaryRepository(BaseRepository[ProductPerformanceSummary]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, ProductPerformanceSummary)

    def list_by_period(self, period_type: str, period_key: str) -> list[ProductPerformanceSummary]:
        stmt = select(ProductPerformanceSummary).where(
            ProductPerformanceSummary.period_type == period_type, ProductPerformanceSummary.period_key == period_key
        )
        return list(self.session.execute(stmt).scalars().all())

    def get_by_key(
        self, period_type: str, period_key: str, product_option_id: int
    ) -> Optional[ProductPerformanceSummary]:
        """UniqueConstraint(period_type, period_key, product_option_id) 기준 조회(계산엔진의 upsert용)."""
        stmt = select(ProductPerformanceSummary).where(
            ProductPerformanceSummary.period_type == period_type,
            ProductPerformanceSummary.period_key == period_key,
            ProductPerformanceSummary.product_option_id == product_option_id,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def list_ranked(
        self, period_type: str, period_key: str, order: str = "desc", limit: int = 10
    ) -> list[ProductPerformanceSummary]:
        """SRS FR-PRD-04: 순이익 기준 베스트(order=desc)/워스트(order=asc) 상품 순위."""
        stmt = select(ProductPerformanceSummary).where(
            ProductPerformanceSummary.period_type == period_type, ProductPerformanceSummary.period_key == period_key
        )
        column = ProductPerformanceSummary.net_profit
        stmt = stmt.order_by(column.asc() if order == "asc" else column.desc()).limit(limit)
        return list(self.session.execute(stmt).scalars().all())


class KpiTargetRepository(BaseRepository[KpiTarget]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, KpiTarget)

    def get_by_period_and_metric(self, period_key: str, metric: str) -> Optional[KpiTarget]:
        stmt = select(KpiTarget).where(KpiTarget.period_key == period_key, KpiTarget.metric == metric)
        return self.session.execute(stmt).scalar_one_or_none()
