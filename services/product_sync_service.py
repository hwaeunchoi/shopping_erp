"""
services/product_sync_service.py
-------------------------------------
상품 자동등록/자동매칭 전담 서비스 (2026-07-08 ERP 데이터 모델 재설계 반영).

핵심 원칙:
- 네이버 스마트스토어가 ERP의 기준 데이터(Master)다. Product = 네이버 Group Product
  (groupProductNo), ProductOption = 네이버 Channel Product(channelProductNo). 상품은
  오직 sync_products_from_naver()(네이버 "상품" API)를 통해서만 생성/갱신된다.
- 어떤 플랫폼도 주문 API에서는 상품을 생성하지 않는다. 매핑이 없는 주문상품을
  만나면(OrderSyncService가 호출) match_unmapped_item()이 아래 3단계로 기존
  상품(옵션)을 찾아 매핑만 추가한다(새 상품은 만들지 않는다):
    1. platform_option_id(플랫폼 옵션번호) 완전 일치 - 옵션 단위 유니크 키, 가장 정확
    2. platform_product_id(플랫폼 상품번호) 완전 일치 - 상품 단위(비유니크)라 그
       상품에 옵션이 정확히 1개일 때만 채택, 모호하면 다음 단계로
    3. seller_product_code(판매자상품코드) 완전 일치 - 유일하게 플랫폼 범위를 넘는
       단계. 쿠팡/카카오/11번가/ESM 주문이 이미 등록된 네이버 상품에 연결되는 경로.
  세 단계 모두 실패하면 product_unmatched_items에 "미매칭 상품"으로 기록하고
  None을 반환한다 - 상품명/옵션명 유사도 등 추측성 매칭은 사용하지 않는다(오탐 위험).
- SKU(ProductOption.sku_code)는 ERP 내부 식별자다. 판매자상품코드/플랫폼코드와
  절대 동일시하지 않으며, 어떤 매칭 단계에도 사용하지 않는다. 신규 옵션 등록 시
  "SKU-{option.id:06d}" 형식으로 ERP가 자체 채번한다(플랫폼 데이터에 의존하지 않음).
"""

import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from integrations.malls.base_mall_connector import BaseMallConnector
from models.product import Product, ProductImage, ProductOption, ProductPlatformMap, UnmatchedPlatformItem
from repositories.product_repository import (
    ProductImageRepository,
    ProductOptionRepository,
    ProductPlatformMapRepository,
    ProductRepository,
    UnmatchedPlatformItemRepository,
)

logger = logging.getLogger(__name__)


