"""
api/routers/products_bulk.py
---------------------------
상용 ERP 확장(3단계, 네 번째 묶음) - 상품/옵션조합 등록과 재고·판매상태·정보수정의
대량 접수·진행상태 조회·실패 재처리. services.product_bulk_service.ProductBulkService의
얇은 API 계층일 뿐이다 - 안전장치(멱등키/supersede/대상 잠금/UNKNOWN 처리/트랜잭션
경계)는 전부 그 서비스와, 그 서비스가 감싸는 기존 단건 서비스에 있다.

기존 단건 엔드포인트(POST .../sync-inventory, POST .../publish-drafts/{id}/submit 등)는
이 라우터와 무관하게 그대로 남아 있다 - 이 라우터는 그 단건 흐름을 복제하지 않고
같은 서비스 계층(ProductBulkService가 내부적으로 각 단건 서비스를 그대로 호출)을
공유한다.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from api.deps import get_current_user, get_db, require_permission
from models.user import User
from repositories.platform_repository import PlatformRepository
from repositories.product_repository import (
    ProductPlatformMapRepository,
    ProductPublishDraftRepository,
    ProductPublishOptionGroupDraftRepository,
)
from services.product_bulk_service import (
    BulkInfoUpdateItem,
    BulkInventoryItem,
    BulkRetryResult,
    BulkSaleStatusItem,
    BulkSubmitResult,
    ProductBulkService,
)

router = APIRouter(
    prefix="/api/products/bulk", tags=["products-bulk"], dependencies=[Depends(require_permission("PRODUCT_MANAGE"))]
)


# --- 선택 화면용 목록 조회 ---


class BulkTargetOut(BaseModel):
    """대량 선택 체크박스 목록의 한 행 - 상품상세를 거치지 않고 채널 매핑/초안을
    직접 고를 수 있게 하는 최소 요약 정보다."""

    id: int
    platform_id: int
    platform_code: str
    product_name: Optional[str] = None
    sku_code: Optional[str] = None
    registered_at: Optional[datetime] = None


class BulkTargetListOut(BaseModel):
    items: list[BulkTargetOut]


def _platform_code_lookup(db, platform_ids: set[int]) -> dict[int, str]:
    """대량 선택 목록 화면에 채널명을 보여주기 위한 platform_id -> code 캐시.
    플랫폼 종류는 소수(네이버/쿠팡 등)라 목록 크기와 무관하게 매번 다시
    조회해도 비용이 작다."""
    repo = PlatformRepository(db)
    return {pid: (p.code if (p := repo.get_by_id(pid)) else "") for pid in platform_ids}


@router.get(
    "/platform-maps",
    response_model=BulkTargetListOut,
    summary="대량 재고/판매상태/정보수정 선택 목록",
    description="현재 채널 매핑 목록을 조회한다(재고/판매상태/정보수정 대량 접수의 선택 대상). "
    "keyword는 SKU코드/판매자상품코드 부분일치로 좁힌다.",
)
def list_bulk_platform_maps(
    platform_id: Optional[int] = None, keyword: Optional[str] = None, limit: int = 200, db=Depends(get_db)
) -> BulkTargetListOut:
    mappings = ProductPlatformMapRepository(db).list_filtered(platform_id=platform_id, keyword=keyword, limit=limit)
    codes = _platform_code_lookup(db, {m.platform_id for m in mappings})
    items = [
        BulkTargetOut(
            id=m.id,
            platform_id=m.platform_id,
            platform_code=codes.get(m.platform_id, ""),
            product_name=m.product_option.product.name if m.product_option else None,
            sku_code=m.product_option.sku_code if m.product_option else None,
        )
        for m in mappings
    ]
    return BulkTargetListOut(items=items)


@router.get(
    "/publish-drafts",
    response_model=BulkTargetListOut,
    summary="대량 신규 등록(단일 SKU) 선택 목록",
    description="현재 등록 초안(ProductPublishDraft) 목록을 조회한다 - 이미 등록 완료된 초안도 "
    "포함하며, 재등록 차단은 접수 시점(대량 접수 API)에서 VALIDATION_FAILED로 알려준다.",
)
def list_bulk_publish_drafts(
    platform_id: Optional[int] = None, limit: int = 200, db=Depends(get_db)
) -> BulkTargetListOut:
    drafts = ProductPublishDraftRepository(db).list_filtered(platform_id=platform_id, limit=limit)
    codes = _platform_code_lookup(db, {d.platform_id for d in drafts})
    items = [
        BulkTargetOut(
            id=d.id,
            platform_id=d.platform_id,
            platform_code=codes.get(d.platform_id, ""),
            product_name=d.product_option.product.name if d.product_option else None,
            sku_code=d.product_option.sku_code if d.product_option else None,
            registered_at=d.registered_at,
        )
        for d in drafts
    ]
    return BulkTargetListOut(items=items)


@router.get(
    "/option-publish-drafts",
    response_model=BulkTargetListOut,
    summary="대량 옵션조합 등록 선택 목록",
    description="현재 옵션조합 등록 초안(ProductPublishOptionGroupDraft) 목록을 조회한다.",
)
def list_bulk_option_publish_drafts(
    platform_id: Optional[int] = None, limit: int = 200, db=Depends(get_db)
) -> BulkTargetListOut:
    drafts = ProductPublishOptionGroupDraftRepository(db).list_filtered(platform_id=platform_id, limit=limit)
    codes = _platform_code_lookup(db, {d.platform_id for d in drafts})
    items = [
        BulkTargetOut(
            id=d.id,
            platform_id=d.platform_id,
            platform_code=codes.get(d.platform_id, ""),
            product_name=d.product.name if d.product else None,
            sku_code=None,
            registered_at=d.registered_at,
        )
        for d in drafts
    ]
    return BulkTargetListOut(items=items)


# --- 대량 접수 결과 공통 응답 ---


class BulkItemResultOut(BaseModel):
    target_id: int
    outcome: str
    command_id: Optional[int] = None
    error_code: Optional[str] = None


class BulkSubmitResultOut(BaseModel):
    items: list[BulkItemResultOut]
    aborted: bool


def _to_submit_result_out(result: BulkSubmitResult) -> BulkSubmitResultOut:
    return BulkSubmitResultOut(
        items=[
            BulkItemResultOut(
                target_id=item.target_id, outcome=item.outcome, command_id=item.command_id, error_code=item.error_code
            )
            for item in result.items
        ],
        aborted=result.aborted,
    )


# --- 대량 접수 요청 ---


class BulkPublishSubmitRequest(BaseModel):
    draft_ids: list[int]


class BulkOptionPublishSubmitRequest(BaseModel):
    group_draft_ids: list[int]


class BulkInventorySubmitItem(BaseModel):
    product_platform_map_id: int
    target_quantity: int


class BulkInventorySubmitRequest(BaseModel):
    items: list[BulkInventorySubmitItem]


class BulkSaleStatusSubmitItem(BaseModel):
    product_platform_map_id: int
    target_status: str


class BulkSaleStatusSubmitRequest(BaseModel):
    items: list[BulkSaleStatusSubmitItem]


class BulkInfoUpdateSubmitItem(BaseModel):
    product_platform_map_id: int
    name: Optional[str] = None
    sale_price: Optional[float] = None
    description: Optional[str] = None


class BulkInfoUpdateSubmitRequest(BaseModel):
    items: list[BulkInfoUpdateSubmitItem]


@router.post(
    "/publish-drafts/submit",
    response_model=BulkSubmitResultOut,
    summary="신규 상품(단일 SKU) 등록 초안 대량 접수",
    description="선택한 여러 등록 초안을 채널별로 일괄 접수한다. 실제 채널 전송은 하지 않고 "
    "outbox 명령만 생성한다(스케줄러가 비동기로 처리) - 일부 항목이 실패해도 나머지 항목의 "
    "접수는 유지된다. 항목별 결과는 ACCEPTED/VALIDATION_FAILED/UNSUPPORTED/BLOCKED_BY_UNKNOWN/"
    "DUPLICATE_OR_SUPERSEDED/FAILED_TO_ENQUEUE 중 하나다.",
)
def submit_bulk_publish_drafts(payload: BulkPublishSubmitRequest, db=Depends(get_db)) -> BulkSubmitResultOut:
    service = ProductBulkService(db)
    result = service.submit_publish_drafts(payload.draft_ids)
    return _to_submit_result_out(result)


@router.post(
    "/option-publish-drafts/submit",
    response_model=BulkSubmitResultOut,
    summary="옵션조합 등록 초안 대량 접수",
    description="submit_bulk_publish_drafts와 동일 원칙 - 옵션조합(그룹 초안) 대상.",
)
def submit_bulk_option_publish_drafts(
    payload: BulkOptionPublishSubmitRequest, db=Depends(get_db)
) -> BulkSubmitResultOut:
    service = ProductBulkService(db)
    result = service.submit_option_publish_drafts(payload.group_draft_ids)
    return _to_submit_result_out(result)


@router.post(
    "/platform-map/sync-inventory/submit",
    response_model=BulkSubmitResultOut,
    summary="재고 수량 대량 전송 접수",
    description="선택한 여러 채널 매핑의 재고 수량을 일괄 접수한다.",
)
def submit_bulk_inventory_updates(payload: BulkInventorySubmitRequest, db=Depends(get_db)) -> BulkSubmitResultOut:
    service = ProductBulkService(db)
    items = [
        BulkInventoryItem(product_platform_map_id=it.product_platform_map_id, target_quantity=it.target_quantity)
        for it in payload.items
    ]
    result = service.submit_inventory_updates(items)
    return _to_submit_result_out(result)


@router.post(
    "/platform-map/sync-sale-status/submit",
    response_model=BulkSubmitResultOut,
    summary="판매상태 대량 전송 접수",
    description="선택한 여러 채널 매핑의 판매상태를 일괄 접수한다.",
)
def submit_bulk_sale_status_updates(payload: BulkSaleStatusSubmitRequest, db=Depends(get_db)) -> BulkSubmitResultOut:
    service = ProductBulkService(db)
    items = [
        BulkSaleStatusItem(product_platform_map_id=it.product_platform_map_id, target_status=it.target_status)
        for it in payload.items
    ]
    result = service.submit_sale_status_updates(items)
    return _to_submit_result_out(result)


@router.post(
    "/platform-map/update-info/submit",
    response_model=BulkSubmitResultOut,
    summary="상품정보(이름/판매가/상세설명) 대량 수정 접수",
    description="선택한 여러 채널 매핑의 정보수정을 일괄 접수한다(지원 범위는 채널별로 다르며, "
    "미지원 채널은 UNSUPPORTED로 표시된다 - 예: 쿠팡은 정보수정 미지원).",
)
def submit_bulk_info_updates(payload: BulkInfoUpdateSubmitRequest, db=Depends(get_db)) -> BulkSubmitResultOut:
    service = ProductBulkService(db)
    items = [
        BulkInfoUpdateItem(
            product_platform_map_id=it.product_platform_map_id,
            name=it.name,
            sale_price=it.sale_price,
            description=it.description,
        )
        for it in payload.items
    ]
    result = service.submit_info_updates(items)
    return _to_submit_result_out(result)


# --- 진행상태 조회 ---


class BulkCommandsStatusRequest(BaseModel):
    command_ids: list[int]


class BulkCommandStatusOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    command_type: str
    target_id: int
    status: str
    attempt_count: int
    retryable: bool
    error_code: Optional[str] = None
    next_retry_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class BulkCommandsStatusResponse(BaseModel):
    items: list[BulkCommandStatusOut]


@router.post(
    "/commands/status",
    response_model=BulkCommandsStatusResponse,
    summary="대량 작업 진행상태 조회",
    description="대량 접수 API가 돌려준 command_id들을 한 번에 조회한다(건별 폴링 불필요). "
    "요청한 id 중 상품 관련 명령이 아닌 것은 결과에서 제외된다.",
)
def get_bulk_commands_status(payload: BulkCommandsStatusRequest, db=Depends(get_db)) -> BulkCommandsStatusResponse:
    service = ProductBulkService(db)
    commands = service.get_commands_status(payload.command_ids)
    return BulkCommandsStatusResponse(
        items=[BulkCommandStatusOut.model_validate(c, from_attributes=True) for c in commands]
    )


# --- 실패 항목 선택 재처리 ---


class BulkRetryRequest(BaseModel):
    command_ids: list[int]


class RetryItemResultOut(BaseModel):
    command_id: int
    outcome: str
    error_code: Optional[str] = None


class BulkRetryResponseOut(BaseModel):
    items: list[RetryItemResultOut]
    aborted: bool


def _to_retry_result_out(result: BulkRetryResult) -> BulkRetryResponseOut:
    return BulkRetryResponseOut(
        items=[
            RetryItemResultOut(command_id=item.command_id, outcome=item.outcome, error_code=item.error_code)
            for item in result.items
        ],
        aborted=result.aborted,
    )


@router.post(
    "/commands/retry",
    response_model=BulkRetryResponseOut,
    summary="실패(FAILED) 명령 선택 재처리",
    description="FAILED 상태 명령만 PENDING으로 되돌려 스케줄러가 다시 시도하게 한다. "
    "UNKNOWN 상태 명령은 이 API로 재처리되지 않는다(UNKNOWN_REQUIRES_RESOLUTION으로 표시) - "
    "POST /api/products/sync-commands/{command_id}/resolve로 운영자가 직접 해소해야 한다.",
)
def retry_bulk_commands(
    payload: BulkRetryRequest, db=Depends(get_db), current_user: User = Depends(get_current_user)
) -> BulkRetryResponseOut:
    service = ProductBulkService(db)
    result = service.retry_commands(payload.command_ids, resolved_by=current_user.id)
    return _to_retry_result_out(result)
