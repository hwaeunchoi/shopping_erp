"""
api/routers/orders.py
-------------------------
주문 조회 및 쇼핑몰 커넥터를 통한 주문 수집(sync) 트리거.

/sync는 4~5단계에서 만든 integrations.malls.get_mall_connector()와
services.OrderSyncService를 그대로 호출한다 - API 계층은 여기서도
Repository/커넥터를 직접 다루지 않고 Service만 호출한다.
"""

import logging
import uuid
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from api.deps import get_current_user, get_db, require_permission
from config.settings import settings
from integrations.malls import get_mall_connector
from integrations.malls.errors import RequestBudget
from models.extra import Memo
from models.platform import Platform
from models.user import User
from repositories.customer_repository import CustomerRepository
from repositories.extra_repository import MemoRepository
from repositories.order_repository import OrderRepository
from repositories.platform_repository import PlatformRepository
from repositories.product_repository import ProductOptionRepository, ProductRepository
from services.claim_sync_lock import claim_sync_platform_lock
from services.claim_sync_service import ClaimSyncLimits, ClaimSyncService
from services.exchange_return_service import CancellationService, ExchangeService, OrderRateService, ReturnService
from services.export_service import orders_to_excel
from services.order_sync_service import OrderSyncService
from services.order_view_service import OrderViewService
from services.shipment_service import ShipmentAlreadyExistsError, ShipmentService

router = APIRouter(prefix="/api/orders", tags=["orders"])

logger = logging.getLogger(__name__)


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    platform_id: int
    platform_order_no: str
    customer_id: Optional[int]
    status: str
    order_date: datetime
    total_amount: float
    discount_amount: float
    platform_name: Optional[str] = None
    customer_name: Optional[str] = None


class OrderItemOut(BaseModel):
    id: int
    product_option_id: int
    quantity: int
    unit_price: float
    line_amount: float
    product_name: Optional[str] = None
    option_name: Optional[str] = None
    sku_code: Optional[str] = None


class OrderDetailOut(OrderOut):
    items: list[OrderItemOut] = []
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None


class MemoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    target_type: str
    target_id: int
    content: str
    created_by: Optional[int]
    created_at: datetime


class MemoCreate(BaseModel):
    content: str


class BulkShipItem(BaseModel):
    order_id: int
    carrier: Optional[str] = None
    tracking_no: Optional[str] = None


class BulkShipRequest(BaseModel):
    items: list[BulkShipItem]


class BulkStatusRequest(BaseModel):
    order_ids: list[int]
    status: str
    warehouse_id: Optional[int] = None


class BulkMemoRequest(BaseModel):
    order_ids: list[int]
    content: str


class BulkAssignRequest(BaseModel):
    order_ids: list[int]
    assignee_id: Optional[int] = None


class CsCreateRequest(BaseModel):
    type: str  # EXCHANGE / RETURN / CANCEL
    reason: Optional[str] = None
    order_item_id: Optional[int] = None
    refund_amount: Optional[float] = None


class BulkResult(BaseModel):
    succeeded: int
    failed: int
    errors: list[str] = []


class OrderSyncRequest(BaseModel):
    platform_id: int
    warehouse_id: int
    start_date: date
    end_date: date


# 실계정 첫 검증 프리셋(live_verify)의 코드 강제 범위 - 반품 상태 4종 x 1일 x 1페이지 x retry 0 + 교환 1 = 최대 5회.
LIVE_VERIFY_FEATURES = frozenset({"returns", "exchanges"})
LIVE_VERIFY_MAX_REQUESTS = 5

# 취소 단건 조회 전용 실계정 검증 프리셋(live_verify_cancellations) - 후보 주문 최대 2건 x 1회(retry 0, 페이지 1) = 최대 2회.
# 요청 본문으로는 이 값을 바꿀 수 없다.
LIVE_VERIFY_CANCELLATION_MAX_ORDERS = 2
LIVE_VERIFY_CANCELLATION_MAX_REQUESTS = 2


