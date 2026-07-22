"""
tests/integration/test_repositories.py
--------------------------------------------
Repository 계층 통합 테스트 - 실제 SQL 실행 결과가 기대한 대로 나오는지
확인한다(단순 위임이 아니라 필터/조인 조건이 올바른지가 핵심).
"""

from datetime import datetime, timezone

from models.customer import Customer
from models.inventory import Inventory
from models.order import Order
from repositories.customer_repository import CustomerRepository
from repositories.inventory_repository import InventoryRepository
from repositories.order_repository import OrderRepository
from repositories.platform_repository import PlatformRepository
from repositories.product_repository import ProductOptionRepository, ProductPlatformMapRepository


class TestOrderRepository:
    def test_get_by_platform_order_no(self, db_session, platform, customer):
        order = Order(
            platform_id=platform.id,
            platform_order_no="ABC-1",
            customer_id=customer.id,
            status="NEW",
            order_date=datetime.now(timezone.utc),
            total_amount=1000,
            discount_amount=0,
        )
        db_session.add(order)
        db_session.flush()

        repo = OrderRepository(db_session)
        found = repo.get_by_platform_order_no(platform.id, "ABC-1")
        assert found is not None
        assert found.id == order.id
        assert repo.get_by_platform_order_no(platform.id, "NOPE") is None

    def test_list_by_status_filters_correctly(self, db_session, platform, customer):
        for i, status in enumerate(["NEW", "NEW", "DELIVERED"]):
            db_session.add(
                Order(
                    platform_id=platform.id,
                    platform_order_no=f"S-{i}",
                    customer_id=customer.id,
                    status=status,
                    order_date=datetime.now(timezone.utc),
                    total_amount=100,
                    discount_amount=0,
                )
            )
        db_session.flush()

        repo = OrderRepository(db_session)
        assert len(repo.list_by_status("NEW")) == 2
        assert len(repo.list_by_status("DELIVERED")) == 1
        assert len(repo.list_by_status("CANCELED")) == 0


class TestCustomerRepository:
    def test_get_by_platform_key(self, db_session, platform):
        c = Customer(platform_id=platform.id, platform_customer_key="KEY-1", name="테스트")
        db_session.add(c)
        db_session.flush()

        repo = CustomerRepository(db_session)
        found = repo.get_by_platform_key(platform.id, "KEY-1")
        assert found is not None and found.id == c.id
        assert repo.get_by_platform_key(platform.id, "NOPE") is None

    def test_list_vip_excludes_non_vip(self, db_session, platform):
        db_session.add(Customer(platform_id=platform.id, platform_customer_key="V1", is_vip=True))
        db_session.add(Customer(platform_id=platform.id, platform_customer_key="V2", is_vip=False))
        db_session.flush()

        vips = CustomerRepository(db_session).list_vip()
        assert [c.platform_customer_key for c in vips] == ["V1"]


class TestInventoryRepository:
    def test_get_by_option_and_warehouse(self, db_session, inventory_row):
        repo = InventoryRepository(db_session)
        found = repo.get_by_option_and_warehouse(inventory_row.product_option_id, inventory_row.warehouse_id)
        assert found is not None and found.id == inventory_row.id

    def test_list_below_safety_stock_only_returns_low_stock(self, db_session, product_option, warehouse):
        low = Inventory(
            product_option_id=product_option.id,
            warehouse_id=warehouse.id,
            sellable_stock=1,
            safety_stock=10,
            reserved_stock=0,
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(low)
        db_session.flush()

        results = InventoryRepository(db_session).list_below_safety_stock()
        assert any(r.id == low.id for r in results)


class TestProductOptionAndPlatformMapRepository:
    def test_get_by_sku_code(self, db_session, product_option):
        repo = ProductOptionRepository(db_session)
        found = repo.get_by_sku_code(product_option.sku_code)
        assert found is not None and found.id == product_option.id
        assert repo.get_by_sku_code("NOPE") is None

    def test_get_by_option_id(self, db_session, platform_map):
        repo = ProductPlatformMapRepository(db_session)
        found = repo.get_by_option_id(platform_map.platform_id, platform_map.platform_option_id)
        assert found is not None and found.id == platform_map.id
        assert repo.get_by_option_id(platform_map.platform_id, "NOPE") is None


class TestBaseRepositoryCrud:
    def test_add_get_count_delete_via_platform_repository(self, db_session, platform):
        repo = PlatformRepository(db_session)

        assert repo.count() == 1
        fetched = repo.get_by_id(platform.id)
        assert fetched is not None and fetched.code == "coupang"

        repo.delete(fetched)
        assert repo.count() == 0
        assert repo.get_by_id(platform.id) is None
