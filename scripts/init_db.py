"""
scripts/init_db.py
--------------------
최초 1회 실행하는 DB 초기화 스크립트.

1) 실행에 필요한 디렉터리(logs/, backup/, reports/, reports/generated/,
   reports/templates/)를 생성한다. 이미 존재하면 그대로 넘어간다.
2) models/__init__.py가 import하는 모든 테이블을 생성한다.
   (정식 운영에서는 Alembic 마이그레이션을 우선 사용하는 것을 권장하며,
    이 스크립트는 로컬 개발 환경에서 빠르게 스키마를 맞춰보기 위한 용도이다.)
3) 기본 역할(Admin/Manager/Viewer)과 메뉴 권한(permissions)을 생성한다.
4) 기본 관리자 계정을 생성한다. (최초 로그인 후 반드시 비밀번호를 변경할 것)
5) 5개 쇼핑몰 플랫폼과 기본 창고 1건을 생성한다.
   (광고 플랫폼은 별도 테이블 없이 ad_campaigns.ad_platform_code 등에서
   문자열 코드로만 관리되므로 이 스크립트에서 시딩할 대상이 아니다.)

실행 방법 (Windows, 프로젝트 루트에서):
    python -m venv venv
    venv\\Scripts\\activate
    pip install -r requirements.txt
    copy .env.example .env      (내용 확인 후 필요한 값 수정)
    python scripts\\init_db.py
"""

import sys
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging_config import setup_logging  # noqa: E402
from config.settings import settings  # noqa: E402
from core.database import engine, session_scope  # noqa: E402
from core.security import hash_password  # noqa: E402
from models import Base  # noqa: E402
from models.inventory import Warehouse  # noqa: E402
from models.platform import Platform, PlatformFeeRule  # noqa: E402
from models.user import Permission, Role, RolePermission, User  # noqa: E402

# 화면(메뉴) 단위 기본 권한 정의 - UI와이어프레임 v1.0~v1.2의 사이드바 메뉴와 1:1 대응
DEFAULT_PERMISSIONS = [
    ("DASHBOARD_VIEW", "대시보드 조회", "대시보드"),
    ("ORDER_VIEW", "주문 조회", "주문관리"),
    ("ORDER_EDIT", "주문 수정", "주문관리"),
    ("SHIPMENT_VIEW", "배송 조회", "배송관리"),
    ("EXCHANGE_RETURN_MANAGE", "교환/반품/취소 관리", "교환/반품"),
    ("SETTLEMENT_VIEW", "정산 조회", "정산관리"),
    ("AD_MANAGE", "광고 관리", "광고관리"),
    ("COST_MANAGE", "비용 관리", "비용관리"),
    ("PRODUCT_MANAGE", "상품 관리", "상품관리"),
    ("SUPPLIER_MANAGE", "공급처 관리", "공급처관리"),
    ("INVENTORY_VIEW", "재고 조회", "재고관리"),
    ("ANALYTICS_VIEW", "매출/손익 분석 조회", "매출/손익분석"),
    ("REPORT_VIEW", "보고서 조회/생성", "보고서"),
    ("NOTIFICATION_VIEW", "알림센터 조회", "알림센터"),
    ("SYSTEM_MONITOR_VIEW", "시스템 모니터링 조회", "시스템모니터링"),
    ("SETTINGS_MANAGE", "환경설정 관리", "설정"),
    ("AUDIT_LOG_VIEW", "감사로그 조회", "설정"),
]

# 역할별 기본 권한 매핑 - Admin은 전체, Manager는 설정/감사로그 제외, Viewer는 조회만
ROLE_PERMISSION_MAP = {
    "Admin": [code for code, _, _ in DEFAULT_PERMISSIONS],
    "Manager": [code for code, _, _ in DEFAULT_PERMISSIONS if code not in ("SETTINGS_MANAGE", "AUDIT_LOG_VIEW")],
    "Viewer": [code for code, _, _ in DEFAULT_PERMISSIONS if code.endswith("_VIEW")],
}