class ClaimSyncRequest(OrderSyncRequest):
    """교환·반품(클레임) 수동 수집 요청. live_verify=True는 실계정 첫 검증용 프리셋이다 - 서버가 범위를 코드로 강제한다:
    반품·교환만(취소 제외), 조회기간 정확히 1일, 쿼리당 1페이지, retry 0, 요청 최대 5회. 다른 값을 줘도 더 느슨해지지 않는다."""

    live_verify: bool = False
    live_verify_cancellations: bool = False
    """취소 "후보 주문 단건 조회"만 검증하는 프리셋 - 서버가 후보 최대 2건·페이지 1·retry 0·요청 최대 2회로 고정하고 반품·교환·
    정산은 호출하지 않는다(요청 본문의 기간 값은 이 프리셋에서 사용하지 않는다). live_verify와 함께 켤 수 없다."""

    @model_validator(mode="after")
    def _presets_are_mutually_exclusive(self) -> "ClaimSyncRequest":
        if self.live_verify and self.live_verify_cancellations:
            raise ValueError("live_verify와 live_verify_cancellations는 동시에 사용할 수 없습니다.")
        return self


class OrderSyncResult(BaseModel):
    total: int
    created: int
    updated: int
    skipped_items: int
    auto_matched_products: int


class OrderRowOut(BaseModel):
    """주문관리 목록 행 - 파생 상태(결제/배송/CS) + 공급/채널/배송 정보 포함."""

    id: int
    platform_id: int
    platform_name: Optional[str]
    platform_order_no: str
    order_status: str
    payment_status: str
    shipping_status: str
    cs_status: str
    order_date: datetime
    payment_date: Optional[datetime]
    delivery_completed_date: Optional[datetime]
    is_delayed: bool
    customer_name: Optional[str]
    customer_phone: Optional[str]
    customer_address: Optional[str]
    product_name: Optional[str]
    option_name: Optional[str]
    sku_code: Optional[str]
    channel_product_no: Optional[str]
    channel_option_no: Optional[str]
    item_count: int
    total_quantity: int
    supplier_name: Optional[str]
    carrier: Optional[str]
    tracking_no: Optional[str]
    total_amount: float
    assignee_id: Optional[int]
    assignee_name: Optional[str]
    tags: Optional[str]


class OrderListOut(BaseModel):
    rows: list[OrderRowOut]
    total: int
    page: int
    page_size: int


class OrderRateOut(BaseModel):
    order_count: int
    exchange_count: int
    exchange_rate: float
    return_count: int
    return_rate: float
    cancellation_count: int
    cancellation_rate: float


class OrderAlertsOut(BaseModel):
    unshipped_count: int
    delayed_unshipped_count: int
    exchange_pending_count: int
    return_pending_count: int
    cancellation_pending_count: int


@router.get(
    "",
    response_model=OrderListOut,
    dependencies=[Depends(require_permission("ORDER_VIEW"))],
    summary="주문 목록 조회(주문관리 화면)",
    description="통합검색(keyword: 주문번호/송장번호/고객명/연락처/주소/상품명/옵션/SKU/공급처/메모)과 "
    "기간/쇼핑몰/주문상태/배송상태/공급처/SKU 필터를 지원한다. 각 행에 결제/배송/CS 파생 상태와 "
    "공급처/채널번호/택배사 정보를 포함한다. 페이지네이션(page/page_size). 필요 권한: ORDER_VIEW",
)
def list_orders(
    status_filter: Optional[str] = None,
    platform_id: Optional[int] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    keyword: Optional[str] = None,
    shipping_status: Optional[str] = None,
    supplier_id: Optional[int] = None,
    sku: Optional[str] = None,
    cs_status: Optional[str] = None,
    assignee_id: Optional[int] = None,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
) -> OrderListOut:
    repo = OrderRepository(db)
    common = {
        "status": status_filter,
        "platform_id": platform_id,
        "start_date": start_date,
        "end_date": end_date,
        "keyword": keyword,
        "shipping_status": shipping_status,
        "supplier_id": supplier_id,
        "sku": sku,
        "cs_status": cs_status,
        "assignee_id": assignee_id,
    }
    total = repo.count_filtered(**common)  # type: ignore[arg-type]
    orders = repo.list_filtered(**common, limit=page_size, offset=(page - 1) * page_size)  # type: ignore[arg-type]
    rows = [OrderRowOut(**r) for r in OrderViewService(db).build_rows(orders)]
    return OrderListOut(rows=rows, total=total, page=page, page_size=page_size)


