"""
api/main.py
--------------
FastAPI 애플리케이션 엔트리포인트.

실행 방법 (Windows, 프로젝트 루트에서, venv 활성화 후):
    uvicorn api.main:app --reload

Swagger UI: http://127.0.0.1:8000/docs
"""

import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

import api.routers.settings as settings_router
from api.routers import (
    ads,
    ai_assistant,
    alert_rules,
    analytics,
    auth,
    cancellations,
    costs,
    customers,
    exchanges,
    favorites,
    inventory,
    notifications,
    orders,
    platforms,
    products,
    purchase_orders,
    recent_views,
    reports,
    returns,
    search,
    settlements,
    shipments,
    suppliers,
    system_monitor,
    tasks,
)
from config.logging_config import setup_logging
from config.settings import settings
from integrations.malls.errors import (
    MarketplaceCapabilityUnsupportedError,
    MarketplaceCredentialMissingError,
    MarketplaceError,
    MarketplaceExternalAPIError,
)
from services.product_service import OptionInUseError

setup_logging()
logger = logging.getLogger(__name__)

# 각 태그(라우터)가 어떤 화면/권한과 대응되는지 Swagger UI에 노출한다.
# 권한 코드는 scripts/init_db.py의 DEFAULT_PERMISSIONS와 동일하다.
openapi_tags = [
    {"name": "health", "description": "헬스체크"},
    {"name": "auth", "description": "로그인 및 내 정보 조회"},
    {"name": "products", "description": "상품/SKU 조회. 필요 권한: PRODUCT_MANAGE"},
    {"name": "customers", "description": "고객 조회. 로그인한 사용자면 조회 가능 (전용 권한 없음)"},
    {
        "name": "orders",
        "description": "주문 조회/수집. 조회·비율(rates)은 ORDER_VIEW, 수집(sync)은 ORDER_EDIT, "
        "대시보드 경고(alerts)는 DASHBOARD_VIEW 권한 필요",
    },
    {"name": "shipments", "description": "배송 조회/등록/상태변경. 필요 권한: SHIPMENT_VIEW"},
    {"name": "exchanges", "description": "교환 조회/등록/상태변경. 필요 권한: EXCHANGE_RETURN_MANAGE"},
    {"name": "returns", "description": "반품 조회/등록/상태변경. 필요 권한: EXCHANGE_RETURN_MANAGE"},
    {"name": "cancellations", "description": "취소 조회/등록/상태변경. 필요 권한: EXCHANGE_RETURN_MANAGE"},
    {"name": "inventory", "description": "재고 조회. 필요 권한: INVENTORY_VIEW"},
    {"name": "settlements", "description": "정산 조회. 필요 권한: SETTLEMENT_VIEW"},
    {"name": "costs", "description": "비용 조회/등록. 필요 권한: COST_MANAGE"},
    {"name": "ads", "description": "광고 캠페인/일별 성과 조회. 필요 권한: AD_MANAGE"},
    {"name": "analytics", "description": "매출/손익 분석 조회 및 계산 트리거. 필요 권한: ANALYTICS_VIEW"},
    {
        "name": "reports",
        "description": "월별 경영보고서 조회/엑셀·PDF 다운로드, 예약 보고서(report_schedules) 관리. 필요 권한: REPORT_VIEW",
    },
    {"name": "platforms", "description": "쇼핑몰 플랫폼 기준정보 조회. 로그인한 사용자면 조회 가능 (전용 권한 없음)"},
    {
        "name": "search",
        "description": "글로벌 통합검색(주문/상품/SKU/고객/송장). 로그인한 사용자면 조회 가능 (전용 권한 없음)",
    },
    {
        "name": "favorites",
        "description": "즐겨찾기(상품/보고서 등) 조회/토글. 로그인한 사용자면 사용 가능 (전용 권한 없음)",
    },
    {"name": "recent-views", "description": "최근 본 항목 조회/기록. 로그인한 사용자면 사용 가능 (전용 권한 없음)"},
    {
        "name": "system-monitor",
        "description": "시스템 모니터링(연동상태/작업이력/시스템상태). 필요 권한: SYSTEM_MONITOR_VIEW",
    },
    {"name": "ai-assistant", "description": "ERP AI Assistant 질의응답. 로그인한 사용자면 사용 가능 (전용 권한 없음)"},
    {
        "name": "tasks",
        "description": "대시보드 빠른실행(주문수집/광고수집/보고서생성/백업/전체동기화) 트리거. "
        "task_type별로 ORDER_EDIT/AD_MANAGE/REPORT_VIEW/SETTINGS_MANAGE 권한 필요",
    },
    {"name": "notifications", "description": "알림센터 조회/읽음처리. 필요 권한: NOTIFICATION_VIEW"},
    {"name": "alert-rules", "description": "사용자 정의 알림 규칙 관리. 필요 권한: SETTINGS_MANAGE"},
    {
        "name": "settings",
        "description": "설정 화면(사용자/역할/권한/API Credential/시스템설정) 관리. 필요 권한: SETTINGS_MANAGE",
    },
]

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    debug=settings.debug,
    description="쇼핑몰 통합 ERP 백엔드 API. 인증은 OAuth2 password flow(JWT)를 사용하며, "
    "/api/auth/login에서 발급받은 access_token을 Authorization: Bearer 헤더에 담아 호출한다.",
    openapi_tags=openapi_tags,
)

