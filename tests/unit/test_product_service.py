"""
tests/unit/test_product_service.py
----------------------------------------
ProductService/ProductCostService 단위 테스트.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from repositories.product_repository import ProductCostHistoryRepository
from services.product_service import OptionInUseError, ProductCostService, ProductService


class TestProductCRUD:
    def test_create_and_update_product(self, db_session):
        service = ProductService(db_session)

        product = service.create_product("테스트 상품", "잡화", 10000)
        assert product.status == "ACTIVE"

        updated = service.update_product(product, name="변경된 상품", status="DISCONTINUED")
        assert updated.name == "변경된 상품"
        assert updated.status == "DISCONTINUED"
        assert updated.category == "잡화"  # 변경 안 한 필드는 유지

    def test_create_and_update_option(self, db_session):
        service = ProductService(db_session)
        product = service.create_product("옵션 테스트 상품", None, None)

        option = service.create_option(product.id, "SKU-TEST-001", option_name="블랙")
        assert option.is_active is True

        updated = service.update_option(option, {"color": "블랙", "size": "L"})
        assert updated.color == "블랙"
        assert updated.size == "L"

        deactivated = service.set_option_active(option, False)
        assert deactivated.is_active is False

    def test_update_option_missing_keys_keep_existing_values(self, db_session):
        service = ProductService(db_session)
        product = service.create_product("갱신 미포함 테스트", None, None)
        option = service.create_option(
            product.id, "SKU-PARTIAL-001", option_name="블랙", color="레드", unit_cost_price=3000
        )

        updated = service.update_option(option, {"size": "L"})

        assert updated.size == "L"
        assert updated.option_name == "블랙"  # updates에 없던 키는 그대로 유지된다
        assert updated.color == "레드"
        assert updated.unit_cost_price == 3000

    def test_update_option_explicit_none_clears_nullable_field(self, db_session):
        service = ProductService(db_session)
        product = service.create_product("NULL 삭제 테스트", None, None)
        option = service.create_option(product.id, "SKU-NULLABLE-001", option_name="블랙", barcode="8801234567890")

        updated = service.update_option(option, {"option_name": None, "barcode": None})

        assert updated.option_name is None  # updates에 키가 있으면 값이 None이어도 반영된다
        assert updated.barcode is None

    def test_update_option_preserves_255_char_names(self, db_session):
        service = ProductService(db_session)
        product = service.create_product("255자 테스트", None, None)
        option = service.create_option(product.id, "SKU-255-001")

        updated = service.update_option(option, {"option_name": "A" * 255})
        assert updated.option_name == "A" * 255
        assert len(updated.option_name) == 255

        updated_kr = service.update_option(option, {"option_name": "가" * 255})
        assert updated_kr.option_name == "가" * 255
        assert len(updated_kr.option_name) == 255

    def test_update_option_zero_unit_cost_price_succeeds(self, db_session):
        service = ProductService(db_session)
        product = service.create_product("원가0 테스트", None, None)
        option = service.create_option(product.id, "SKU-ZERO-001", unit_cost_price=3000)

        updated = service.update_option(option, {"unit_cost_price": 0})

        assert updated.unit_cost_price == 0

    def test_duplicate_sku_raises_integrity_error(self, db_session):
        service = ProductService(db_session)
        product = service.create_product("중복SKU 테스트", None, None)
        service.create_option(product.id, "SKU-DUP-001")

        with pytest.raises(IntegrityError):
            service.create_option(product.id, "SKU-DUP-001")

    def test_delete_product_soft_deletes_and_deactivates_options(self, db_session):
        """실주문이 이미 이 상품을 참조하고 있을 수 있어 실제 행은 지우지 않고
        is_deleted만 True로 바꾼다(다른 핵심 엔티티와 동일한 소프트 삭제 관례)."""
        service = ProductService(db_session)
        product = service.create_product("삭제 테스트 상품", None, None)
        option = service.create_option(product.id, "SKU-DELETE-001")

        service.delete_product(product)

        assert product.is_deleted is True
        assert option.is_active is False
        from repositories.product_repository import ProductRepository

        assert ProductRepository(db_session).get_by_id(product.id) is not None  # 실제로 지워지지는 않음

    def test_delete_product_with_orders_still_soft_deletes_without_raising(self, db_session, platform, warehouse):
        """옵션 삭제(delete_option)는 실주문에 쓰인 옵션이면 409(OptionInUseError)로
        막지만, 상품 삭제는 옵션 단위로 주문 참조 여부를 따지지 않고 항상 소프트
        삭제로 처리한다(요청 정책: 주문이 있어도 삭제는 막지 않고 소프트
        삭제/비활성화로 처리)."""
        from models.order import Order, OrderItem

        service = ProductService(db_session)
        product = service.create_product("주문있는 상품 삭제 테스트", None, None)
        option = service.create_option(product.id, "SKU-DELETE-WITH-ORDER-001")
        order = Order(
            platform_id=platform.id,
            platform_order_no="ORD-PRODUCT-DELETE-TEST",
            status="NEW",
            order_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
            total_amount=10000,
            discount_amount=0,
        )
        db_session.add(order)
        db_session.flush()
        db_session.add(
            OrderItem(order_id=order.id, product_option_id=option.id, quantity=1, unit_price=10000, line_amount=10000)
        )
        db_session.flush()

        service.delete_product(product)  # 예외 없이 성공해야 한다

        assert product.is_deleted is True
        assert option.is_active is False

    def test_restore_product_undoes_soft_delete(self, db_session):
        service = ProductService(db_session)
        product = service.create_product("복원 테스트 상품", None, None)
        service.delete_product(product)
        assert product.is_deleted is True

        restored = service.restore_product(product)

        assert restored.is_deleted is False

    def test_duplicate_product_copies_options_but_not_platform_maps(self, db_session, platform):
        service = ProductService(db_session)
        product = service.create_product("복제 원본 상품", "잡화", 10000, brand="브랜드A", manufacturer="제조사A")
        option = service.create_option(product.id, "SKU-ORIG-001", option_name="블랙", unit_cost_price=3000)
        service.create_platform_map(option.id, platform.id, "EXT-ORIG-001")

        duplicated = service.duplicate_product(product)

        assert duplicated.id != product.id
        assert duplicated.name == "복제 원본 상품 (복사본)"
        assert duplicated.category == "잡화"
        assert duplicated.brand == "브랜드A"
        assert duplicated.manufacturer == "제조사A"

        from repositories.product_repository import ProductOptionRepository, ProductPlatformMapRepository

        duplicated_options = ProductOptionRepository(db_session).list_by_product(duplicated.id)
        assert len(duplicated_options) == 1
        assert duplicated_options[0].sku_code != option.sku_code  # SKU는 유니크해야 하므로 새로 생성됨
        assert duplicated_options[0].option_name == "블랙"
        assert duplicated_options[0].unit_cost_price == 3000
        assert ProductPlatformMapRepository(db_session).list_by_option(duplicated_options[0].id) == []

    def test_reorder_options_updates_sort_order(self, db_session):
        service = ProductService(db_session)
        product = service.create_product("순서 테스트 상품", None, None)
        first = service.create_option(product.id, "SKU-ORDER-1")
        second = service.create_option(product.id, "SKU-ORDER-2")
        third = service.create_option(product.id, "SKU-ORDER-3")

        reordered = service.reorder_options(product.id, [third.id, first.id, second.id])

        assert [o.id for o in reordered] == [third.id, first.id, second.id]


class TestOptionDelete:
    def test_delete_option_without_orders_succeeds(self, db_session):
        service = ProductService(db_session)
        product = service.create_product("옵션삭제 테스트 상품", None, None)
        option = service.create_option(product.id, "SKU-DELETABLE-001")

        service.delete_option(option)

        from repositories.product_repository import ProductOptionRepository

        assert ProductOptionRepository(db_session).get_by_id(option.id) is None

    def test_delete_option_already_used_in_order_raises(self, db_session, platform, warehouse):
        from models.order import Order, OrderItem

        service = ProductService(db_session)
        product = service.create_product("주문사용 옵션 테스트 상품", None, None)
        option = service.create_option(product.id, "SKU-IN-USE-001")
        order = Order(
            platform_id=platform.id,
            platform_order_no="ORD-OPTION-DELETE-TEST",
            status="NEW",
            order_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
            total_amount=10000,
            discount_amount=0,
        )
        db_session.add(order)
        db_session.flush()
        db_session.add(
            OrderItem(order_id=order.id, product_option_id=option.id, quantity=1, unit_price=10000, line_amount=10000)
        )
        db_session.flush()

        with pytest.raises(OptionInUseError):
            service.delete_option(option)


class TestPlatformMap:
    def test_create_and_delete_platform_map(self, db_session, platform, product_option):
        service = ProductService(db_session)

        mapping = service.create_platform_map(product_option.id, platform.id, "EXT-CODE-001")
        assert mapping.platform_option_id == "EXT-CODE-001"

        service.delete_platform_map(mapping)
        db_session.flush()
        from repositories.product_repository import ProductPlatformMapRepository

        assert ProductPlatformMapRepository(db_session).get_by_id(mapping.id) is None

    def test_duplicate_platform_map_raises_integrity_error(self, db_session, platform, product_option):
        service = ProductService(db_session)
        service.create_platform_map(product_option.id, platform.id, "EXT-CODE-DUP")

        with pytest.raises(IntegrityError):
            service.create_platform_map(product_option.id, platform.id, "EXT-CODE-DUP")

    def test_update_platform_map_changes_display_fields(self, db_session, platform, product_option):
        service = ProductService(db_session)
        mapping = service.create_platform_map(
            product_option.id, platform.id, "EXT-CODE-002", display_name="이전 노출명"
        )

        updated = service.update_platform_map(
            mapping, display_name="새 노출명", seller_product_code="NEW-SELLER-CODE", platform_product_id="PRODNO-1"
        )

        assert updated.display_name == "새 노출명"
        assert updated.seller_product_code == "NEW-SELLER-CODE"
        assert updated.platform_product_id == "PRODNO-1"


class TestProductCostService:
    def test_first_cost_record_has_no_predecessor_to_close(self, db_session, product_option):
        service = ProductCostService(db_session)

        record = service.add_cost(product_option.id, 5000, datetime(2026, 1, 1, tzinfo=timezone.utc))

        assert record.effective_to is None
        assert record.cost_price == 5000

    def test_adding_new_cost_closes_previous_open_record(self, db_session, product_option):
        service = ProductCostService(db_session)
        first = service.add_cost(product_option.id, 5000, datetime(2026, 1, 1, tzinfo=timezone.utc))

        second = service.add_cost(product_option.id, 6000, datetime(2026, 3, 1, tzinfo=timezone.utc))

        assert first.effective_to == datetime(2026, 3, 1, tzinfo=timezone.utc)
        assert second.effective_to is None
        history = service.list_history(product_option.id)
        assert [h.cost_price for h in history] == [6000, 5000]  # effective_from 최신순

    def test_get_effective_cost_selects_matching_period(self, db_session, product_option):
        service = ProductCostService(db_session)
        service.add_cost(product_option.id, 5000, datetime(2026, 1, 1, tzinfo=timezone.utc))
        service.add_cost(product_option.id, 6000, datetime(2026, 3, 1, tzinfo=timezone.utc))
        repo = ProductCostHistoryRepository(db_session)

        before = repo.get_effective_cost(product_option.id, datetime(2026, 2, 1, tzinfo=timezone.utc))
        after = repo.get_effective_cost(product_option.id, datetime(2026, 4, 1, tzinfo=timezone.utc))

        assert before is not None and before.cost_price == 5000
        assert after is not None and after.cost_price == 6000

    def test_get_effective_cost_returns_none_when_no_record(self, db_session, product_option):
        repo = ProductCostHistoryRepository(db_session)

        result = repo.get_effective_cost(product_option.id, datetime(2026, 1, 1, tzinfo=timezone.utc))

        assert result is None


class TestProductImages:
    def test_first_image_becomes_thumbnail_automatically(self, db_session):
        service = ProductService(db_session)
        product = service.create_product("이미지 테스트 상품", None, None)

        first = service.add_image(product.id, "https://img.example.com/1.jpg")
        second = service.add_image(product.id, "https://img.example.com/2.jpg")

        assert first.is_thumbnail is True
        assert second.is_thumbnail is False

    def test_set_thumbnail_unsets_other_images(self, db_session):
        service = ProductService(db_session)
        product = service.create_product("썸네일 테스트 상품", None, None)
        first = service.add_image(product.id, "https://img.example.com/1.jpg")
        second = service.add_image(product.id, "https://img.example.com/2.jpg")

        service.set_thumbnail(product.id, second)
        db_session.refresh(first)
        db_session.refresh(second)

        assert first.is_thumbnail is False
        assert second.is_thumbnail is True

    def test_deleting_thumbnail_promotes_another_image(self, db_session):
        from repositories.product_repository import ProductImageRepository

        service = ProductService(db_session)
        product = service.create_product("썸네일삭제 테스트 상품", None, None)
        first = service.add_image(product.id, "https://img.example.com/1.jpg")
        second = service.add_image(product.id, "https://img.example.com/2.jpg")
        assert first.is_thumbnail is True

        service.delete_image(first)

        remaining = ProductImageRepository(db_session).list_by_product(product.id)
        assert len(remaining) == 1
        assert remaining[0].id == second.id
        assert remaining[0].is_thumbnail is True


class TestUnmatchedItems:
    def test_resolve_unmatched_item_creates_mapping_and_marks_matched(self, db_session, platform, product_option):
        from models.product import ProductPlatformMap, UnmatchedPlatformItem
        from repositories.product_repository import UnmatchedPlatformItemRepository

        service = ProductService(db_session)
        unmatched_repo = UnmatchedPlatformItemRepository(db_session)
        item = unmatched_repo.add(
            UnmatchedPlatformItem(
                platform_id=platform.id,
                platform_order_no="ORD-UNMATCHED-1",
                platform_option_id="COUPANG-CODE-1",
                product_name="미매칭 상품명",
                seller_product_code="SELLER-UNMATCHED-1",
                quantity=2,
                unit_price=5000,
            )
        )
        db_session.flush()

        mapping = service.resolve_unmatched_item(item, product_option.id)

        assert mapping.product_option_id == product_option.id
        assert mapping.platform_option_id == "COUPANG-CODE-1"
        assert item.status == "MATCHED"
        assert item.matched_option_id == product_option.id
        assert item.matched_at is not None
        assert (
            db_session.query(ProductPlatformMap)
            .filter_by(platform_id=platform.id, product_option_id=product_option.id)
            .count()
            == 1
        )

    def test_list_unmatched_items_excludes_matched(self, db_session, platform):
        from models.product import UnmatchedPlatformItem
        from repositories.product_repository import UnmatchedPlatformItemRepository

        unmatched_repo = UnmatchedPlatformItemRepository(db_session)
        pending = unmatched_repo.add(UnmatchedPlatformItem(platform_id=platform.id, platform_option_id="CODE-PENDING"))
        matched = unmatched_repo.add(
            UnmatchedPlatformItem(platform_id=platform.id, platform_option_id="CODE-MATCHED", status="MATCHED")
        )
        db_session.flush()

        service = ProductService(db_session)
        result = service.list_unmatched_items()

        result_ids = {i.id for i in result}
        assert pending.id in result_ids
        assert matched.id not in result_ids
