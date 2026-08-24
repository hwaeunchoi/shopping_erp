"""
services/product_service.py
--------------------------------
상품/옵션(SKU)/플랫폼매핑 CRUD + 원가 이력 관리 업무로직. SRS FR-PRD-01/02/03 대응.

ProductCostHistory는 effective_from~effective_to로 시점별 원가를 추적하는
이력 테이블이다(models/product.py 설계 원칙). ProductCostService.add_cost()는
새 원가를 등록할 때 기존에 열려 있던(effective_to가 NULL인) 레코드를 새
레코드의 effective_from으로 닫아, 항상 한 시점에 유효한 원가가 하나만
존재하도록 유지한다.
"""

from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from models.product import (
    Product,
    ProductCostHistory,
    ProductImage,
    ProductOption,
    ProductPlatformMap,
    UnmatchedPlatformItem,
)
from repositories.order_repository import OrderRepository
from repositories.product_repository import (
    ProductCostHistoryRepository,
    ProductImageRepository,
    ProductOptionRepository,
    ProductPlatformMapRepository,
    ProductRepository,
    UnmatchedPlatformItemRepository,
)


class OptionInUseError(Exception):
    """옵션이 실주문(order_items)에 이미 참조되고 있어 삭제할 수 없을 때 발생한다."""