@router.get(
    "/rates",
    response_model=OrderRateOut,
    dependencies=[Depends(require_permission("ORDER_VIEW"))],
    summary="교환율/반품율/취소율 조회",
    description="start_date~end_date 기간의 전체 주문건수 대비 교환/반품/취소 신청 비율(%)을 계산한다. "
    "기간을 지정하지 않으면 전체 기간을 대상으로 한다. 필요 권한: ORDER_VIEW",
)
def get_order_rates(
    start_date: Optional[date] = None, end_date: Optional[date] = None, db: Session = Depends(get_db)
) -> dict:
    return OrderRateService(db).calculate_rates(start_date=start_date, end_date=end_date)


@router.get(
    "/alerts",
    response_model=OrderAlertsOut,
    dependencies=[Depends(require_permission("DASHBOARD_VIEW"))],
    summary="대시보드 경고 카드(미배송/교환대기/반품대기/취소대기 건수)",
    description="현재 시점 기준 스냅샷이다(기간 필터 없음). SRS FR-DASH-01 대응. 필요 권한: DASHBOARD_VIEW",
)
def get_order_alerts(db: Session = Depends(get_db)) -> dict:
    return OrderRateService(db).pending_alerts()


@router.get(
    "/export",
    dependencies=[Depends(require_permission("ORDER_VIEW"))],
    summary="주문 목록 엑셀 내보내기",
    description="status_filter/platform_id/start_date~end_date 조건에 맞는 주문 목록을 xlsx 파일로 "
    "다운로드한다(최대 10,000건). SRS FR-ORD-05 대응. 필요 권한: ORDER_VIEW",
)
def export_orders(
    status_filter: Optional[str] = None,
    platform_id: Optional[int] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    keyword: Optional[str] = None,
    shipping_status: Optional[str] = None,
    supplier_id: Optional[int] = None,
    sku: Optional[str] = None,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    orders = OrderRepository(db).list_filtered(
        status=status_filter,
        platform_id=platform_id,
        start_date=start_date,
        end_date=end_date,
        keyword=keyword,
        shipping_status=shipping_status,
        supplier_id=supplier_id,
        sku=sku,
        limit=10000,
    )
    buffer = orders_to_excel(orders)
    filename = f"orders_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.post(
    "/bulk/ship",
    response_model=BulkResult,
    dependencies=[Depends(require_permission("ORDER_EDIT"))],
    summary="송장 일괄 등록",
    description="선택 주문에 택배사/송장번호를 등록하고 배송상태를 SHIPPING으로 전환한다. 필요 권한: ORDER_EDIT",
)
def bulk_ship(payload: BulkShipRequest, db: Session = Depends(get_db)) -> BulkResult:
    order_repo = OrderRepository(db)
    service = ShipmentService(db)
    succeeded, errors = 0, []
    for item in payload.items:
        try:
            if order_repo.get_by_id(item.order_id) is None:
                raise ValueError(f"주문 없음: {item.order_id}")
            shipment = service.shipment_repo.get_by_order(item.order_id)
            if shipment is None:
                shipment = service.create(item.order_id, item.carrier, item.tracking_no)
            else:
                service.update_info(shipment, carrier=item.carrier, tracking_no=item.tracking_no)
            service.change_status(shipment, "SHIPPING")
            succeeded += 1
        except (ValueError, ShipmentAlreadyExistsError) as e:
            errors.append(f"order {item.order_id}: {e}")
    db.commit()
    return BulkResult(succeeded=succeeded, failed=len(errors), errors=errors)


