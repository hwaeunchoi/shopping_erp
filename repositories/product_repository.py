"""
repositories/product_repository.py
--------------------------------------
ERD 2.4 상품/공급처/재고 그룹 중 상품 관련(products, product_options,
product_platform_map, product_cost_history)에 대한 Repository.
"""

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from models.base import utcnow
from models.product import (
    Product,
    ProductCostHistory,
    ProductImage,
    ProductOption,
    ProductPlatformMap,
    UnmatchedPlatformItem,
)
from repositories.base_repository import BaseRepository


class ProductRepository(BaseRepository[Product]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Product)

    def list_active(self) -> list[Product]:
        stmt = select(Product).where(Product.status == "ACTIVE", Product.is_deleted.is_(False))
        return list(self.session.execute(stmt).scalars().all())

    def list_by_category(self, category: str) -> list[Product]:
        stmt = select(Product).where(Product.category == category, Product.is_deleted.is_(False))
        return list(self.session.execute(stmt).scalars().all())

    def list_all_including_deleted(self, category: Optional[str] = None) -> list[Product]:
        """복원(restore) 화면에서 소프트 삭제된 상품도 볼 수 있도록 is_deleted 필터 없이
        전체(상태 무관)를 조회한다."""
        stmt = select(Product)
        if category is not None:
            stmt = stmt.where(Product.category == category)
        return list(self.session.execute(stmt).scalars().all())

    def _filtered_stmt(
        self, category: Optional[str] = None, status: Optional[str] = None, search: Optional[str] = None
    ) -> Select[Any]:
        stmt = select(Product).where(Product.is_deleted.is_(False))
        if category is not None:
            stmt = stmt.where(Product.category == category)
        if status is not None:
            stmt = stmt.where(Product.status == status)
        if search:
            stmt = stmt.where(Product.name.ilike(f"%{search}%"))
        return stmt

    def list_filtered(
        self,
        category: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Product]:
        stmt = self._filtered_stmt(category, status, search).order_by(Product.id.desc()).limit(limit).offset(offset)
        return list(self.session.execute(stmt).scalars().all())

    def count_filtered(
        self, category: Optional[str] = None, status: Optional[str] = None, search: Optional[str] = None
    ) -> int:
        stmt = select(func.count()).select_from(self._filtered_stmt(category, status, search).subquery())
        return self.session.execute(stmt).scalar_one()

    def search(self, keyword: str, limit: int = 5) -> list[Product]:
        """SRS UI v1.1 통합검색: 상품명으로 검색한다."""
        stmt = (
            select(Product)
            .where(Product.is_deleted.is_(False), Product.name.ilike(f"%{keyword}%"))
            .order_by(Product.id.desc())
            .limit(limit)
        )
        return list(self.session.execute(stmt).scalars().all())


class ProductOptionRepository(BaseRepository[ProductOption]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, ProductOption)

    def get_by_sku_code(self, sku_code: str) -> Optional[ProductOption]:
        stmt = select(ProductOption).where(ProductOption.sku_code == sku_code)
        return self.session.execute(stmt).scalar_one_or_none()

    def list_by_product(self, product_id: int) -> list[ProductOption]:
        stmt = (
            select(ProductOption)
            .where(ProductOption.product_id == product_id)
            .order_by(ProductOption.sort_order, ProductOption.id)
        )
        return list(self.session.execute(stmt).scalars().all())

    def search(self, keyword: str, limit: int = 5) -> list[ProductOption]:
        """SRS UI v1.1 통합검색: SKU 코드로 검색한다."""
        stmt = select(ProductOption).where(ProductOption.sku_code.ilike(f"%{keyword}%")).limit(limit)
        return list(self.session.execute(stmt).scalars().all())

    def reorder(self, product_id: int, ordered_option_ids: list[int]) -> None:
        """전달받은 순서대로 sort_order(0부터)를 다시 매긴다."""
        options_by_id = {o.id: o for o in self.list_by_product(product_id)}
        for index, option_id in enumerate(ordered_option_ids):
            option = options_by_id.get(option_id)
            if option is not None:
                option.sort_order = index


