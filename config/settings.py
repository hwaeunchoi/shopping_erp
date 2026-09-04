"""
config/settings.py
--------------------
.env 파일을 로드하여 애플리케이션 전역 설정을 노출한다.
pydantic-settings를 사용하여 타입 검증과 기본값을 함께 관리한다.

사용 예:
    from config.settings import settings
    print(settings.database_url)
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # 애플리케이션 기본 정보
    app_name: str = "쇼핑몰 통합 ERP"
    app_version: str = "0.1.0"
    debug: bool = False

    # DB - 초기엔 SQLite, 추후 PostgreSQL 전환 시 이 값만 교체
    # 예: postgresql+psycopg://user:pw@localhost:5432/erp
    database_url: str = f"sqlite:///{BASE_DIR / 'erp.db'}"

    # 인증
    jwt_secret_key: str = "CHANGE_ME_IN_PRODUCTION"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 8  # 8시간

    # API Key 암호화 마스터 키 (Fernet 등 대칭키 암호화에 사용)
    credential_encryption_key: str = "CHANGE_ME_IN_PRODUCTION"

    # 디렉터리 경로
    reports_dir: Path = BASE_DIR / "reports"
    logs_dir: Path = BASE_DIR / "logs"
    backup_dir: Path = BASE_DIR / "backup"

    # 백업 정책
    backup_retention_days: int = 30
    backup_max_count: int = 60

    # 휴면 고객 판정 기준(일) - v1.2 고객분석
    dormant_customer_days: int = 90

    # 상용 ERP 확장(1단계) - 실 채널 쓰기(송장 전송)/상태 재조회 기본 차단.
    # 실계정 검증 승인 전에는 반드시 False로 유지한다(기본값 자체가 False) - 이 값이
    # False면 ShipmentDispatchService.enqueue()/POST /api/shipments/{id}/submit/
    # outbox_dispatch_job/channel_status_sync_job이 모두 실제 채널 호출 없이 안전하게
    # 차단된다. 기존 주문수집(order_collect_job)/네이버 상품동기화(product_sync_job)는
    # 이 플래그와 무관하게 항상 그대로 동작한다(이번 병합으로 자동 활성화되지 않는다).
    shipment_channel_submit_enabled: bool = False
    # 채널 상태 읽기전용 재조회 잡(channel_status_sync_job)의 별도 활성화 스위치 -
    # 이 잡도 실 채널 API(fetch_orders)를 호출하므로 위 플래그와 별개로 기본 차단한다.
    channel_status_sync_enabled: bool = False

    # 상용 ERP 확장(2단계) - 취소/반품/교환 클레임 수집 + 정산(회차 요약/주문단위 상세)
    # 수집을 자동(scheduler.jobs.claim_sync_job/settlement_sync_job) + 수동(POST
    # /api/orders/sync-claims, /api/settlements/sync) 모두 이 플래그로 통제한다.
    # False(기본값)면 실제 채널 호출 없이 안전하게 차단된다 - 실계정 검증 승인 후
    # 운영자가 명시적으로 켜야 한다. 환불 실행/취소 승인/반품 완료 처리/교환
    # 재발송처럼 채널에 "쓰는" 클레임 처리 액션은 이 플래그와 무관하게 이번 단계
    # 범위 밖이다(Stage 2-B, docs/COMMERCIAL_ERP_ROADMAP.md 참고) - 아직 구현되지
    # 않았다.
    claims_settlement_sync_enabled: bool = False

    # 상용 ERP 확장(3단계, 첫 묶음) - 기존 채널 상품(옵션)의 재고 수량/판매상태 전송을
    # 자동(scheduler.jobs.product_sync_dispatch_job) + 수동(POST /api/products/
    # platform-map/{id}/sync-inventory, /sync-sale-status) 모두 이 플래그로 통제한다.
    # False(기본값)면 세션도 열지 않고 커넥터도 만들지 않아 외부 호출이 0건임을
    # 보장한다 - 실계정 검증 승인 후 운영자가 명시적으로 켜야 한다. 신규 상품 등록,
    # 상품명/가격/이미지 등 전체 상품정보 수정, 클레임 승인/환불 등 다른 쓰기 액션은
    # 이 플래그와 무관하게 이번 묶음 범위 밖이다(docs/COMMERCIAL_ERP_ROADMAP.md 참고).
    product_channel_sync_enabled: bool = False

    # 상용 ERP 확장(3단계, 두 번째 묶음) - 옵션 조합 없는 단순 상품의 신규 등록
    # (PRODUCT_CREATE)만 이 플래그로 통제한다. 자동(scheduler.jobs.
    # product_publish_dispatch_job) + 수동(POST /api/products/.../publish-draft/
    # submit) 모두 대상이다. False(기본값)면 실제 채널 호출 없이 안전하게
    # 차단된다 - 실계정 검증 승인 후 운영자가 명시적으로 켜야 한다. 옵션 조합
    # 등록/옵션 구조 변경/대량 등록/자동 가격결정/자동 재고배분/다른 채널 추가는
    # 이번 묶음 범위 밖이다(docs/COMMERCIAL_ERP_ROADMAP.md 참고). 아래
    # product_info_update_enabled 및 위 product_channel_sync_enabled와는 완전히
    # 별개의 독립 플래그다 - 재고 기능을 켰다고 신규 등록까지, 신규 등록을
    # 켰다고 정보수정까지 자동으로 허용되지 않는다(하나를 켜도 나머지 둘은
    # 여전히 OFF로 남는다 - 켜져도 정보수정을 자동 허용해서는 안 된다는
    # 감사 지적 반영).
    product_publish_enabled: bool = False

    # 상용 ERP 확장(3단계, 두 번째 묶음) - 기존 채널 상품의 상품명/판매가/상세설명
    # 중 일부만 수정하는 PRODUCT_INFO_UPDATE 명령만 이 플래그로 통제한다.
    # product_channel_sync_enabled(재고/판매상태)·product_publish_enabled(신규
    # 등록) 어느 쪽을 켜도 이 플래그는 그대로 OFF로 남는다 - 세 플래그 모두
    # 서로 독립이다(services.product_sync_dispatch_service._enqueue,
    # scheduler.jobs.product_sync_dispatch_job._enabled_command_types 참고).
    # False(기본값)면 실제 채널 호출 없이 안전하게 차단된다.
    product_info_update_enabled: bool = False

    # 상용 ERP 확장(3단계, 세 번째 묶음) - 하나의 로컬 상품에 속한 여러 SKU를 채널
    # 옵션 조합 상품 하나로 묶어 등록하는 PRODUCT_OPTION_CREATE 명령만 이 플래그로
    # 통제한다. 위 product_publish_enabled(옵션 조합 없는 단순 상품 등록)와는 완전히
    # 독립이다 - 단순 등록을 켜도 옵션조합 등록은 여전히 OFF로 남고, 그 반대도
    # 마찬가지다(services.product_option_publish_service.ProductOptionPublishService.
    # enqueue_create, scheduler.jobs.product_option_publish_dispatch_job 참고).
    # False(기본값)면 실제 채널 호출 없이 안전하게 차단된다 - 실계정 검증 승인 후
    # 운영자가 명시적으로 켜야 한다.
    product_option_publish_enabled: bool = False

    model_config = SettingsConfigDict(env_file=str(BASE_DIR / ".env"), env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    """설정 객체를 프로세스당 1회만 생성하도록 캐싱한다."""
    return Settings()


settings = get_settings()
