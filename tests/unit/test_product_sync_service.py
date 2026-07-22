"""
tests/unit/test_product_sync_service.py
-------------------------------------------
ProductSyncService 단위 테스트. 네이버 상품 API 동기화(상품이 생기는 유일한 경로)와
자동매칭(3단계 cascade: platform_option_id -> platform_product_id -> seller_product_code,
전부 실제 고유ID 기반 - 이름/유사도 매칭은 사용하지 않는다) 로직을 검증한다.
"""

from datetime import date
from typing import Any

from integrations.malls.base_mall_connector import BaseMallConnector
from models.product import Product, ProductOption, ProductPlatformMap
from repositories.product_repository import (
    ProductImageRepository,
    ProductOptionRepository,
    UnmatchedPlatformItemRepository,
)
from services.product_sync_service import ProductSyncService


class StubProductConnector(BaseMallConnector):
    """fetch_products()만 실제로 값을 통제하는 스텁 - 나머지는 이 테스트에서 쓰지 않는다."""

    platform_code = "stub"

    def __init__(self, raw_products: list[dict[str, Any]]) -> None:
        self._raw_products = raw_products

    def fetch_products(self) -> list[dict[str, Any]]:
        return self._raw_products

    def fetch_orders(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        raise NotImplementedError

    def fetch_order_detail(self, platform_order_no: str) -> dict[str, Any]:
        raise NotImplementedError

    def update_shipment(self, platform_order_no: str, carrier: str, tracking_no: str) -> bool:
        raise NotImplementedError

    def fetch_settlements(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        raise NotImplementedError


def _naver_product(
    product_name: str = "네이버 상품",
    items: list[dict[str, Any]] | None = None,
    representative_url: str | None = None,
    optional_urls: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "product_name": product_name,
        "category": "잡화",
        "brand": "브랜드A",
        "manufacturer": "제조사A",
        "images": {"representative_url": representative_url, "optional_urls": optional_urls or []},
        "items": items
        or [
            {
                "platform_option_id": "ITEM-001",
                "platform_product_id": "PRODUCT-001",
                "option_name": "블랙",
                "seller_product_code": "SELLER-001",
                "sale_price": 19900,
                "is_selling": True,
                "option_image_url": None,
            }
        ],
    }


class TestSyncProductsFromNaver:
    def test_registers_new_product_option_and_mapping(self, db_session, platform):
        connector = StubProductConnector([_naver_product()])
        service = ProductSyncService(db_session)

        result = service.sync_products_from_naver(connector, platform.id)

        assert result == {"total_items": 1, "created_products": 1, "created_options": 1, "updated_options": 0}
        mapping = (
            db_session.query(ProductPlatformMap).filter_by(platform_id=platform.id, platform_option_id="ITEM-001").one()
        )
        assert mapping.platform_product_id == "PRODUCT-001"
        assert mapping.seller_product_code == "SELLER-001"
        option = db_session.query(ProductOption).filter_by(id=mapping.product_option_id).one()
        assert option.option_name == "블랙"
        assert option.is_active is True
        assert float(option.sale_price) == 19900
        product = db_session.query(Product).filter_by(id=option.product_id).one()
        assert product.name == "네이버 상품"
        assert product.category == "잡화"
        assert product.brand == "브랜드A"
        assert product.manufacturer == "제조사A"

    def test_sku_is_erp_internal_not_derived_from_platform_data(self, db_session, platform):
        """SKU는 ERP 내부 채번(SKU-{id:06d})이어야 하고, 판매자상품코드/플랫폼코드와
        절대 같은 값이 되어서는 안 된다 - AUTO-{platform_id}-{code} 방식은 폐기."""
        connector = StubProductConnector([_naver_product()])
        service = ProductSyncService(db_session)

        service.sync_products_from_naver(connector, platform.id)

        option = db_session.query(ProductOption).one()
        assert option.sku_code == f"SKU-{option.id:06d}"
        assert not option.sku_code.startswith("AUTO-")
        assert option.sku_code != "ITEM-001"  # platform_option_id와 동일시하지 않는다
        assert option.sku_code != "SELLER-001"  # seller_product_code와도 동일시하지 않는다

    def test_groups_multiple_options_under_one_product(self, db_session, platform):
        connector = StubProductConnector(
            [
                _naver_product(
                    items=[
                        {
                            "platform_option_id": "ITEM-A",
                            "platform_product_id": "PRODUCT-GROUP",
                            "option_name": "블랙",
                            "seller_product_code": "SELLER-A",
                            "is_selling": True,
                        },
                        {
                            "platform_option_id": "ITEM-B",
                            "platform_product_id": "PRODUCT-GROUP",
                            "option_name": "화이트",
                            "seller_product_code": "SELLER-B",
                            "is_selling": True,
                        },
                    ]
                )
            ]
        )
        service = ProductSyncService(db_session)

        result = service.sync_products_from_naver(connector, platform.id)

        assert result["created_products"] == 1
        assert result["created_options"] == 2
        options = (
            db_session.query(ProductOption)
            .join(ProductPlatformMap, ProductPlatformMap.product_option_id == ProductOption.id)
            .filter(ProductPlatformMap.platform_product_id == "PRODUCT-GROUP")
            .all()
        )
        product_ids = {o.product_id for o in options}
        assert len(product_ids) == 1  # 같은 상품(Group Product) 아래로 묶였다
        # SKU는 서로 다른 ERP 내부 채번값이어야 한다(플랫폼 옵션번호와 무관).
        skus = {o.sku_code for o in options}
        assert len(skus) == 2

    def test_resync_updates_existing_mapping_instead_of_duplicating(self, db_session, platform):
        connector = StubProductConnector([_naver_product(product_name="변경 전 이름")])
        service = ProductSyncService(db_session)
        service.sync_products_from_naver(connector, platform.id)
        products_before = db_session.query(Product).count()
        original_option = db_session.query(ProductOption).one()
        original_sku = original_option.sku_code

        connector2 = StubProductConnector(
            [
                _naver_product(
                    product_name="변경 후 이름",
                    items=[
                        {
                            "platform_option_id": "ITEM-001",
                            "platform_product_id": "PRODUCT-001",
                            "option_name": "블랙-수정",
                            "seller_product_code": "SELLER-001-NEW",
                            "sale_price": 21900,
                            "is_selling": False,
                        }
                    ],
                )
            ]
        )
        result = service.sync_products_from_naver(connector2, platform.id)

        assert result == {"total_items": 1, "created_products": 0, "created_options": 0, "updated_options": 1}
        assert db_session.query(Product).count() == products_before  # 새 상품이 생기지 않았다
        mapping = (
            db_session.query(ProductPlatformMap).filter_by(platform_id=platform.id, platform_option_id="ITEM-001").one()
        )
        assert mapping.display_name == "변경 후 이름"
        assert mapping.seller_product_code == "SELLER-001-NEW"
        option = ProductOptionRepository(db_session).get_by_id(mapping.product_option_id)
        assert option is not None
        assert option.option_name == "블랙-수정"
        assert option.is_active is False
        assert option.sale_price == 21900
        assert option.sku_code == original_sku  # 재동기화로 SKU가 바뀌지 않는다(ERP 내부값 불변)

    def test_saves_representative_optional_and_option_images(self, db_session, platform):
        connector = StubProductConnector(
            [
                _naver_product(
                    representative_url="https://img.example.com/rep.jpg",
                    optional_urls=["https://img.example.com/opt1.jpg"],
                    items=[
                        {
                            "platform_option_id": "ITEM-001",
                            "platform_product_id": "PRODUCT-001",
                            "option_name": "블랙",
                            "is_selling": True,
                            "option_image_url": "https://img.example.com/option-black.jpg",
                        }
                    ],
                )
            ]
        )
        service = ProductSyncService(db_session)

        service.sync_products_from_naver(connector, platform.id)

        mapping = (
            db_session.query(ProductPlatformMap).filter_by(platform_id=platform.id, platform_option_id="ITEM-001").one()
        )
        option = ProductOptionRepository(db_session).get_by_id(mapping.product_option_id)
        assert option is not None
        images = ProductImageRepository(db_session).list_by_product(option.product_id)
        urls_by_option = {(i.image_url, i.product_option_id) for i in images}
        assert ("https://img.example.com/rep.jpg", None) in urls_by_option
        assert ("https://img.example.com/opt1.jpg", None) in urls_by_option
        assert ("https://img.example.com/option-black.jpg", option.id) in urls_by_option
        representative = next(i for i in images if i.image_url == "https://img.example.com/rep.jpg")
        assert representative.is_thumbnail is True

    def test_same_group_product_id_split_across_two_raw_batches_does_not_duplicate_product(self, db_session, platform):
        """실제 사고 재현(2026-07-08, groupProductNo=51839130): 네이버 API 페이지네이션
        응답 저하/재시도로 같은 groupProductNo의 항목들이 서로 다른 raw 배치로 나뉘어
        들어와도(예: 커넥터가 두 번의 fetch_products() 호출로 각각 일부만 반환), 같은
        상품이 Product 2개로 쪼개지면 안 된다. _find_or_create_product의 2차 방어
        확인(같은 platform_product_id로 이미 등록된 상품 재사용)을 검증한다."""
        service = ProductSyncService(db_session)

        # 1차 배치: 이 그룹의 항목 중 일부만 포함(실제 사고에서 page 1에 해당)
        first_batch = StubProductConnector(
            [
                _naver_product(
                    product_name="곰솥",
                    items=[
                        {
                            "platform_option_id": "ITEM-A",
                            "platform_product_id": "GROUP-SPLIT",
                            "option_name": "20호",
                            "seller_product_code": "SELLER-20",
                            "is_selling": True,
                        }
                    ],
                )
            ]
        )
        service.sync_products_from_naver(first_batch, platform.id)

        # 2차 배치: 같은 groupProductNo인데 이번엔 완전히 다른 항목만 포함(page 11에 해당) -
        # 이 raw의 items 안에는 1차 배치에서 등록한 ITEM-A가 전혀 없다.
        second_batch = StubProductConnector(
            [
                _naver_product(
                    product_name="곰솥",
                    items=[
                        {
                            "platform_option_id": "ITEM-B",
                            "platform_product_id": "GROUP-SPLIT",
                            "option_name": "3호",
                            "seller_product_code": "SELLER-3",
                            "is_selling": True,
                        }
                    ],
                )
            ]
        )
        result = service.sync_products_from_naver(second_batch, platform.id)

        assert result["created_products"] == 0  # 새 상품을 또 만들지 않았다
        products = (
            db_session.query(Product)
            .join(ProductOption, ProductOption.product_id == Product.id)
            .join(ProductPlatformMap, ProductPlatformMap.product_option_id == ProductOption.id)
            .filter(ProductPlatformMap.platform_product_id == "GROUP-SPLIT")
            .all()
        )
        distinct_product_ids = {p.id for p in products}
        assert len(distinct_product_ids) == 1  # 두 배치의 항목이 결국 같은 Product 하나로 모였다

        options = (
            db_session.query(ProductOption)
            .join(ProductPlatformMap, ProductPlatformMap.product_option_id == ProductOption.id)
            .filter(ProductPlatformMap.platform_product_id == "GROUP-SPLIT")
            .all()
        )
        assert len(options) == 2  # ITEM-A, ITEM-B 둘 다 같은 상품 아래 등록됨

    def test_resync_does_not_duplicate_images(self, db_session, platform):
        connector = StubProductConnector([_naver_product(representative_url="https://img.example.com/rep.jpg")])
        service = ProductSyncService(db_session)
        service.sync_products_from_naver(connector, platform.id)

        service.sync_products_from_naver(connector, platform.id)

        mapping = (
            db_session.query(ProductPlatformMap).filter_by(platform_id=platform.id, platform_option_id="ITEM-001").one()
        )
        option = ProductOptionRepository(db_session).get_by_id(mapping.product_option_id)
        assert option is not None
        images = ProductImageRepository(db_session).list_by_product(option.product_id)
        assert len(images) == 1


class TestMatchUnmappedItem:
    """3단계 자동매칭(platform_option_id -> platform_product_id -> seller_product_code,
    전부 실제 고유ID 기반) - 새 상품은 절대 만들지 않고, 실패 시 미매칭 상품으로 기록한다.
    상품명/옵션명 유사도는 오탐 위험이 커 절대 사용하지 않는다."""

    def test_matches_by_platform_option_id_on_same_platform(self, db_session, platform, product_option):
        db_session.add(
            ProductPlatformMap(
                product_option_id=product_option.id, platform_id=platform.id, platform_option_id="COUPANG-ITEM-1"
            )
        )
        db_session.flush()
        products_before = db_session.query(Product).count()
        service = ProductSyncService(db_session)

        mapping = service.match_unmapped_item(platform.id, {"platform_option_id": "COUPANG-ITEM-1"}, "ORD-1")

        assert mapping is not None
        assert mapping.product_option_id == product_option.id
        assert db_session.query(Product).count() == products_before

    def test_matches_by_platform_product_id_when_unambiguous(self, db_session, platform, product_option):
        """플랫폼상품번호(상품 단위, 비유니크)는 그 상품에 옵션이 정확히 1개일 때만 채택한다."""
        db_session.add(
            ProductPlatformMap(
                product_option_id=product_option.id,
                platform_id=platform.id,
                platform_option_id="EXISTING-OPTION-CODE",
                platform_product_id="GROUP-001",
            )
        )
        db_session.flush()
        service = ProductSyncService(db_session)

        mapping = service.match_unmapped_item(
            platform.id, {"platform_option_id": "NEW-OPTION-CODE", "platform_product_id": "GROUP-001"}, "ORD-2"
        )

        assert mapping is not None
        assert mapping.product_option_id == product_option.id

    def test_does_not_match_by_platform_product_id_when_ambiguous(
        self, db_session, platform, product_option, second_product_option
    ):
        """같은 platform_product_id를 공유하는 옵션이 2개 이상이면 어느 쪽인지 특정할 수
        없으므로 자동 연결하지 않는다(확실하지 않으면 미매칭으로 남긴다)."""
        db_session.add_all(
            [
                ProductPlatformMap(
                    product_option_id=product_option.id,
                    platform_id=platform.id,
                    platform_option_id="OPTION-A",
                    platform_product_id="GROUP-AMBIGUOUS",
                ),
                ProductPlatformMap(
                    product_option_id=second_product_option.id,
                    platform_id=platform.id,
                    platform_option_id="OPTION-B",
                    platform_product_id="GROUP-AMBIGUOUS",
                ),
            ]
        )
        db_session.flush()
        service = ProductSyncService(db_session)

        mapping = service.match_unmapped_item(
            platform.id, {"platform_option_id": "NEW-OPTION-CODE", "platform_product_id": "GROUP-AMBIGUOUS"}, "ORD-3"
        )

        assert mapping is None
        unmatched = UnmatchedPlatformItemRepository(db_session).list_pending()
        assert len(unmatched) == 1

    def test_matches_by_seller_product_code_across_platforms(
        self, db_session, platform, naver_platform, product_option
    ):
        """seller_product_code는 유일하게 플랫폼 범위를 넘는 단계 - 쿠팡 주문이 이미
        네이버로 등록된 상품에 연결되는 경로를 검증한다."""
        db_session.add(
            ProductPlatformMap(
                product_option_id=product_option.id,
                platform_id=naver_platform.id,
                platform_option_id="NAVER-ITEM-1",
                seller_product_code="SHARED-CODE",
            )
        )
        db_session.flush()
        products_before = db_session.query(Product).count()
        service = ProductSyncService(db_session)

        mapping = service.match_unmapped_item(
            platform.id, {"platform_option_id": "COUPANG-ITEM-1", "seller_product_code": "SHARED-CODE"}, "ORD-4"
        )

        assert mapping is not None
        assert mapping.product_option_id == product_option.id
        assert db_session.query(Product).count() == products_before

    def test_does_not_use_sku_or_name_similarity_for_matching(self, db_session, platform, product_option):
        """SKU(ERP 내부값) 일치나 상품명 유사도로는 절대 매칭하지 않는다 - product_option의
        SKU/상품명과 완전히 같은 값을 넘겨도 다른 단서가 없으면 미매칭으로 남아야 한다."""
        service = ProductSyncService(db_session)

        mapping = service.match_unmapped_item(
            platform.id,
            {
                "platform_option_id": "COUPANG-ITEM-2",
                "seller_product_code": product_option.sku_code,  # SKU를 판매자코드 자리에 넣어도
                "product_name": "테스트 상품",  # product_option 픽스처와 이름이 같아도
            },
            "ORD-5",
        )

        assert mapping is None
        unmatched = UnmatchedPlatformItemRepository(db_session).list_pending()
        assert len(unmatched) == 1

    def test_records_unmatched_item_when_all_stages_fail(self, db_session, platform):
        products_before = db_session.query(Product).count()
        service = ProductSyncService(db_session)

        mapping = service.match_unmapped_item(
            platform.id,
            {
                "platform_option_id": "COUPANG-ITEM-4",
                "platform_product_id": "GROUP-UNKNOWN",
                "seller_product_code": "NO-SUCH-CODE",
                "product_name": "완전히 관계없는 상품명",
                "quantity": 2,
                "unit_price": 5000,
            },
            "ORD-6",
        )

        assert mapping is None
        assert db_session.query(Product).count() == products_before
        unmatched = UnmatchedPlatformItemRepository(db_session).list_pending()
        assert len(unmatched) == 1
        assert unmatched[0].platform_option_id == "COUPANG-ITEM-4"
        assert unmatched[0].platform_product_id == "GROUP-UNKNOWN"
        assert unmatched[0].platform_order_no == "ORD-6"
        assert unmatched[0].status == "PENDING"

    def test_returns_none_without_creating_anything_when_no_platform_option_id(self, db_session, platform):
        service = ProductSyncService(db_session)

        mapping = service.match_unmapped_item(platform.id, {"platform_option_id": ""}, "ORD-7")

        assert mapping is None
        assert UnmatchedPlatformItemRepository(db_session).list_pending() == []

    def test_naver_platform_also_only_matches_never_registers(self, db_session, naver_platform):
        """네이버 플랫폼이어도 match_unmapped_item은 절대 새 상품을 만들지 않는다 -
        상품 등록은 오직 sync_products_from_naver를 통해서만 일어난다."""
        products_before = db_session.query(Product).count()
        service = ProductSyncService(db_session)

        mapping = service.match_unmapped_item(naver_platform.id, {"platform_option_id": "NAVER-ITEM-UNKNOWN"}, "ORD-8")

        assert mapping is None
        assert db_session.query(Product).count() == products_before