class ProductService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.product_repo = ProductRepository(session)
        self.option_repo = ProductOptionRepository(session)
        self.platform_map_repo = ProductPlatformMapRepository(session)
        self.image_repo = ProductImageRepository(session)
        self.unmatched_repo = UnmatchedPlatformItemRepository(session)
        self.order_repo = OrderRepository(session)

    def create_product(
        self,
        name: str,
        category: Optional[str],
        base_price: Optional[float],
        status: str = "ACTIVE",
        brand: Optional[str] = None,
        manufacturer: Optional[str] = None,
    ) -> Product:
        product = Product(
            name=name, category=category, base_price=base_price, status=status, brand=brand, manufacturer=manufacturer
        )
        return self.product_repo.add(product)

    def update_product(
        self,
        product: Product,
        name: Optional[str] = None,
        category: Optional[str] = None,
        base_price: Optional[float] = None,
        status: Optional[str] = None,
        brand: Optional[str] = None,
        manufacturer: Optional[str] = None,
    ) -> Product:
        if name is not None:
            product.name = name
        if category is not None:
            product.category = category
        if base_price is not None:
            product.base_price = base_price
        if status is not None:
            product.status = status
        if brand is not None:
            product.brand = brand
        if manufacturer is not None:
            product.manufacturer = manufacturer
        self.session.flush()
        return product

    def restore_product(self, product: Product) -> Product:
        """소프트 삭제된 상품을 되돌린다(products.is_deleted=False). 삭제 시
        함께 비활성화된 옵션은 사용자가 필요에 따라 개별적으로 다시 활성화하도록
        두고, 여기서는 자동으로 되돌리지 않는다(어떤 옵션을 다시 팔지는 사용자
        판단 영역)."""
        product.is_deleted = False
        self.session.flush()
        return product

    def duplicate_product(self, product: Product) -> Product:
        """상품과 옵션(+이미지)을 복제해 새 상품으로 만든다. 플랫폼 매핑은 특정
        외부 채널의 상품코드에 종속된 정보라 복제하지 않는다 - 복제된 상품은
        아직 어떤 플랫폼에도 연결되지 않은 새 상품으로 취급하고, 필요하면
        사용자가 플랫폼 매핑을 새로 등록한다."""
        new_product = self.product_repo.add(
            Product(
                name=f"{product.name} (복사본)",
                category=product.category,
                brand=product.brand,
                manufacturer=product.manufacturer,
                base_price=product.base_price,
                status=product.status,
            )
        )
        for option in self.option_repo.list_by_product(product.id):
            self.option_repo.add(
                ProductOption(
                    product_id=new_product.id,
                    option_name=option.option_name,
                    color=option.color,
                    size=option.size,
                    sku_code=self._generate_unique_sku_code(option.sku_code),
                    barcode=None,  # 바코드는 실물마다 고유하므로 복제하지 않는다.
                    unit_cost_price=option.unit_cost_price,
                    sale_price=option.sale_price,
                    is_active=option.is_active,
                    sort_order=option.sort_order,
                )
            )
        # 상품 전체(대표/추가) 이미지만 복제한다 - 옵션이미지는 새로 생성된 옵션의
        # id로 다시 연결해야 하는데 그 대응 관계까지 복제하는 것은 과도한 추측이라
        # 하지 않는다(필요하면 사용자가 새 상품에서 옵션이미지를 직접 등록한다).
        for image in product.images:
            if image.product_option_id is not None:
                continue
            self.session.add(
                ProductImage(
                    product_id=new_product.id,
                    image_url=image.image_url,
                    is_thumbnail=image.is_thumbnail,
                    sort_order=image.sort_order,
                )
            )
        self.session.flush()
        return new_product

    def _generate_unique_sku_code(self, base_sku_code: str) -> str:
        candidate = f"{base_sku_code}-COPY"[:50]
        suffix = 2
        while self.option_repo.get_by_sku_code(candidate) is not None:
            candidate = f"{base_sku_code}-COPY{suffix}"[:50]
            suffix += 1
        return candidate

    def create_option(
        self,
        product_id: int,
        sku_code: str,
        option_name: Optional[str] = None,
        color: Optional[str] = None,
        size: Optional[str] = None,
        barcode: Optional[str] = None,
        unit_cost_price: Optional[float] = None,
        sale_price: Optional[float] = None,
    ) -> ProductOption:
        existing_options = self.option_repo.list_by_product(product_id)
        next_sort_order = max((o.sort_order for o in existing_options), default=-1) + 1
        option = ProductOption(
            product_id=product_id,
            sku_code=sku_code,
            option_name=option_name,
            color=color,
            size=size,
            barcode=barcode,
            unit_cost_price=unit_cost_price,
            sale_price=sale_price,
            is_active=True,
            sort_order=next_sort_order,
        )
        return self.option_repo.add(option)

    def update_option(self, option: ProductOption, updates: dict[str, Any]) -> ProductOption:
        """PATCH 방식 부분 수정 - updates에 키가 없는 필드는 손대지 않고(기존 값
        유지), 키가 있으면 값이 None이어도(명시적 NULL) 그대로 반영해 지운다.
        허용된 6개 필드만 이름으로 직접 대입하고 그 외 키는 무시한다(임의 키를
        setattr로 반영하지 않는다)."""
        if "option_name" in updates:
            option.option_name = updates["option_name"]
        if "color" in updates:
            option.color = updates["color"]
        if "size" in updates:
            option.size = updates["size"]
        if "barcode" in updates:
            option.barcode = updates["barcode"]
        if "unit_cost_price" in updates:
            option.unit_cost_price = updates["unit_cost_price"]
        if "sale_price" in updates:
            option.sale_price = updates["sale_price"]
        self.session.flush()
        return option

    def set_option_active(self, option: ProductOption, is_active: bool) -> ProductOption:
        option.is_active = is_active
        self.session.flush()
        return option

    def delete_option(self, option: ProductOption) -> None:
        """옵션을 삭제한다. 이미 실주문(order_items)이 이 옵션을 참조하고 있으면
        FK 무결성/과거 손익 이력 보호를 위해 삭제를 거부한다(OptionInUseError) -
        이 경우 사용자는 대신 비활성화(set_option_active)를 사용해야 한다."""
        if self.order_repo.has_items_for_option(option.id):
            raise OptionInUseError(f"이미 주문에서 사용 중인 옵션은 삭제할 수 없습니다: option_id={option.id}")
        self.option_repo.delete(option)

    def reorder_options(self, product_id: int, ordered_option_ids: list[int]) -> list[ProductOption]:
        self.option_repo.reorder(product_id, ordered_option_ids)
        self.session.flush()
        return self.option_repo.list_by_product(product_id)

    def delete_product(self, product: Product) -> None:
        """상품을 소프트 삭제한다(products.is_deleted=True) - 옵션 삭제(delete_option)와
        달리 실주문 참조 여부를 확인해 막지 않는다. 상품은 옵션을 여러 개 가질 수
        있어 "일부 옵션만 주문에 쓰였다"는 애매한 경우가 흔하므로, 하드 삭제를
        아예 허용하지 않고 항상 소프트 삭제+옵션 비활성화로 처리하는 편이 단순하고
        안전하다(다른 핵심 엔티티와 동일한 관례 - models/base.py SoftDeleteMixin).
        목록 조회(list_active 등)에서는 자동으로 제외되며, 함께 노출되지 않도록
        옵션도 모두 비활성화한다."""
        product.is_deleted = True
        for option in self.option_repo.list_by_product(product.id):
            option.is_active = False
        self.session.flush()

    def create_platform_map(
        self,
        product_option_id: int,
        platform_id: int,
        platform_option_id: str,
        display_name: Optional[str] = None,
        seller_product_code: Optional[str] = None,
        platform_product_id: Optional[str] = None,
    ) -> ProductPlatformMap:
        """(platform_id, platform_option_id) 조합이 이미 있으면 IntegrityError가
        발생하고, api/main.py의 전역 핸들러가 409로 변환한다.

        platform_option_id(옵션 단위, 매핑 키)와 platform_product_id(상품 단위,
        비유니크·참고용)는 의미가 다른 별개 값이다 - 혼동하지 않는다.
        """
        mapping = ProductPlatformMap(
            product_option_id=product_option_id,
            platform_id=platform_id,
            platform_option_id=platform_option_id,
            platform_product_id=platform_product_id,
            display_name=display_name,
            seller_product_code=seller_product_code,
        )
        return self.platform_map_repo.add(mapping)

    def update_platform_map(
        self,
        mapping: ProductPlatformMap,
        display_name: Optional[str] = None,
        seller_product_code: Optional[str] = None,
        platform_product_id: Optional[str] = None,
        platform_option_id: Optional[str] = None,
    ) -> ProductPlatformMap:
        if display_name is not None:
            mapping.display_name = display_name
        if seller_product_code is not None:
            mapping.seller_product_code = seller_product_code
        if platform_product_id is not None:
            mapping.platform_product_id = platform_product_id
        if platform_option_id is not None:
            mapping.platform_option_id = platform_option_id
        self.session.flush()
        return mapping

    def delete_platform_map(self, mapping: ProductPlatformMap) -> None:
        self.platform_map_repo.delete(mapping)

    def add_image(
        self, product_id: int, image_url: str, is_thumbnail: bool = False, product_option_id: Optional[int] = None
    ) -> ProductImage:
        """이미지를 등록한다. product_option_id가 주어지면 옵션 전용 이미지(옵션이미지)로
        등록되고, 대표이미지 자동지정 로직은 상품 전체 이미지(product_option_id가 없는
        이미지)에만 적용된다 - 옵션이미지는 대표이미지 후보가 아니다.

        is_thumbnail=True이거나 이 상품의 첫 상품-전체 이미지면 대표이미지로
        지정한다(대표이미지는 항상 정확히 1개만 존재하도록 기존 대표이미지는 해제한다).
        """
        existing_images = self.image_repo.list_by_product(product_id)
        product_level_images = [i for i in existing_images if i.product_option_id is None]
        make_thumbnail = product_option_id is None and (is_thumbnail or not product_level_images)
        if make_thumbnail:
            for image in product_level_images:
                image.is_thumbnail = False
        next_sort_order = max((i.sort_order for i in existing_images), default=-1) + 1
        image = self.image_repo.add(
            ProductImage(
                product_id=product_id,
                product_option_id=product_option_id,
                image_url=image_url,
                is_thumbnail=make_thumbnail,
                sort_order=next_sort_order,
            )
        )
        return image

    def set_thumbnail(self, product_id: int, image: ProductImage) -> ProductImage:
        """대표이미지를 지정한다 - 같은 상품의 다른 상품-전체 이미지는 모두 대표이미지에서
        해제한다(옵션이미지는 대표이미지 대상이 아니다)."""
        for other in self.image_repo.list_by_product(product_id):
            if other.product_option_id is None:
                other.is_thumbnail = other.id == image.id
        self.session.flush()
        return image

    def delete_image(self, image: ProductImage) -> None:
        was_thumbnail = image.is_thumbnail
        product_id = image.product_id
        self.image_repo.delete(image)
        if was_thumbnail:
            remaining = self.image_repo.list_by_product(product_id)
            if remaining:
                remaining[0].is_thumbnail = True
                self.session.flush()

    def list_unmatched_items(self) -> list[UnmatchedPlatformItem]:
        """자동매칭 4단계가 모두 실패해 아직 사용자가 직접 연결하지 않은 주문상품
        목록을 조회한다(services.product_sync_service.ProductSyncService.match_unmapped_item
        참고)."""
        return self.unmatched_repo.list_pending()

    def resolve_unmatched_item(self, item: UnmatchedPlatformItem, product_option_id: int) -> ProductPlatformMap:
        """미매칭 상품을 사용자가 지정한 기존 옵션에 수동으로 연결한다 - 새 플랫폼
        매핑을 등록하고 미매칭 기록을 MATCHED로 표시한다(matched_by="MANUAL"). 이후
        재수집 시 OrderSyncService._sync_items()가 이 매핑을 찾아 주문상품을 채운다."""
        assert item.platform_option_id is not None  # 기록 시점에 이미 검증됨
        mapping = self.create_platform_map(
            product_option_id,
            item.platform_id,
            item.platform_option_id,
            display_name=item.product_name,
            seller_product_code=item.seller_product_code,
            platform_product_id=item.platform_product_id,
        )
        self.unmatched_repo.mark_matched(item, product_option_id, matched_by="MANUAL")
        return mapping


class ProductCostService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.cost_repo = ProductCostHistoryRepository(session)

    def add_cost(
        self,
        product_option_id: int,
        cost_price: float,
        effective_from: datetime,
        supplier_id: Optional[int] = None,
        created_by: Optional[int] = None,
    ) -> ProductCostHistory:
        open_record = self.cost_repo.get_open(product_option_id)
        if open_record is not None:
            open_record.effective_to = effective_from

        record = ProductCostHistory(
            product_option_id=product_option_id,
            supplier_id=supplier_id,
            cost_price=cost_price,
            effective_from=effective_from,
            effective_to=None,
            created_by=created_by,
        )
        return self.cost_repo.add(record)

    def list_history(self, product_option_id: int) -> list[ProductCostHistory]:
        return self.cost_repo.list_by_option(product_option_id)