@router.post(
    "/bulk/status",
    response_model=BulkResult,
    dependencies=[Depends(require_permission("ORDER_EDIT"))],
    summary="주문상태 일괄 변경",
    description="선택 주문의 상태를 일괄 변경하고 이력을 남긴다(재고 반영 포함). 필요 권한: ORDER_EDIT",
)
def bulk_status(payload: BulkStatusRequest, db: Session = Depends(get_db)) -> BulkResult:
    order_repo = OrderRepository(db)
    sync = OrderSyncService(db)
    succeeded, errors = 0, []
    for order_id in payload.order_ids:
        order = order_repo.get_by_id(order_id)
        if order is None:
            errors.append(f"order {order_id}: 주문 없음")
            continue
        try:
            sync.apply_status_change(order, payload.status, payload.warehouse_id)
            succeeded += 1
        except Exception as e:  # noqa: BLE001 - 개별 주문 실패가 전체를 막지 않도록 격리
            errors.append(f"order {order_id}: {e}")
    db.commit()
    return BulkResult(succeeded=succeeded, failed=len(errors), errors=errors)


@router.post(
    "/bulk/memo",
    response_model=BulkResult,
    dependencies=[Depends(require_permission("ORDER_EDIT"))],
    summary="메모 일괄 추가",
    description="선택 주문에 동일 메모를 일괄 등록한다. 필요 권한: ORDER_EDIT",
)
def bulk_memo(
    payload: BulkMemoRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> BulkResult:
    order_repo = OrderRepository(db)
    memo_repo = MemoRepository(db)
    succeeded, errors = 0, []
    for order_id in payload.order_ids:
        if order_repo.get_by_id(order_id) is None:
            errors.append(f"order {order_id}: 주문 없음")
            continue
        memo_repo.add(
            Memo(
                target_type="ORDER",
                target_id=order_id,
                content=payload.content,
                created_by=current_user.id,
                created_at=datetime.now(timezone.utc),
            )
        )
        succeeded += 1
    db.commit()
    return BulkResult(succeeded=succeeded, failed=len(errors), errors=errors)


@router.patch(
    "/bulk/assign",
    response_model=BulkResult,
    dependencies=[Depends(require_permission("ORDER_EDIT"))],
    summary="담당자 일괄 지정",
    description="선택 주문의 담당자(assignee_id)를 일괄 지정한다. null이면 담당자 해제. 필요 권한: ORDER_EDIT",
)
def bulk_assign(payload: BulkAssignRequest, db: Session = Depends(get_db)) -> BulkResult:
    order_repo = OrderRepository(db)
    succeeded, errors = 0, []
    for order_id in payload.order_ids:
        order = order_repo.get_by_id(order_id)
        if order is None:
            errors.append(f"order {order_id}: 주문 없음")
            continue
        order.assignee_id = payload.assignee_id
        succeeded += 1
    db.commit()
    return BulkResult(succeeded=succeeded, failed=len(errors), errors=errors)


@router.post(
    "/{order_id}/cs",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("ORDER_EDIT"))],
    summary="주문 워크벤치에서 CS(교환/반품/취소) 접수",
    description="교환·반품·취소를 별도 화면이 아니라 주문 상세에서 바로 접수한다(기존 CS 서비스에 위임). "
    "type=EXCHANGE/RETURN/CANCEL. 필요 권한: ORDER_EDIT",
    responses={404: {"description": "주문을 찾을 수 없습니다."}, 400: {"description": "허용되지 않는 CS 유형"}},
)
def create_order_cs(order_id: int, payload: CsCreateRequest, db: Session = Depends(get_db)) -> dict:
    if OrderRepository(db).get_by_id(order_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="주문을 찾을 수 없습니다.")
    obj_id: int
    obj_status: str
    if payload.type == "EXCHANGE":
        ex = ExchangeService(db).create(order_id, payload.order_item_id, payload.reason)
        obj_id, obj_status = ex.id, ex.status
    elif payload.type == "RETURN":
        ret = ReturnService(db).create(order_id, payload.order_item_id, payload.reason, payload.refund_amount)
        obj_id, obj_status = ret.id, ret.status
    elif payload.type == "CANCEL":
        can = CancellationService(db).create(order_id, payload.reason, payload.refund_amount)
        obj_id, obj_status = can.id, can.status
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"허용되지 않는 CS 유형: {payload.type}")
    db.commit()
    return {"type": payload.type, "id": obj_id, "status": obj_status}