# is_active: 이 플랫폼을 신규 설치 시 기본으로 켜둘지 여부. integrations/malls의
# SUPPORTED_CONNECTORS(실 API 연동이 검증된 채널)와 일치시켜 네이버·쿠팡만 True로
# 둔다 - ESM/11번가/카카오쇼핑은 아직 더미 커넥터라 신규 설치에서부터 활성화된
# 것처럼 보이면 안 된다. 다른 모듈의 커넥터 클래스 집합을 참조해 자동 추론하지
# 않고 여기 명시적인 값으로 고정한다(초기화 스크립트의 의존성·순환 import를
# 늘리지 않기 위함 - integrations/malls는 반대로 이 모듈을 참조하지 않는다).
DEFAULT_PLATFORMS = [
    # code, name, connector_class, settlement_cycle_days, is_active
    ("naver_smartstore", "네이버 스마트스토어", "NaverSmartstoreConnector", 7, True),
    ("coupang", "쿠팡", "CoupangConnector", 15, True),
    ("esm", "ESM(G마켓/옥션)", "EsmConnector", 14, False),
    ("elevenst", "11번가", "ElevenstConnector", 14, False),
    ("kakao_shopping", "카카오쇼핑", "KakaoShoppingConnector", 7, False),
]

# 플랫폼별 기본 수수료율(%) - platform_fee_rules에 최초 1건씩 시딩한다.
# services.ProfitCalculationService가 이 테이블을 조회해 손익 계산에 사용한다
# (fee_rate 컬럼은 백분율로 저장, 예: 3.50 = 3.5%). effective_to를 비워두어
# 별도 개정 이력이 생기기 전까지는 무기한 적용된다.
DEFAULT_PLATFORM_FEE_RATES = {
    "naver_smartstore": 3.5,
    "coupang": 10.0,
    "esm": 12.0,
    "elevenst": 12.0,
    "kakao_shopping": 3.5,
}
PLATFORM_FEE_RATE_EFFECTIVE_FROM = date(2020, 1, 1)


