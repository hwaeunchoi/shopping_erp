"""
tests/unit/test_merge_split_product_groups.py
--------------------------------------------------
scripts/merge_split_product_groups.py의 legacy platform_map 삭제 판단 로직
회귀 테스트. 2026-07-08 대량 병합 실행 중 발견된 실제 버그를 재현한다:
옵션이 legacy 자기참조 매핑 하나만 가진 경우(정상 매핑이 전혀 없는 경우) 그
매핑을 삭제하면 옵션이 플랫폼과 완전히 연결이 끊긴다 - 이런 옵션은 절대
삭제 대상에 포함되면 안 된다.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

from merge_split_product_groups import build_plan  # noqa: E402

from models.platform import Platform  # noqa: E402
from models.product import Product, ProductOption, ProductPlatformMap  # noqa: E402


def _make_product_with_option(db_session, *, name: str, sku: str) -> tuple[Product, ProductOption]:
    product = Product(name=name, status="ACTIVE")
    db_session.add(product)
    db_session.flush()
    option = ProductOption(product_id=product.id, sku_code=sku, is_active=True)
    db_session.add(option)
    db_session.flush()
    return product, option


class TestBuildPlanLegacyMapDeletion:
    def test_option_with_only_legacy_map_is_not_deleted(self, db_session):
        """옵션이 legacy 자기참조 매핑 하나만 가진 경우 - 삭제하면 매핑이 0개가
        되므로 삭제 대상에서 반드시 제외돼야 한다(2026-07-08 실제 버그 재현)."""
        platform = Platform(code="naver_smartstore", name="네이버", connector_class="NaverSmartstoreConnector")
        db_session.add(platform)
        db_session.flush()

        keep_product, keep_option = _make_product_with_option(db_session, name="유지 상품", sku="SKU-KEEP-1")
        losing_product, losing_option = _make_product_with_option(db_session, name="제거될 상품", sku="SKU-LOSE-1")

        # KEEP 쪽은 옵션 2개(더 많음)를 갖도록 하나 더 추가
        keep_option_2 = ProductOption(product_id=keep_product.id, sku_code="SKU-KEEP-2", is_active=True)
        db_session.add(keep_option_2)
        db_session.flush()

        db_session.add_all(
            [
                ProductPlatformMap(
                    product_option_id=keep_option.id,
                    platform_id=platform.id,
                    platform_option_id="ITEM-KEEP-1",
                    platform_product_id="GROUP-X",
                    seller_product_code="SELLER-KEEP-1",
                ),
                ProductPlatformMap(
                    product_option_id=keep_option_2.id,
                    platform_id=platform.id,
                    platform_option_id="ITEM-KEEP-2",
                    platform_product_id="GROUP-X",
                    seller_product_code="SELLER-KEEP-2",
                ),
                # LOSING 쪽 옵션: legacy 자기참조 매핑 "하나만" 존재(정상 매핑 없음)
                ProductPlatformMap(
                    product_option_id=losing_option.id,
                    platform_id=platform.id,
                    platform_option_id="GROUP-X",
                    platform_product_id="GROUP-X",
                    seller_product_code=None,
                ),
            ]
        )
        db_session.flush()

        plan = build_plan(db_session, platform.id, "GROUP-X")

        assert plan is not None
        assert plan.is_safe
        assert plan.keep_product_id == keep_product.id
        assert plan.losing_product_id == losing_product.id
        moved = next(o for o in plan.options_to_move if o.option_id == losing_option.id)
        assert moved.legacy_map_ids == []  # 정상 매핑이 없으므로 삭제 대상에서 제외됨
        assert plan.legacy_map_ids_to_delete == []

    def test_option_with_legacy_and_real_map_deletes_only_legacy(self, db_session):
        """옵션이 legacy 자기참조 매핑 + 정상 실데이터 매핑을 모두 가진 경우 -
        삭제 후에도 정상 매핑이 남으므로 legacy 매핑만 삭제 대상에 포함된다."""
        platform = Platform(code="naver_smartstore", name="네이버", connector_class="NaverSmartstoreConnector")
        db_session.add(platform)
        db_session.flush()

        keep_product, keep_option = _make_product_with_option(db_session, name="유지 상품", sku="SKU-KEEP-3")
        keep_option_2 = ProductOption(product_id=keep_product.id, sku_code="SKU-KEEP-4", is_active=True)
        db_session.add(keep_option_2)
        db_session.flush()
        losing_product, losing_option = _make_product_with_option(db_session, name="제거될 상품", sku="SKU-LOSE-2")

        db_session.add_all(
            [
                ProductPlatformMap(
                    product_option_id=keep_option.id,
                    platform_id=platform.id,
                    platform_option_id="ITEM-KEEP-3",
                    platform_product_id="GROUP-Y",
                    seller_product_code="SELLER-KEEP-3",
                ),
                ProductPlatformMap(
                    product_option_id=keep_option_2.id,
                    platform_id=platform.id,
                    platform_option_id="ITEM-KEEP-4",
                    platform_product_id="GROUP-Y",
                    seller_product_code="SELLER-KEEP-4",
                ),
                # LOSING 쪽 옵션: legacy 자기참조 매핑 + 정상 실데이터 매핑 둘 다 존재
                ProductPlatformMap(
                    product_option_id=losing_option.id,
                    platform_id=platform.id,
                    platform_option_id="GROUP-Y",
                    platform_product_id="GROUP-Y",
                    seller_product_code=None,
                ),
                ProductPlatformMap(
                    product_option_id=losing_option.id,
                    platform_id=platform.id,
                    platform_option_id="ITEM-LOSE-2-REAL",
                    platform_product_id="GROUP-Y",
                    seller_product_code="SELLER-LOSE-2",
                ),
            ]
        )
        db_session.flush()

        plan = build_plan(db_session, platform.id, "GROUP-Y")

        assert plan is not None
        assert plan.is_safe
        moved = next(o for o in plan.options_to_move if o.option_id == losing_option.id)
        assert len(moved.legacy_map_ids) == 1
        assert plan.legacy_map_ids_to_delete == moved.legacy_map_ids
