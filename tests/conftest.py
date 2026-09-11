"""
tests/conftest.py
--------------------
pytest 공용 픽스처.

실제 개발 DB(erp.db)는 절대 사용하지 않는다 - 테스트마다 독립된 인메모리
SQLite에 models.Base.metadata로 스키마를 새로 만들어 사용한다. Service/
Repository는 세션을 생성자에서 주입받는 설계라 core.database를 거치지
않고도 그대로 테스트할 수 있다.

아래 두 os.environ 대입은 이 파일의 다른 어떤 import보다도 먼저 실행돼야
한다 - config.settings.Settings()는 모듈이 처음 import될 때 딱 한 번만
생성되고(lru_cache) 그 이후로는 아무리 os.environ이 바뀌어도 값이 갱신되지
않는다. pytest는 이 rootdir conftest.py를 다른 어떤 테스트 모듈/하위
conftest(tests/integration/conftest.py 포함)보다 먼저 임포트하므로, 여기서
os.environ에 "테스트 전용" 값을 직접 대입(대입이지 setdefault가 아님 -
setdefault는 만약 셸에 이미 실제 운영/데모 값이 설정돼 있으면 그걸 그대로
남겨버려 테스트 격리가 깨진다)해두면 이후 api.main.app이 생성되고
validate_startup_secrets()가 실행될 때(예: tests/integration/conftest.py의
`with TestClient(app) as c:`) 항상 이 안전한 dummy 값을 보게 된다.

.env 파일은 건드리지 않는다 - 이 값은 이 pytest 프로세스의 메모리에만
존재하고, 알려진 데모 기본값(CHANGE_ME_IN_PRODUCTION 등)과도 다르며
validate_production_secret()의 최소 길이 기준을 만족하고 서로 다른 값이다.
"""

import os

os.environ["JWT_SECRET_KEY"] = "test-only-dummy-jwt-secret-DO-NOT-USE-IN-PRODUCTION-0000"
os.environ["CREDENTIAL_ENCRYPTION_KEY"] = "test-only-dummy-credential-secret-DO-NOT-USE-IN-PRODUCTION-1111"

from datetime import date, datetime, timezone  # noqa: E402
from typing import Generator  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from config.settings import Settings  # noqa: E402
from models import Base  # noqa: E402
from models.customer import Customer  # noqa: E402
from models.inventory import Inventory, Warehouse  # noqa: E402
from models.platform import Platform, PlatformFeeRule  # noqa: E402
from models.product import Product, ProductOption, ProductPlatformMap  # noqa: E402
from models.supplier import Supplier  # noqa: E402


@pytest.fixture()
def isolated_default_settings(monkeypatch) -> Settings:
    """config.settings.Settings 필드의 "기본값"을 로컬 개발자·운영자의 실제
    .env 파일이나 셸 환경변수와 완전히 무관하게 검증하려는 테스트 전용
    픽스처(예: "OFF가 기본값" 계열 테스트 - tests/unit/test_postgres_backup_service.py
    TestDisabledByDefault 참고).

    왜 필요한가: config.settings.settings는 get_settings()의 @lru_cache로
    프로세스당 정확히 한 번만 만들어지는 모듈 싱글턴이고, 그 인스턴스화 시점에
    실제 저장소 루트의 .env 파일(model_config의 env_file - 애플리케이션의
    정상 운영 동작이라 이 파일 자체는 건드리지 않는다)을 그대로 읽는다. 로컬
    개발/운영 작업 중 그 .env에 예: POSTGRES_BACKUP_ENABLED=true처럼 실제
    운영 승인을 받아 켜둔 플래그가 있으면, "기본값은 False"라고 주장하는
    테스트가 실제 파일 내용을 그대로 보고 실패한다(회귀가 아니라 테스트
    격리 결함 - 개발자마다, 시점마다 결과가 달라진다).

    이 픽스처는 매번 새 Settings 인스턴스를 만들되:
    - `_env_file=None`으로 그 인스턴스만 .env 파일을 읽지 않게 한다
      (pydantic-settings가 공식 지원하는 인스턴스별 오버라이드 - Settings의
      model_config 자체나 애플리케이션이 실제로 쓰는 config.settings.settings
      싱글턴에는 전혀 영향이 없다).
    - Settings의 모든 필드 이름에 대응하는 환경변수(대문자 변환)를 monkeypatch로
      제거해, 혹시 셸에 같은 이름의 환경변수가 실제로 export돼 있어도(파일이
      아니라 셸 환경 오염) 함께 차단한다 - POSTGRES_BACKUP_ENABLED 하나만이
      아니라 모든 필드를 일괄 대상으로 하므로 이후 이런 종류의 "기본값" 테스트가
      늘어나도 같은 오염 위험이 자동으로 차단된다.
    - get_settings()의 lru_cache는 건드리지 않는다 - 이 픽스처가 만드는 것은
      그 캐시와 무관한 완전히 별개의 인스턴스라 초기화할 대상 자체가 없다
      (애플리케이션이 실제로 쓰는 config.settings.settings 싱글턴은 이 테스트
      전후로 조금도 바뀌지 않는다).
    - monkeypatch만 쓰므로 테스트 종료 시 pytest가 환경변수를 자동으로
      원상복구한다(cwd는 이 픽스처가 아예 바꾸지 않는다) - 테스트 실행 순서와
      무관하게 항상 클래스 필드 기본값 그대로를 돌려준다.
    """
    for field_name in Settings.model_fields:
        monkeypatch.delenv(field_name.upper(), raising=False)

    return Settings(_env_file=None)  # type: ignore[call-arg]


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