@router.get(
    "/{order_id}/detail",
    dependencies=[Depends(require_permission("ORDER_VIEW"))],
    summary="주문 상세 패널 통합 조회",
    description="주문/품목/재고/배송/고객/메모/로그를 한 응답으로 반환한다(주문관리 상세 패널 전용). "
    "필요 권한: ORDER_VIEW",
    responses={404: {"description": "주문을 찾을 수 없습니다."}},
)
def get_order_detail(order_id: int, db: Session = Depends(get_db)) -> dict:
    order = OrderRepository(db).get_by_id(order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="주문을 찾을 수 없습니다.")
    return OrderViewService(db).build_detail(order)


@router.get(
    "/{order_id}",
    response_model=OrderDetailOut,
    dependencies=[Depends(require_permission("ORDER_VIEW"))],
    summary="주문 상세 조회",
    description="주문 정보와 주문 항목(items) 목록을 함께 반환한다. 필요 권한: ORDER_VIEW",
    responses={404: {"description": "주문을 찾을 수 없습니다."}},
)
def get_order(order_id: int, db: Session = Depends(get_db)) -> OrderDetailOut:
    repo = OrderRepository(db)
    order = repo.get_by_id(order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="주문을 찾을 수 없습니다.")

    option_repo = ProductOptionRepository(db)
    product_repo = ProductRepository(db)
    item_models = []
    for item in repo.list_items(order_id):
        option = option_repo.get_by_id(item.product_option_id)
        product = product_repo.get_by_id(option.product_id) if option else None
        item_models.append(
            OrderItemOut(
                id=item.id,
                product_option_id=item.product_option_id,
                quantity=item.quantity,
                unit_price=float(item.unit_price),
                line_amount=float(item.line_amount),
                product_name=product.name if product else None,
                option_name=option.option_name if option else None,
                sku_code=option.sku_code if option else None,
            )
        )

    customer = CustomerRepository(db).get_by_id(order.customer_id) if order.customer_id else None
    platform = PlatformRepository(db).get_by_id(order.platform_id)
    return OrderDetailOut(
        **OrderOut.model_validate(order).model_dump(exclude={"platform_name", "customer_name"}),
        items=item_models,
        platform_name=platform.name if platform else None,
        customer_name=customer.name if customer else None,
        customer_phone=customer.phone if customer else None,
    )


@router.get(
    "/{order_id}/memos",
    response_model=list[MemoOut],
    dependencies=[Depends(require_permission("ORDER_VIEW"))],
    summary="주문 메모 목록 조회",
    responses={404: {"description": "주문을 찾을 수 없습니다."}},
)
def list_order_memos(order_id: int, db: Session = Depends(get_db)) -> list[Memo]:
    if OrderRepository(db).get_by_id(order_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="주문을 찾을 수 없습니다.")
    return MemoRepository(db).list_by_target("ORDER", order_id)