class ProductPlatformMapRepository(BaseRepository[ProductPlatformMap]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, ProductPlatformMap)

    def get_by_option_id(self, platform_id: int, platform_option_id: str) -> Optional[ProductPlatformMap]:
        """(platform_id, platform_option_id) 정확히 일치하는 매핑을 찾는다 - 자동매칭
        1순위(옵션 단위 유니크 키) 및 이미 매핑된 주문상품의 재확인에 사용."""
        stmt = select(ProductPlatformMap).where(
            ProductPlatformMap.platform_id == platform_id, ProductPlatformMap.platform_option_id == platform_option_id
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def list_by_platform_and_origin_product_id(
        self, platform_id: int, platform_origin_product_id: str
    ) -> list[ProductPlatformMap]:
        """(platform_id, platform_origin_product_id)를 공유하는 매핑을 전부 찾는다 -
        platform_option_id와 달리 platform_origin_product_id에는 유니크 제약이 없어
        (네이버 원상품 하나가 스마트스토어/윈도우 등 복수 채널상품을 가질 수 있어
        의도적으로 허용) 서로 다른 ProductPlatformMap 행이 같은 원상품을 가리킬 수
        있다 - 상용 ERP 확장(3단계)의 재고/판매상태 전송 동시성 제어가 이 경우를
        같은 외부 대상으로 묶어 처리하기 위해 조회한다(services.
        product_sync_dispatch_service._resolve_contention_target_ids 참고)."""
        stmt = select(ProductPlatformMap).where(
            ProductPlatformMap.platform_id == platform_id,
            ProductPlatformMap.platform_origin_product_id == platform_origin_product_id,
        )
        return list(self.session.execute(stmt).scalars().all())

    def list_by_option(self, product_option_id: int) -> list[ProductPlatformMap]:
        stmt = select(ProductPlatformMap).where(ProductPlatformMap.product_option_id == product_option_id)
        return list(self.session.execute(stmt).scalars().all())

    def get_by_seller_product_code(self, seller_product_code: str) -> Optional[ProductPlatformMap]:
        """플랫폼과 무관하게 동일한 판매자상품코드를 가진 기존 매핑 1건을 찾는다
        (자동매칭 3순위, 유일하게 플랫폼 범위를 넘는 단계).

        쿠팡/카카오/11번가/ESM은 네이버 상품 동기화로 이미 등록된 물리적 상품을
        그대로 노출한 것이므로, 새 상품을 만드는 대신 판매자상품코드로 기존
        네이버 등록분을 찾아 그 상품(옵션)에 새 플랫폼 매핑만 추가한다.
        """
        stmt = select(ProductPlatformMap).where(ProductPlatformMap.seller_product_code == seller_product_code)
        return self.session.execute(stmt).scalars().first()

    def list_by_product_id(self, platform_id: int, platform_product_id: str) -> list[ProductPlatformMap]:
        """(platform_id, platform_product_id) - 상품(그룹) 단위 식별자로 걸리는 모든 매핑을
        찾는다(자동매칭 2순위). platform_product_id는 비유니크라 여러 옵션이 걸릴 수
        있으므로, 호출부에서 결과가 정확히 1건일 때만 채택해야 한다(모호하면 다음
        단계로 넘어감)."""
        stmt = select(ProductPlatformMap).where(
            ProductPlatformMap.platform_id == platform_id, ProductPlatformMap.platform_product_id == platform_product_id
        )
        return list(self.session.execute(stmt).scalars().all())


class ProductImageRepository(BaseRepository[ProductImage]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, ProductImage)

    def list_by_product(self, product_id: int) -> list[ProductImage]:
        stmt = (
            select(ProductImage)
            .where(ProductImage.product_id == product_id)
            .order_by(ProductImage.sort_order, ProductImage.id)
        )
        return list(self.session.execute(stmt).scalars().all())


class ProductCostHistoryRepository(BaseRepository[ProductCostHistory]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, ProductCostHistory)

    def list_by_option(self, product_option_id: int) -> list[ProductCostHistory]:
        stmt = (
            select(ProductCostHistory)
            .where(ProductCostHistory.product_option_id == product_option_id)
            .order_by(ProductCostHistory.effective_from.desc())
        )
        return list(self.session.execute(stmt).scalars().all())

    def get_open(self, product_option_id: int) -> Optional[ProductCostHistory]:
        """effective_to가 아직 닫히지 않은(무기한 적용 중인) 최신 원가 레코드를 찾는다."""
        stmt = (
            select(ProductCostHistory)
            .where(ProductCostHistory.product_option_id == product_option_id, ProductCostHistory.effective_to.is_(None))
            .order_by(ProductCostHistory.effective_from.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalars().first()

    def get_effective_cost(self, product_option_id: int, on_date: datetime) -> Optional[ProductCostHistory]:
        """on_date 시점에 적용 가능한 원가 레코드를 찾는다(플랫폼 수수료율과 동일한 선택 규칙).

        effective_from <= on_date이고 (effective_to가 NULL이거나 on_date 이상)인
        레코드 중 effective_from이 가장 최근인 것을 우선한다.
        """
        stmt = (
            select(ProductCostHistory)
            .where(
                ProductCostHistory.product_option_id == product_option_id,
                ProductCostHistory.effective_from <= on_date,
                or_(ProductCostHistory.effective_to.is_(None), ProductCostHistory.effective_to >= on_date),
            )
            .order_by(ProductCostHistory.effective_from.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalars().first()


class UnmatchedPlatformItemRepository(BaseRepository[UnmatchedPlatformItem]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, UnmatchedPlatformItem)

    def list_pending(self) -> list[UnmatchedPlatformItem]:
        stmt = (
            select(UnmatchedPlatformItem)
            .where(UnmatchedPlatformItem.status == "PENDING")
            .order_by(UnmatchedPlatformItem.created_at.desc())
        )
        return list(self.session.execute(stmt).scalars().all())

    def mark_matched(
        self, item: UnmatchedPlatformItem, product_option_id: int, matched_by: str
    ) -> UnmatchedPlatformItem:
        item.status = "MATCHED"
        item.matched_option_id = product_option_id
        item.matched_by = matched_by
        item.matched_at = utcnow()
        return item