class ProductSyncService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.product_repo = ProductRepository(session)
        self.option_repo = ProductOptionRepository(session)
        self.platform_map_repo = ProductPlatformMapRepository(session)
        self.image_repo = ProductImageRepository(session)
        self.unmatched_repo = UnmatchedPlatformItemRepository(session)

    # ---------------------------------------------------------------
    # 네이버 상품 API 동기화 - 상품이 새로 생기는 유일한 경로
    # ---------------------------------------------------------------

    def sync_products_from_naver(self, connector: BaseMallConnector, platform_id: int) -> dict[str, int]:
        """네이버 상품 API에서 상품 목록을 가져와 등록/갱신하고 대표/추가/옵션
        이미지를 저장한다.

        커넥터의 fetch_products()는 상품(Group Product) 단위로 그룹핑된 정규화
        목록을 반환한다:
        [{"product_name", "category", "brand", "manufacturer",
          "images": {"representative_url", "optional_urls": [...]},
          "items": [{"platform_option_id"(channelProductNo), "platform_product_id"(groupProductNo),
                     "option_name", "seller_product_code", "sale_price", "is_selling",
                     "option_image_url"}, ...]}, ...]

        이미 platform_option_id(channelProductNo)로 매핑된 옵션은 이름/가격/이미지
        등 최신 정보로 갱신하고, 매핑이 없는 옵션은 새 옵션(+필요시 새 상품)을 생성한다.
        """
        raw_products = connector.fetch_products()
        created_products = created_options = updated_options = 0
        total_items = 0

        for raw in raw_products:
            product, is_new_product = self._find_or_create_product(platform_id, raw)
            if is_new_product:
                created_products += 1
            self._save_product_images(product.id, raw)

            for item in raw.get("items", []):
                total_items += 1
                platform_option_id = item.get("platform_option_id")
                if not platform_option_id:
                    continue
                mapping = self.platform_map_repo.get_by_option_id(platform_id, platform_option_id)
                if mapping is None:
                    mapping = self._register_option(product.id, platform_id, item, raw.get("product_name"))
                    created_options += 1
                else:
                    self._update_option_from_naver(mapping, item, raw.get("product_name"))
                    updated_options += 1
                self._save_option_image(product.id, mapping.product_option_id, item)

        self.session.flush()
        return {
            "total_items": total_items,
            "created_products": created_products,
            "created_options": created_options,
            "updated_options": updated_options,
        }

    def _find_or_create_product(self, platform_id: int, raw: dict[str, Any]) -> tuple[Product, bool]:
        """이 네이버 상품 그룹(Group Product) 아래 옵션 중 하나라도 이미 매핑돼 있으면
        그 상품을 재사용하고, 없으면 새 상품을 만든다 - 같은 상품의 여러 옵션이
        서로 다른 상품으로 쪼개지지 않도록 groupProductNo 단위로 묶는다.

        1차: 이번 raw 묶음 자체의 items 중 이미 매핑된 옵션이 있는지 확인한다.
        2차(방어적 재확인, 2026-07-08 실제 사고로 도입): 1차에서 못 찾았더라도,
        같은 groupProductNo(platform_product_id)로 이미 등록된 옵션이 DB에
        있는지 한 번 더 확인한다. 커넥터의 페이지네이션 중 일시적 응답 저하나
        재시도로 인해 같은 groupProductNo의 항목이 서로 다른 배치(raw)로 나뉘어
        들어오는 경우, 1차 검사만으로는 이 raw의 items 안에 매핑된 옵션이 하나도
        없어 새 상품을 또 만들어버릴 수 있다 - 실제로 groupProductNo=51839130
        건에서 이 경로로 같은 물리 상품이 Product 2개로 쪼개지는 사고가 있었다
        (page 1과 page 11에 나뉘어 응답됨, 재현·확인 완료). 이 2차 확인이 그
        재발을 막는다."""
        for item in raw.get("items", []):
            platform_option_id = item.get("platform_option_id")
            if not platform_option_id:
                continue
            existing = self.platform_map_repo.get_by_option_id(platform_id, platform_option_id)
            if existing is not None:
                option = self.option_repo.get_by_id(existing.product_option_id)
                assert option is not None
                product = self.product_repo.get_by_id(option.product_id)
                assert product is not None
                return product, False

        group_platform_product_id = next(
            (item.get("platform_product_id") for item in raw.get("items", []) if item.get("platform_product_id")), None
        )
        if group_platform_product_id:
            candidates = self.platform_map_repo.list_by_product_id(platform_id, group_platform_product_id)
            if candidates:
                option = self.option_repo.get_by_id(candidates[0].product_option_id)
                assert option is not None
                product = self.product_repo.get_by_id(option.product_id)
                assert product is not None
                logger.warning(
                    "groupProductNo=%s: 이번 동기화 배치(raw)의 항목 중에는 기존 매핑이 없었지만, "
                    "같은 groupProductNo로 이미 등록된 상품(product_id=%s)을 찾아 재사용합니다 - "
                    "이 그룹이 두 번 이상의 배치로 쪼개져 들어왔을 가능성이 있습니다(예: 페이지네이션 "
                    "응답 저하/재시도). 이 경고가 반복되면 _fetch_raw_products_live/"
                    "_normalize_live_products의 페이지 응답 안정성을 점검하세요.",
                    group_platform_product_id,
                    product.id,
                )
                return product, False

        product_name = raw.get("product_name") or "자동등록 상품"
        product = self.product_repo.add(
            Product(
                name=product_name[:200],
                category=(raw.get("category") or None),
                brand=(raw.get("brand") or None),
                manufacturer=(raw.get("manufacturer") or None),
                status="ACTIVE",
            )
        )
        self.session.flush()
        return product, True

    def _register_option(
        self, product_id: int, platform_id: int, item: dict[str, Any], product_name: Optional[str]
    ) -> ProductPlatformMap:
        platform_option_id = item["platform_option_id"]
        option_name = item.get("option_name")

        # SKU는 ERP 내부 식별자 - 플랫폼 데이터에 의존하지 않는다. 옵션을 먼저 등록해
        # id를 채번한 뒤, 그 id를 근거로 최종 SKU를 확정한다(등록 시점엔 sku_code가
        # NOT NULL+UNIQUE라 임시값이 필요하므로 옵션 id 기반의 자리표시자를 넣고 갱신).
        option = self.option_repo.add(
            ProductOption(
                product_id=product_id,
                sku_code=f"PENDING-{platform_id}-{platform_option_id}"[:50],
                option_name=option_name[:255] if option_name else None,
                sale_price=item.get("sale_price"),
                is_active=item.get("is_selling", True),
            )
        )
        self.session.flush()  # option.id 확정
        option.sku_code = f"SKU-{option.id:06d}"

        mapping = self.platform_map_repo.add(
            ProductPlatformMap(
                product_option_id=option.id,
                platform_id=platform_id,
                platform_option_id=platform_option_id,
                platform_product_id=(item.get("platform_product_id") or None),
                display_name=(product_name[:200] if product_name else None),
                seller_product_code=(item.get("seller_product_code") or None),
            )
        )
        logger.info(
            "네이버 상품 API로 상품을 자동 등록했습니다: product_id=%s, option_id=%s, sku_code=%s, "
            "platform_option_id=%s",
            product_id,
            option.id,
            option.sku_code,
            platform_option_id,
        )
        return mapping

    def _update_option_from_naver(
        self, mapping: ProductPlatformMap, item: dict[str, Any], product_name: Optional[str]
    ) -> None:
        option = self.option_repo.get_by_id(mapping.product_option_id)
        assert option is not None
        option_name = item.get("option_name")
        if option_name:
            option.option_name = option_name[:255]
        if "sale_price" in item and item["sale_price"] is not None:
            option.sale_price = item["sale_price"]
        if "is_selling" in item:
            option.is_active = bool(item["is_selling"])

        if item.get("seller_product_code"):
            mapping.seller_product_code = item["seller_product_code"]
        if product_name:
            mapping.display_name = product_name[:200]
        if item.get("platform_product_id"):
            mapping.platform_product_id = item["platform_product_id"]

    def _save_product_images(self, product_id: int, raw: dict[str, Any]) -> None:
        images = raw.get("images") or {}
        existing = self.image_repo.list_by_product(product_id)
        existing_urls = {i.image_url for i in existing if i.product_option_id is None}
        has_thumbnail = any(i.is_thumbnail for i in existing if i.product_option_id is None)

        representative_url = images.get("representative_url")
        if representative_url and representative_url not in existing_urls:
            self.image_repo.add(
                ProductImage(
                    product_id=product_id, image_url=representative_url, is_thumbnail=not has_thumbnail, sort_order=0
                )
            )
            has_thumbnail = True

        for idx, url in enumerate(images.get("optional_urls") or [], start=1):
            if url in existing_urls:
                continue
            self.image_repo.add(ProductImage(product_id=product_id, image_url=url, is_thumbnail=False, sort_order=idx))

    def _save_option_image(self, product_id: int, product_option_id: int, item: dict[str, Any]) -> None:
        url = item.get("option_image_url")
        if not url:
            return
        existing_urls = {
            i.image_url for i in self.image_repo.list_by_product(product_id) if i.product_option_id == product_option_id
        }
        if url in existing_urls:
            return
        self.image_repo.add(
            ProductImage(
                product_id=product_id,
                product_option_id=product_option_id,
                image_url=url,
                is_thumbnail=False,
                sort_order=0,
            )
        )

    # ---------------------------------------------------------------
    # 자동매칭(주문 수집 중 호출) - 절대 새 상품을 만들지 않는다
    # ---------------------------------------------------------------

    def match_unmapped_item(
        self, platform_id: int, item: dict[str, Any], platform_order_no: str
    ) -> Optional[ProductPlatformMap]:
        """매핑이 없는 주문상품을 만나면, 절대 새 상품을 만들지 않고 아래 3단계로만
        기존 상품(옵션)을 찾아 매핑만 추가한다 - 상품명/옵션명 유사도 등 추측성
        매칭은 사용하지 않는다. 세 단계 모두 실패하면(아직 네이버 상품 동기화가
        안 됐거나 원래 없는 상품이라는 뜻) 미매칭 상품(product_unmatched_items)으로
        기록하고 None을 반환한다.
        """
        platform_option_id = item.get("platform_option_id")
        if not platform_option_id:
            return None

        # 이미 정확히 이 (platform_id, platform_option_id) 매핑이 있으면 그대로
        # 반환한다(멱등) - 새로 만들면 유니크 제약 위반이 된다. 실제 호출부
        # (OrderSyncService._sync_items)는 이 경우를 이미 걸러내고서 호출하지만,
        # 이 메서드가 단독으로 호출될 가능성(수동 재확인 등)에 대비한 방어 코드다.
        existing_exact = self.platform_map_repo.get_by_option_id(platform_id, platform_option_id)
        if existing_exact is not None:
            return existing_exact

        option_id = self._find_matching_option_id(platform_id, item)
        if option_id is None:
            self._record_unmatched_item(platform_id, platform_order_no, item)
            return None

        mapping = self.platform_map_repo.add(
            ProductPlatformMap(
                product_option_id=option_id,
                platform_id=platform_id,
                platform_option_id=platform_option_id,
                platform_product_id=(item.get("platform_product_id") or None),
                display_name=(item.get("product_name") or None),
                seller_product_code=(item.get("seller_product_code") or None),
            )
        )
        logger.info("기존 상품에 자동 매칭했습니다: option_id=%s, platform_option_id=%s", option_id, platform_option_id)
        return mapping

    def _find_matching_option_id(self, platform_id: int, item: dict[str, Any]) -> Optional[int]:
        """platform_option_id의 정확한(platform_id, platform_option_id) 일치는 match_unmapped_item()
        상단에서 이미 확인했으므로(멱등 처리), 여기서는 2·3순위(플랫폼상품번호 ->
        판매자상품코드)만 확인한다."""
        platform_product_id = item.get("platform_product_id")
        if platform_product_id:
            candidates = self.platform_map_repo.list_by_product_id(platform_id, platform_product_id)
            distinct_option_ids = {c.product_option_id for c in candidates}
            if len(distinct_option_ids) == 1:
                return next(iter(distinct_option_ids))
            # 상품에 옵션이 2개 이상 걸리면(비유니크 키) 어느 옵션인지 특정할 수
            # 없으므로 자동 연결하지 않는다 - 확실하지 않으면 미매칭으로 남긴다.

        seller_product_code = item.get("seller_product_code")
        if seller_product_code:
            existing = self.platform_map_repo.get_by_seller_product_code(seller_product_code)
            if existing is not None:
                return existing.product_option_id

        return None

    def _record_unmatched_item(self, platform_id: int, platform_order_no: str, item: dict[str, Any]) -> None:
        self.unmatched_repo.add(
            UnmatchedPlatformItem(
                platform_id=platform_id,
                platform_order_no=platform_order_no,
                platform_option_id=item.get("platform_option_id"),
                platform_product_id=item.get("platform_product_id"),
                product_name=item.get("product_name"),
                option_name=item.get("option_name"),
                seller_product_code=item.get("seller_product_code"),
                quantity=item.get("quantity"),
                unit_price=item.get("unit_price"),
            )
        )