@router.post(
    "/{order_id}/memos",
    response_model=MemoOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("ORDER_EDIT"))],
    summary="주문 메모 등록",
    responses={404: {"description": "주문을 찾을 수 없습니다."}},
)
def create_order_memo(
    order_id: int, payload: MemoCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> Memo:
    if OrderRepository(db).get_by_id(order_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="주문을 찾을 수 없습니다.")
    memo = Memo(
        target_type="ORDER",
        target_id=order_id,
        content=payload.content,
        created_by=current_user.id,
        created_at=datetime.now(timezone.utc),
    )
    MemoRepository(db).add(memo)
    db.commit()
    return memo


@router.post(
    "/sync",
    response_model=OrderSyncResult,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_permission("ORDER_EDIT"))],
    summary="쇼핑몰 주문 수집",
    description="지정한 플랫폼의 쇼핑몰 커넥터를 통해 [start_date, end_date] 구간의 주문을 수집하여 "
    "생성/갱신한다. 필요 권한: ORDER_EDIT",
    responses={404: {"description": "플랫폼을 찾을 수 없습니다."}},
)
def sync_orders(payload: OrderSyncRequest, db: Session = Depends(get_db)) -> OrderSyncResult:
    platform = PlatformRepository(db).get_by_id(payload.platform_id)
    if platform is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="플랫폼을 찾을 수 없습니다.")

    connector = get_mall_connector(platform.connector_class, session=db, platform_id=platform.id)
    result = OrderSyncService(db).sync_orders(
        connector, platform.id, payload.warehouse_id, payload.start_date, payload.end_date
    )
    db.commit()
    return OrderSyncResult(**result)


class ClaimFeatureResult(BaseModel):
    """클레임 기능(취소/반품/교환)별 결과. 미지원과 결과 0건을 status로 구분한다."""

    status: str  # SUCCESS | UNSUPPORTED | FAILED
    count: int
    reason_code: Optional[str] = None
    retryable: Optional[bool] = None


class ClaimSyncResult(BaseModel):
    overall_status: str  # SUCCESS | PARTIAL | UNSUPPORTED | FAILED
    cancellations: ClaimFeatureResult
    returns: ClaimFeatureResult
    exchanges: ClaimFeatureResult
    skipped_no_order: int
    requests_used: Optional[int] = None
    verification_level: Optional[str] = None


# 외부 API 실패로 분류되는 안전 reason_code(HTTP 502/503 판정용). CREDENTIAL_MISSING/
# CAPABILITY_UNSUPPORTED/INTERNAL_ERROR/DB_WRITE_FAILED 는 외부 오류가 아니다.
_EXTERNAL_REASONS = {
    "AUTH_FAILED",
    "RATE_LIMITED",
    "SERVER_ERROR",
    "BAD_RESPONSE",
    "TIMEOUT",
    "CONNECT_FAILED",
    "TRANSPORT_ERROR",
    "PARSE_FAILED",
}


def _claim_http_status(result: dict) -> int:
    """구조화된 클레임 결과 -> HTTP 상태. 전 기능 미지원을 200 성공으로 보이지 않게 한다."""
    overall = result["overall_status"]
    if overall in ("SUCCESS", "PARTIAL"):
        return status.HTTP_200_OK
    if overall == "UNSUPPORTED":  # 지원 기능이 하나도 없음
        return status.HTTP_501_NOT_IMPLEMENTED
    # overall == FAILED: 지원 기능이 모두 실패(SUCCESS 없음). 실패 원인으로 상태 결정.
    failed = [result[k] for k in ("cancellations", "returns", "exchanges") if result[k]["status"] == "FAILED"]
    codes = {f["reason_code"] for f in failed}
    if codes == {"CREDENTIAL_MISSING"}:
        return status.HTTP_409_CONFLICT
    if codes and codes <= _EXTERNAL_REASONS:
        return (
            status.HTTP_503_SERVICE_UNAVAILABLE if any(f["retryable"] for f in failed) else status.HTTP_502_BAD_GATEWAY
        )
    # 내부/DB 오류 또는 혼합 원인 - 안전한 기본값(내부 오류)으로 처리한다.
    return status.HTTP_500_INTERNAL_SERVER_ERROR