app.include_router(auth.router)
app.include_router(products.router)
app.include_router(customers.router)
app.include_router(orders.router)
app.include_router(shipments.router)
app.include_router(exchanges.router)
app.include_router(returns.router)
app.include_router(cancellations.router)
app.include_router(inventory.router)
app.include_router(suppliers.router)
app.include_router(purchase_orders.router)
app.include_router(settlements.router)
app.include_router(costs.router)
app.include_router(ads.router)
app.include_router(analytics.router)
app.include_router(reports.router)
app.include_router(platforms.router)
app.include_router(search.router)
app.include_router(favorites.router)
app.include_router(recent_views.router)
app.include_router(system_monitor.router)
app.include_router(ai_assistant.router)
app.include_router(tasks.router)
app.include_router(notifications.router)
app.include_router(alert_rules.router)
app.include_router(settings_router.router)


@app.exception_handler(IntegrityError)
def handle_integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
    """DB 제약조건 위반(중복 키, 잘못된 외래키 참조 등)을 500 대신 409로 변환한다.

    원본 예외 메시지는 SQL 구문/내부 스키마를 노출할 수 있어 응답에는 담지
    않고 로그에만 남긴다.
    """
    logger.warning("DB 제약조건 위반: %s", exc, exc_info=False)
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": "데이터 제약조건을 위반했습니다 (중복된 값이거나 잘못된 참조입니다)."},
    )


@app.exception_handler(OptionInUseError)
def handle_option_in_use_error(request: Request, exc: OptionInUseError) -> JSONResponse:
    """이미 주문에서 사용 중인 옵션(SKU)을 삭제하려는 요청을 409로 변환한다."""
    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})


@app.exception_handler(MarketplaceError)
def handle_marketplace_error(request: Request, exc: MarketplaceError) -> JSONResponse:
    """쇼핑몰 연동 오류를 안전한 HTTP 응답으로 변환한다.

    - 인증정보 누락        -> 409 (사용자 조치: 연결정보 확인)
    - 미지원 기능/채널      -> 501
    - 외부 API 오류(재시도O) -> 503
    - 외부 API 오류(재시도X) -> 502
    - 기타 MarketplaceError  -> 500
    응답에는 Secret/Authorization/원본 응답/스택트레이스/개인정보/전체 vendorId·sellerId를
    담지 않는다 - 안전한 error_code·메시지·retryable·platform_code만 노출한다.
    """
    marketplace_code = getattr(exc, "marketplace_code", None)
    if isinstance(exc, MarketplaceCredentialMissingError):
        http_status, error_code, message, retryable = (
            status.HTTP_409_CONFLICT,
            "CREDENTIAL_MISSING",
            "쇼핑몰 연결정보를 확인해 주세요.",
            False,
        )
    elif isinstance(exc, MarketplaceCapabilityUnsupportedError):
        http_status, error_code, message, retryable = (
            status.HTTP_501_NOT_IMPLEMENTED,
            "CAPABILITY_UNSUPPORTED",
            "해당 채널/기능은 아직 지원하지 않습니다.",
            False,
        )
    elif isinstance(exc, MarketplaceExternalAPIError):
        retryable = bool(exc.retryable)
        http_status = status.HTTP_503_SERVICE_UNAVAILABLE if retryable else status.HTTP_502_BAD_GATEWAY
        error_code = "EXTERNAL_API_ERROR"
        message = "쇼핑몰 연동 중 일시적인 오류가 발생했습니다." if retryable else "쇼핑몰 연동 중 오류가 발생했습니다."
    else:
        http_status, error_code, message, retryable = (
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "MARKETPLACE_ERROR",
            "쇼핑몰 연동 처리 중 오류가 발생했습니다.",
            False,
        )
    logger.warning("쇼핑몰 연동 오류: code=%s platform=%s", error_code, marketplace_code)
    return JSONResponse(
        status_code=http_status,
        content={
            "detail": message,
            "error_code": error_code,
            "retryable": retryable,
            "platform_code": marketplace_code,
        },
    )


@app.exception_handler(Exception)
def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """예상하지 못한 예외가 그대로 스택트레이스로 노출되지 않도록 한다.

    HTTPException/RequestValidationError는 FastAPI가 이미 자체 핸들러를
    등록해두었으므로(정확한 타입 매치가 우선) 이 핸들러로 넘어오지 않는다.
    """
    logger.exception("처리되지 않은 예외가 발생했습니다.")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": "서버 내부 오류가 발생했습니다."}
    )


@app.get("/health", tags=["health"], summary="헬스체크", description="서버가 정상적으로 응답하는지 확인한다.")
def health_check() -> dict[str, str]:
    return {"status": "ok"}
