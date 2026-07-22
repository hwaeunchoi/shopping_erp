"""
tests/conftest.py
--------------------
pytest 공용 픽스처.

실제 개발 DB(erp.db)는 절대 사용하지 않는다 - 테스트마다 독립된 인메모리
SQLite에 models.Base.metadata로 스키마를 새로 만들어 사용한다. Service/
Repository는 세션을 생성자에서 주입받는 설계라 core.database를 거치지
않고도 그대로 테스트할 수 있다.
"""

from datetime import date, datetime, timezone
from typing import Generator

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from models import Base
from models.customer import Customer
from models.inventory import Inventory, Warehouse
from models.platform import Platform, PlatformFeeRule
from models.product import Product, ProductOption, ProductPlatformMap
from models.supplier import Supplier


@pytest.fixture()
def engine() -> Generator[Engine, None, None]:
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})

    @event.listens_for(eng, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db_session(engine: Engine) -> Generator[Session, None, None]:
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_local()
    yield session
    session.close()


@pytest.fixture()
def platform(db_session) -> Platform:
    p = Platform(
        code="coupang", name="쿠팡", connector_class="CoupangConnector", settlement_cycle_days=15, is_active=True
    )
    db_session.add(p)
    db_session.flush()
    return p


@pytest.fixture()
def naver_platform(db_session) -> Platform:
    """네이버 전용 자동등록 vs 그 외 플랫폼 자동매칭 분기 테스트용 - platform(쿠팡)과 별개."""
    p = Platform(
        code="naver_smartstore",
        name="네이버 스마트스토어",
        connector_class="NaverSmartstoreConnector",
        settlement_cycle_days=7,
        is_active=True,
    )
    db_session.add(p)
    db_session.flush()
    return p


@pytest.fixture()
def platform_fee_rule(db_session, platform) -> PlatformFeeRule:
    rule = PlatformFeeRule(platform_id=platform.id, fee_rate=10.0, effective_from=date(2020, 1, 1), effective_to=None)
    db_session.add(rule)
    db_session.flush()
    return rule


@pytest.fixture()
def warehouse(db_session) -> Warehouse:
    w = Warehouse(name="본사창고", is_active=True)
    db_session.add(w)
    db_session.flush()
    return w


@pytest.fixture()
def product_option(db_session) -> ProductOption:
    product = Product(name="테스트 상품", category="테스트", base_price=10000, status="ACTIVE")
    db_session.add(product)
    db_session.flush()
    option = ProductOption(product_id=product.id, sku_code="TEST-SKU-001", is_active=True)
    db_session.add(option)
    db_session.flush()
    return option


@pytest.fixture()
def second_product_option(db_session) -> ProductOption:
    """product_option과 별개의 상품/옵션 - 매칭 모호성(같은 platform_product_id를
    공유하는 옵션이 2개 이상인 경우) 테스트용."""
    product = Product(name="테스트 상품2", category="테스트", base_price=20000, status="ACTIVE")
    db_session.add(product)
    db_session.flush()
    option = ProductOption(product_id=product.id, sku_code="TEST-SKU-002", is_active=True)
    db_session.add(option)
    db_session.flush()
    return option


@pytest.fixture()
def inventory_row(db_session, product_option, warehouse) -> Inventory:
    inv = Inventory(
        product_option_id=product_option.id,
        warehouse_id=warehouse.id,
        sellable_stock=100,
        reserved_stock=0,
        safety_stock=10,
        updated_at=datetime.now(timezone.utc),
    )
    db_session.add(inv)
    db_session.flush()
    return inv


@pytest.fixture()
def supplier(db_session) -> Supplier:
    s = Supplier(name="테스트공급처", is_active=True)
    db_session.add(s)
    db_session.flush()
    return s


@pytest.fixture()
def customer(db_session, platform) -> Customer:
    c = Customer(platform_id=platform.id, platform_customer_key="CUST-001", name="홍길동")
    db_session.add(c)
    db_session.flush()
    return c


@pytest.fixture()
def platform_map(db_session, platform, product_option) -> ProductPlatformMap:
    m = ProductPlatformMap(
        product_option_id=product_option.id, platform_id=platform.id, platform_option_id="EXT-CODE-1"
    )
    db_session.add(m)
    db_session.flush()
    return m