def _cancellation_verification_level(lookup: dict) -> str:
    """취소 후보 단건 조회 검증이 어디까지 증명했는지 구분한다 - 취소 0건을 저장 경로 검증으로 과장하지 않는다."""
    if lookup.get("status") != "SUCCESS":
        return "NOT_VALIDATED"
    if lookup.get("count", 0) >= 1:
        return "LIVE_CANCEL_PERSISTENCE_VALIDATED"
    if lookup.get("checked", 0) >= 1:
        return "LIVE_CANCEL_AUTH_AND_ENVELOPE_VALIDATED"
    return "NOT_VALIDATED"  # 유효 후보가 없어 외부 요청이 없었다


def _live_verify_cancellations(db: Session, platform: Platform) -> JSONResponse:
    """취소 단건 조회 전용 프리셋: 후보 최대 2건, 페이지 1, retry 0, 요청 최대 2회. 반품·교환·정산은 호출하지 않는다.
    scheduler와 같은 (platform_id, CLAIM_SYNC) 잠금 안에서 실제 운영 서비스 경로(회전 cursor 포함)를 그대로 쓴다."""
    with claim_sync_platform_lock(platform.id, db.get_bind()) as acquired:
        if not acquired:
            running = {"status": "ALREADY_RUNNING", "count": 0, "reason_code": "ALREADY_RUNNING", "retryable": True}
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "overall_status": "ALREADY_RUNNING",
                    "cancellations": running,
                    "returns": running,
                    "exchanges": running,
                    "skipped_no_order": 0,
                },
            )
        connector = get_mall_connector(platform.connector_class, session=db, platform_id=platform.id)
        limits = ClaimSyncLimits(
            max_pages=1,
            max_retries=0,
            budget=RequestBudget(
                max_requests=min(LIVE_VERIFY_CANCELLATION_MAX_REQUESTS, settings.claim_sync_max_requests_per_run)
            ),
        )
        lookup = ClaimSyncService(db).sync_cancellations_by_candidate_orders(
            connector, platform.id, LIVE_VERIFY_CANCELLATION_MAX_ORDERS, limits
        )
        not_requested = {"status": "UNSUPPORTED", "count": 0, "reason_code": "NOT_REQUESTED", "retryable": False}
        result = {
            "overall_status": lookup["status"],
            "requests_used": limits.budget.used,
            "cancellations": lookup,
            "returns": not_requested,
            "exchanges": not_requested,
            "skipped_no_order": 0,
            "resolved_unmatched": 0,
            "candidate_limit": LIVE_VERIFY_CANCELLATION_MAX_ORDERS,
            "verification_level": _cancellation_verification_level(lookup),
        }
        try:
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            logger.warning("취소 검증 커밋 실패: trace=%s", uuid.uuid4().hex[:8])
            failed = {"status": "FAILED", "count": 0, "reason_code": "DB_WRITE_FAILED", "retryable": False}
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={
                    "overall_status": "FAILED",
                    "cancellations": failed,
                    "returns": not_requested,
                    "exchanges": not_requested,
                    "skipped_no_order": 0,
                    "verification_level": "NOT_VALIDATED",
                },
            )
        return JSONResponse(status_code=_claim_http_status(result), content=result)