def init_directories() -> None:
    """실행에 필요한 디렉터리를 생성한다. 이미 존재하면 건너뛴다."""
    dirs = [
        settings.logs_dir,
        settings.backup_dir,
        settings.reports_dir,
        settings.reports_dir / "generated",
        settings.reports_dir / "templates",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
    print(f"[OK] 디렉터리 확인/생성 완료 ({len(dirs)}개)")


def init_schema() -> None:
    """모든 테이블 생성 (개발용 빠른 부트스트랩)."""
    Base.metadata.create_all(bind=engine)
    print(f"[OK] 테이블 생성 완료 (총 {len(Base.metadata.tables)}개)")


def seed_roles_and_permissions() -> None:
    with session_scope() as db:
        if db.execute(select(func.count()).select_from(Role)).scalar_one() > 0:
            print("[SKIP] 역할/권한 데이터가 이미 존재합니다.")
            return

        roles: dict[str, Role] = {}
        for role_name in ("Admin", "Manager", "Viewer"):
            role = Role(name=role_name)
            db.add(role)
            roles[role_name] = role
        db.flush()  # role.id 확보

        permissions: dict[str, Permission] = {}
        for code, name, menu_group in DEFAULT_PERMISSIONS:
            perm = Permission(code=code, name=name, menu_group=menu_group)
            db.add(perm)
            permissions[code] = perm
        db.flush()

        for role_name, perm_codes in ROLE_PERMISSION_MAP.items():
            for code in perm_codes:
                db.add(RolePermission(role_id=roles[role_name].id, permission_id=permissions[code].id))

        print("[OK] 역할 3종(Admin/Manager/Viewer) 및 권한 매핑 생성 완료")


def seed_default_admin() -> None:
    with session_scope() as db:
        existing_admin = db.execute(select(User).where(User.username == "admin")).scalar_one_or_none()
        if existing_admin:
            print("[SKIP] admin 계정이 이미 존재합니다.")
            return

        admin_role = db.execute(select(Role).where(Role.name == "Admin")).scalar_one_or_none()
        if not admin_role:
            raise RuntimeError("Admin 역할이 없습니다. seed_roles_and_permissions()를 먼저 실행하세요.")

        admin = User(
            username="admin",
            password_hash=hash_password("ChangeMe!123"),  # 최초 로그인 후 반드시 변경
            name="관리자",
            role_id=admin_role.id,
            is_active=True,
        )
        db.add(admin)
        print("[OK] 기본 관리자 계정 생성 완료 (username=admin / password=ChangeMe!123 - 로그인 후 반드시 변경하세요)")


def seed_platforms(db: Session) -> None:
    """신규 설치(플랫폼 테이블이 완전히 비어 있을 때)에서만 DEFAULT_PLATFORMS를
    시딩한다. 테이블에 행이 하나라도 있으면 아무것도 추가·수정하지 않고 그대로
    반환한다 - 기존 운영 DB에서 사용자가 바꾼 is_active 값을 이 함수가 되돌릴
    방법 자체가 없다(플랫폼별 upsert가 아니라 테이블 전체 단위 가드).

    세션의 commit/rollback은 호출자 책임이다(main()의 session_scope() 참고) -
    이 함수는 add()만 하고 커밋하지 않는다."""
    if db.execute(select(func.count()).select_from(Platform)).scalar_one() > 0:
        print("[SKIP] 플랫폼 데이터가 이미 존재합니다.")
        return
    for code, name, connector_class, cycle_days, is_active in DEFAULT_PLATFORMS:
        db.add(
            Platform(
                code=code,
                name=name,
                connector_class=connector_class,
                settlement_cycle_days=cycle_days,
                is_active=is_active,
            )
        )
    print(f"[OK] 쇼핑몰 플랫폼 {len(DEFAULT_PLATFORMS)}개 생성 완료")


def seed_platform_fee_rules() -> None:
    with session_scope() as db:
        if db.execute(select(func.count()).select_from(PlatformFeeRule)).scalar_one() > 0:
            print("[SKIP] 플랫폼 수수료율 데이터가 이미 존재합니다.")
            return

        platforms = db.execute(select(Platform)).scalars().all()
        if not platforms:
            print("[SKIP] 플랫폼 데이터가 없어 수수료율을 시딩할 수 없습니다. seed_platforms()를 먼저 실행하세요.")
            return

        created = 0
        for platform in platforms:
            fee_rate = DEFAULT_PLATFORM_FEE_RATES.get(platform.code)
            if fee_rate is None:
                continue
            db.add(
                PlatformFeeRule(
                    platform_id=platform.id,
                    fee_rate=fee_rate,
                    effective_from=PLATFORM_FEE_RATE_EFFECTIVE_FROM,
                    effective_to=None,
                )
            )
            created += 1
        print(f"[OK] 플랫폼 수수료율 {created}건 생성 완료")


def seed_default_warehouse() -> None:
    with session_scope() as db:
        if db.execute(select(func.count()).select_from(Warehouse)).scalar_one() > 0:
            print("[SKIP] 창고 데이터가 이미 존재합니다.")
            return
        db.add(Warehouse(name="본사창고", location=None, is_active=True))
        print("[OK] 기본 창고 1건 생성 완료")


def main() -> None:
    init_directories()
    setup_logging()
    print(f"[{datetime.now(timezone.utc).isoformat()}] DB 초기화를 시작합니다.")
    init_schema()
    seed_roles_and_permissions()
    seed_default_admin()
    with session_scope() as db:
        seed_platforms(db)
    seed_platform_fee_rules()
    seed_default_warehouse()
    print("DB 초기화가 완료되었습니다.")


if __name__ == "__main__":
    main()
