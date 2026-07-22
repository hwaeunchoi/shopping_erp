"""
api/routers/orders.py
-------------------------
주문 조회 및 쇼핑몰 커넥터를 통한 주문 수집(sync) 트리거.

/sync는 4~5단계에서 만든 integrations.malls.get_mall_connector()와
services.OrderSyncService를 그대로 호출한다 - API 계층은 여기서도
Repository/커넥터를 직접 다루지 않고 Service만 호출한다.
"""

from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.deps import get_current_user, get_db, require_permission
from integrations.malls import get_mall_connector
from models.extra import Memo
from models.user import User
from repositories.customer_repository import CustomerRepository
from repositories.extra_repository import MemoRepository
from repositories.order_repository import OrderRepository
from repositories.platform_repository import PlatformRepository
from repositories.product_repository import ProductOptionRepository, ProductRepository
from services.exchange_return_service import CancellationService, ExchangeService, OrderRateService, ReturnService
from services.export_service import orders_to_excel
from services.order_sync_service import OrderSyncService
from services.order_view_service import OrderViewService
from services.shipment_service import ShipmentAlreadyExistsError, ShipmentService

router = APIRouter(prefix="/api/orders", tags=["orders"])


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