@router.post(
    "/sync-claims",
    response_model=ClaimSyncResult,
    dependencies=[Depends(require_permission("ORDER_EDIT"))],
    summary="교환/반품/취소 수집",
    description="지정한 플랫폼에서 [start_date, end_date] 구간의 교환/반품/취소를 capability 기준으로 "
    "수집해 이미 수집된 주문에 연결한다(미러링). 기능별 결과(SUCCESS/UNSUPPORTED/FAILED)를 구조화해 "
    "반환하며, HTTP 상태는 전체 결과에 따라 200/409/501/502/503/500이 될 수 있으나 body 형식은 동일하다. "
    "필요 권한: ORDER_EDIT",
    responses={404: {"description": "플랫폼을 찾을 수 없습니다."}},
)
def sync_claims(payload: ClaimSyncRequest, db: Session = Depends(get_db)) -> JSONResponse:
    if not settings.claim_sync_enabled:
        # 실계정 검증 승인 전 기본 차단 - 커넥터를 만들지도, 플랫폼을 조회하지도 않고
        # 즉시 반환한다(외부 호출 0건). 기존 응답 계약(구조화 body + _claim_http_status)
        # 은 그대로 유지한다 - UNSUPPORTED는 이미 501로 매핑되어 있다.
        disabled = {"status": "UNSUPPORTED", "count": 0, "reason_code": "FEATURE_DISABLED", "retryable": False}
        disabled_result = {
            "overall_status": "UNSUPPORTED",
            "cancellations": disabled,
            "returns": disabled,
            "exchanges": disabled,
            "skipped_no_order": 0,
        }
        return JSONResponse(status_code=_claim_http_status(disabled_result), content=disabled_result)

    platform = PlatformRepository(db).get_by_id(payload.platform_id)
    if platform is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="플랫폼을 찾을 수 없습니다.")

    if payload.live_verify_cancellations:
        return _live_verify_cancellations(db, platform)

    limits = None
    features = None
    if payload.live_verify:
        # 실계정 첫 검증 프리셋 - 코드로 범위를 강제한다(반품 4 + 교환 1 = 최대 5회, 취소 제외).
        if (payload.end_date - payload.start_date).days != 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="live_verify는 조회기간이 정확히 1일이어야 합니다.",
            )
        features = LIVE_VERIFY_FEATURES
        limits = ClaimSyncLimits(
            max_pages=1,
            max_retries=0,
            budget=RequestBudget(max_requests=min(LIVE_VERIFY_MAX_REQUESTS, settings.claim_sync_max_requests_per_run)),
        )

    # 같은 플랫폼의 scheduler claim_sync와 같은 잠금 - 겹치면 외부 호출·DB 쓰기 없이 ALREADY_RUNNING으로 끝난다.
    with claim_sync_platform_lock(platform.id, db.get_bind()) as acquired:
        if not acquired:
            running = {"status": "ALREADY_RUNNING", "count": 0, "reason_code": "ALREADY_RUNNING", "retryable": True}
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "overall_status": "ALREADY_RUNNING",
                    "cancellations": running,
                    "returns": running,
                    "exchanges": running,
                    "skipped_no_order": 0,
                },
            )
        # 미검증 채널은 팩토리가 MarketplaceCapabilityUnsupportedError -> 전역 핸들러 501.
        connector = get_mall_connector(platform.connector_class, session=db, platform_id=platform.id)
        # 자동 수집과 같은 호출량 제한(페이지 상한·retry·요청 예산)을 쓴다 - 수동 호출도 무제한 경로가 없다.
        result = ClaimSyncService(db).sync_claims(
            connector, platform.id, payload.start_date, payload.end_date, limits, features
        )
        try:
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            logger.warning("클레임 수집 커밋 실패: trace=%s", uuid.uuid4().hex[:8])
            failed = {"status": "FAILED", "count": 0, "reason_code": "DB_WRITE_FAILED", "retryable": False}
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={
                    "overall_status": "FAILED",
                    "cancellations": failed,
                    "returns": failed,
                    "exchanges": failed,
                    "skipped_no_order": result.get("skipped_no_order", 0),
                },
            )

        # 200/409/501/502/503/500 어떤 경우에도 동일한 구조화 body를 유지한다.
        return JSONResponse(status_code=_claim_http_status(result), content=result)
