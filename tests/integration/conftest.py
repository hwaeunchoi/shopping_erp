"""
tests/integration/conftest.py
----------------------------------
API 통합 테스트 전용 픽스처.

FastAPI의 동기 라우트 핸들러는 스레드풀(run_in_threadpool)에서 실행되므로,
인메모리 SQLite가 요청마다 새 스레드에서 빈 DB로 보이지 않도록 StaticPool로
단일 커넥션을 강제한다(tests/conftest.py의 단순 엔진과 이 부분만 다르다).

get_db 의존성만 오버라이드하고 나머지는 실제 앱(api.main.app) 그대로 사용한다
- 라우터/의존성 로직 자체를 그대로 검증하기 위함이다.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.deps import get_db
from api.main import app
from core.security import hash_password
from models import Base
from models.inventory import Warehouse
from models.platform import Platform
from models.user import Permission, Role, RolePermission, User

ALL_PERMISSION_CODES = [
    "DASHBOARD_VIEW",
    "ORDER_VIEW",
    "ORDER_EDIT",
    "SHIPMENT_VIEW",
    "EXCHANGE_RETURN_MANAGE",
    "SETTLEMENT_VIEW",
    "AD_MANAGE",
    "COST_MANAGE",
    "PRODUCT_MANAGE",
    "SUPPLIER_MANAGE",
    "INVENTORY_VIEW",
    "ANALYTICS_VIEW",
    "REPORT_VIEW",
    "NOTIFICATION_VIEW",
    "SYSTEM_MONITOR_VIEW",
    "SETTINGS_MANAGE",
    "AUDIT_LOG_VIEW",
    "CS_VIEW",
    "CS_MANAGE",
    "CS_ASSIGN",
    "CS_CLOSE",
    "CS_REPLY_SUBMIT",
    "CS_PII_DETAIL",
    "OPERATIONS_RETRY",
    "OPERATIONS_UNKNOWN_RESOLVE",
]


@pytest.fixture()
def api_engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(eng, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def api_session_factory(api_engine):
    return sessionmaker(bind=api_engine, autoflush=False, autocommit=False)


@pytest.fixture()
def client(api_engine, api_session_factory):
    def override_get_db():
        db = api_session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def seed_data(api_session_factory):
    """관리자 계정(전체 권한) + 플랫폼 1개 + 창고 1개를 시딩한다."""
    db = api_session_factory()
    try:
        role = Role(name="Admin")
        db.add(role)
        db.flush()
        for code in ALL_PERMISSION_CODES:
            perm = Permission(code=code, name=code)
            db.add(perm)
            db.flush()
            db.add(RolePermission(role_id=role.id, permission_id=perm.id))

        admin = User(
            username="admin",
            password_hash=hash_password("ChangeMe!123"),
            name="관리자",
            role_id=role.id,
            is_active=True,
        )
        db.add(admin)

        platform = Platform(
            code="coupang", name="쿠팡", connector_class="CoupangConnector", settlement_cycle_days=15, is_active=True
        )
        db.add(platform)
        warehouse = Warehouse(name="본사창고", is_active=True)
        db.add(warehouse)
        db.commit()

        return {"admin_id": admin.id, "platform_id": platform.id, "warehouse_id": warehouse.id}
    finally:
        db.close()


@pytest.fixture()
def auth_headers(client, seed_data):
    resp = client.post("/api/auth/login", data={"username": "admin", "password": "ChangeMe!123"})
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
