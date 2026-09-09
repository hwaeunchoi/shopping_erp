"""
repositories/order_repository.py
------------------------------------
ERD 2.5 주문/배송/교환/반품/취소 그룹에 대한 Repository.

orders는 order_items/order_status_history/shipment을 소유하는 애그리거트
루트이므로 OrderRepository.list_items()로 함께 다루지만, exchanges/returns/
cancellations/shipments는 "배송관리"/"교환·반품 관리" 화면처럼 주문을 가로질러
목록 조회가 필요하므로 별도 Repository로 둔다.

exchanges/returns/cancellations는 상태/주문/기간/사유검색 필터와 페이지네이션
모양이 동일해(교환·반품·취소 관리 화면 공통 요구사항) _filtered_stmt()로
공통화한다.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional, Union

from sqlalchemy import Select, and_, exists, func, or_, select
from sqlalchemy.orm import Session

from models.customer import Customer
from models.extra import Memo
from models.inventory import Inventory
from models.order import (
    Cancellation,
    ClaimCollectionCursor,
    ClaimUnmatched,
    Exchange,
    Order,
    OrderItem,
    Return,
    Shipment,
    ShipmentItem,
)
from models.product import Product, ProductOption, ProductPlatformMap
from models.supplier import ProductSupplierMap, Supplier
from repositories.base_repository import BaseRepository

RequestDateRangeModel = Union[type[Exchange], type[Return], type[Cancellation]]

# "취소 후보 주문"으로 볼 상태 - 아직 배송이 끝나지 않아 취소될 여지가 있는 주문만
# 대상으로 한다(DELIVERED/CANCELED/EXCHANGED/RETURNED/REFUNDED는 제외 - 이미
# 종결됐거나 취소가 아닌 다른 클레임 경로로 처리될 상태). services.claim_sync_service
# 참고.
CANCELLATION_LOOKUP_CANDIDATE_STATUSES = ["NEW", "PREPARING", "SHIPPING"]


def _filtered_stmt(
    model: RequestDateRangeModel,
    status: Optional[str] = None,
    order_id: Optional[int] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    search: Optional[str] = None,
) -> Select[Any]:
    """exchanges/returns/cancellations 공통 필터(상태/주문/기간/사유검색)를 적용한 select문을 만든다."""
    stmt = select(model)
    if status is not None:
        stmt = stmt.where(model.status == status)
    if order_id is not None:
        stmt = stmt.where(model.order_id == order_id)
    if start_date is not None:
        stmt = stmt.where(model.requested_at >= datetime.combine(start_date, datetime.min.time()))
    if end_date is not None:
        stmt = stmt.where(model.requested_at < datetime.combine(end_date, datetime.min.time()) + timedelta(days=1))
    if search:
        stmt = stmt.where(model.reason.ilike(f"%{search}%"))
    return stmt


class OrderRepository(BaseRepository[Order]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Order)

    def get_by_platform_order_no(self, platform_id: int, platform_order_no: str) -> Optional[Order]:
        """중복 수집 방지(SRS FR-MALL-04)를 위한 조회."""
        stmt = select(Order).where(Order.platform_id == platform_id, Order.platform_order_no == platform_order_no)
        return self.session.execute(stmt).scalar_one_or_none()

    def list_by_status(self, status: str) -> list[Order]:
        stmt = select(Order).where(Order.status == status, Order.is_deleted.is_(False))
        return list(self.session.execute(stmt).scalars().all())

    def list_by_date_range(self, start: datetime, end: datetime) -> list[Order]:
        stmt = select(Order).where(Order.order_date >= start, Order.order_date < end, Order.is_deleted.is_(False))
        return list(self.session.execute(stmt).scalars().all())

    def list_cancellation_lookup_candidates(self, platform_id: int, after_id: int, limit: int) -> list[Order]:
        """취소 "후보 주문 단건 조회"(예: 쿠팡)의 다음 배치를 Order.id 오름차순으로
        가져온다. 매 실행 최대 limit건만 반환해(전체 무제한 순회 금지) 요청 수를
        예산 내로 제한한다 - 회전식 순회는 호출부(ClaimCollectionCursor 갱신)가
        담당한다."""
        stmt = (
            select(Order)
            .where(
                Order.platform_id == platform_id,
                Order.id > after_id,
                Order.status.in_(CANCELLATION_LOOKUP_CANDIDATE_STATUSES),
                Order.is_deleted.is_(False),
            )
            .order_by(Order.id.asc())
            .limit(limit)
        )
        return list(self.session.execute(stmt).scalars().all())

    def list_filtered(
        self,
        status: Optional[str] = None,
        platform_id: Optional[int] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        keyword: Optional[str] = None,
        shipping_status: Optional[str] = None,
        supplier_id: Optional[int] = None,
        sku: Optional[str] = None,
        cs_status: Optional[str] = None,
        assignee_id: Optional[int] = None,
        bucket: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Order]:
        """SRS FR-ORD-01: 버킷/플랫폼/상태/기간/배송상태/공급처/SKU/CS/담당자 필터 + 통합검색."""
        stmt = select(Order).where(Order.is_deleted.is_(False))
        stmt = self._apply_filters(
            stmt,
            status,
            platform_id,
            start_date,
            end_date,
            keyword,
            shipping_status,
            supplier_id,
            sku,
            cs_status,
            assignee_id,
            bucket,
        )
        stmt = stmt.order_by(Order.id.desc()).limit(limit).offset(offset)
        return list(self.session.execute(stmt).scalars().all())

    def count_filtered(
        self,
        status: Optional[str] = None,
        platform_id: Optional[int] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        keyword: Optional[str] = None,
        shipping_status: Optional[str] = None,
        supplier_id: Optional[int] = None,
        sku: Optional[str] = None,
        cs_status: Optional[str] = None,
        assignee_id: Optional[int] = None,
        bucket: Optional[str] = None,
    ) -> int:
        """FR-ORD-04(교환율/반품율/취소율)의 분모, 목록 총건수, 버킷 건수로 쓰인다."""
        stmt = select(func.count()).select_from(Order).where(Order.is_deleted.is_(False))
        stmt = self._apply_filters(
            stmt,
            status,
            platform_id,
            start_date,
            end_date,
            keyword,
            shipping_status,
            supplier_id,
            sku,
            cs_status,
            assignee_id,
            bucket,
        )
        return self.session.execute(stmt).scalar_one()

    def _apply_filters(
        self,
        stmt: Select[Any],
        status: Optional[str],
        platform_id: Optional[int],
        start_date: Optional[date],
        end_date: Optional[date],
        keyword: Optional[str],
        shipping_status: Optional[str] = None,
        supplier_id: Optional[int] = None,
        sku: Optional[str] = None,
        cs_status: Optional[str] = None,
        assignee_id: Optional[int] = None,
        bucket: Optional[str] = None,
    ) -> Select[Any]:
        if bucket:
            stmt = self._apply_bucket(stmt, bucket)
        if status is not None:
            stmt = stmt.where(Order.status == status)
        if platform_id is not None:
            stmt = stmt.where(Order.platform_id == platform_id)
        if assignee_id is not None:
            stmt = stmt.where(Order.assignee_id == assignee_id)
        if start_date is not None:
            stmt = stmt.where(Order.order_date >= datetime.combine(start_date, datetime.min.time()))
        if end_date is not None:
            stmt = stmt.where(Order.order_date < datetime.combine(end_date, datetime.min.time()) + timedelta(days=1))
        if shipping_status is not None:
            stmt = self._apply_shipping_status(stmt, shipping_status)
        if supplier_id is not None:
            stmt = stmt.where(self._order_has_supplier(supplier_id))
        if sku:
            stmt = stmt.where(self._order_has_sku(sku))
        if cs_status is not None:
            stmt = self._apply_cs_status(stmt, cs_status)
        if keyword:
            stmt = stmt.where(self._keyword_clause(keyword))
        return stmt

    # --- 주문관리 버킷(운영자의 하루 작업 큐) ---------------------------------
    # 버킷은 상호배타적이며 아래 우선순위로 첫 매치가 이긴다. 운영자는 왼쪽 버킷부터
    # 0으로 비워나가면 하루 업무가 끝난다.
    #   CS > 배송완료 > 발송완료 > 매칭필요 > 발주대기 > 신규주문 > 송장대기

    def _has_cs(self):
        return or_(
            exists().where(Exchange.order_id == Order.id),
            exists().where(Return.order_id == Order.id),
            exists().where(Cancellation.order_id == Order.id),
            Order.status.in_(("CANCELED", "RETURNED", "REFUNDED", "EXCHANGED")),
        )

    def _is_delivered(self):
        return Order.status == "DELIVERED"

    def _has_shipment_in_transit(self):
        return exists().where(
            and_(
                ShipmentItem.order_id == Order.id,
                Shipment.id == ShipmentItem.shipment_id,
                Shipment.status.in_(("SHIPPING", "DELIVERED")),
            )
        )

    def _needs_matching(self):
        """상품이 SKU에 매칭되지 않아 주문 품목이 하나도 생성되지 않은 주문.

        수집 시 매칭 실패한 품목은 order_items가 만들어지지 않으므로(OrderSyncService
        skipped_items) 품목 0건이 곧 '매칭 필요' 신호다.
        """
        return ~exists().where(OrderItem.order_id == Order.id)

    def _needs_purchase(self):
        """가용재고(current-reserved)가 주문수량보다 적은 품목이 있는 주문 = 발주 필요."""
        available = (
            select(func.coalesce(func.sum(Inventory.sellable_stock - Inventory.reserved_stock), 0))
            .where(Inventory.product_option_id == OrderItem.product_option_id)
            .scalar_subquery()
        )
        return exists().where(and_(OrderItem.order_id == Order.id, OrderItem.quantity > available))

    def _apply_bucket(self, stmt: Select[Any], bucket: str) -> Select[Any]:
        cs, delivered, shipped = self._has_cs(), self._is_delivered(), self._has_shipment_in_transit()
        matching, purchase = self._needs_matching(), self._needs_purchase()

        if bucket == "CS":
            return stmt.where(cs)
        if bucket == "DELIVERED":
            return stmt.where(and_(~cs, delivered))
        if bucket == "SHIPPED":
            return stmt.where(and_(~cs, ~delivered, shipped))
        if bucket == "MATCH_REQUIRED":
            return stmt.where(and_(~cs, ~delivered, ~shipped, matching))
        if bucket == "PO_REQUIRED":
            return stmt.where(and_(~cs, ~delivered, ~shipped, ~matching, purchase))
        if bucket == "NEW":
            return stmt.where(and_(~cs, ~delivered, ~shipped, ~matching, ~purchase, Order.status == "NEW"))
        if bucket == "AWAITING_INVOICE":
            return stmt.where(and_(~cs, ~delivered, ~shipped, ~matching, ~purchase, Order.status != "NEW"))
        return stmt

    def _apply_cs_status(self, stmt: Select[Any], cs_status: str) -> Select[Any]:
        """CS 상태 필터. NONE = 교환/반품/취소가 하나도 없는 주문."""
        has_ex = exists().where(Exchange.order_id == Order.id)
        has_ret = exists().where(Return.order_id == Order.id)
        has_can = exists().where(Cancellation.order_id == Order.id)
        if cs_status == "NONE":
            return stmt.where(and_(~has_ex, ~has_ret, ~has_can))
        if cs_status == "EXCHANGE":
            return stmt.where(has_ex)
        if cs_status == "RETURN":
            return stmt.where(has_ret)
        if cs_status == "CANCEL":
            return stmt.where(has_can)
        return stmt

    def _apply_shipping_status(self, stmt: Select[Any], shipping_status: str) -> Select[Any]:
        """UNSHIPPED = 배송 레코드가 없거나 READY, 그 외는 shipments.status 일치.

        배송은 shipment_items를 통해 N:M으로 연결되므로 EXISTS를 2단계로 건다.
        """
        has_shipment = exists().where(and_(ShipmentItem.order_id == Order.id, Shipment.id == ShipmentItem.shipment_id))
        if shipping_status == "UNSHIPPED":
            return stmt.where(
                or_(
                    ~has_shipment,
                    exists().where(
                        and_(
                            ShipmentItem.order_id == Order.id,
                            Shipment.id == ShipmentItem.shipment_id,
                            Shipment.status == "READY",
                        )
                    ),
                )
            )
        return stmt.where(
            exists().where(
                and_(
                    ShipmentItem.order_id == Order.id,
                    Shipment.id == ShipmentItem.shipment_id,
                    Shipment.status == shipping_status,
                )
            )
        )

    def _order_has_supplier(self, supplier_id: int):
        return exists().where(
            and_(
                OrderItem.order_id == Order.id,
                ProductSupplierMap.product_option_id == OrderItem.product_option_id,
                ProductSupplierMap.supplier_id == supplier_id,
            )
        )

    def _order_has_sku(self, sku: str):
        return exists().where(
            and_(
                OrderItem.order_id == Order.id,
                ProductOption.id == OrderItem.product_option_id,
                ProductOption.sku_code.ilike(f"%{sku}%"),
            )
        )

    def _keyword_clause(self, keyword: str):
        """통합검색: 주문번호/송장번호/고객명/연락처/주소/상품명/옵션/SKU/공급처/메모."""
        like = f"%{keyword}%"
        customer_match = exists().where(
            and_(
                Customer.id == Order.customer_id,
                or_(Customer.name.ilike(like), Customer.phone.ilike(like), Customer.address.ilike(like)),
            )
        )
        shipment_match = exists().where(
            and_(
                ShipmentItem.order_id == Order.id,
                Shipment.id == ShipmentItem.shipment_id,
                Shipment.tracking_no.ilike(like),
            )
        )
        product_match = exists().where(
            and_(
                OrderItem.order_id == Order.id,
                ProductOption.id == OrderItem.product_option_id,
                or_(
                    ProductOption.sku_code.ilike(like),
                    ProductOption.option_name.ilike(like),
                    and_(Product.id == ProductOption.product_id, Product.name.ilike(like)),
                ),
            )
        )
        supplier_match = exists().where(
            and_(
                OrderItem.order_id == Order.id,
                ProductSupplierMap.product_option_id == OrderItem.product_option_id,
                Supplier.id == ProductSupplierMap.supplier_id,
                Supplier.name.ilike(like),
            )
        )
        memo_match = exists().where(
            and_(Memo.target_type == "ORDER", Memo.target_id == Order.id, Memo.content.ilike(like))
        )
        channel_match = exists().where(
            and_(
                OrderItem.order_id == Order.id,
                ProductPlatformMap.product_option_id == OrderItem.product_option_id,
                or_(
                    ProductPlatformMap.platform_option_id.ilike(like),
                    ProductPlatformMap.platform_product_id.ilike(like),
                ),
            )
        )
        return or_(
            Order.platform_order_no.ilike(like),
            customer_match,
            shipment_match,
            product_match,
            supplier_match,
            memo_match,
            channel_match,
        )

    def count_created_between(self, start: datetime, end: datetime) -> int:
        """상용 ERP 확장(6단계) - "오늘 수집된 주문" 근거. 채널의 주문일자(order_date)가
        아니라 우리 DB에 실제로 적재된 시각(created_at, TimestampMixin 기본값)을
        기준으로 센다 - 채널 주문일자는 늦게 수집되면 "오늘"이 아닐 수 있고, 반대로
        오늘 수집된 주문의 채널 주문일자가 어제일 수도 있어 "수집" 의미와 맞지
        않는다(services/operations_dashboard_service.py 모듈 docstring 참고).
        [start, end) - 호출부가 명확한 UTC 경계를 계산해 넘긴다(naive UTC 비교
        관례는 count_delayed_unshipped과 동일)."""
        stmt = (
            select(func.count())
            .select_from(Order)
            .where(Order.is_deleted.is_(False), Order.created_at >= start, Order.created_at < end)
        )
        return self.session.execute(stmt).scalar_one()

    def count_delayed_unshipped(self, threshold_days: int = 2) -> int:
        """SRS FR-ORD-02: 배송준비(NEW/PREPARING) 상태로 threshold_days일 이상 머문 주문 수."""
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=threshold_days)
        stmt = (
            select(func.count())
            .select_from(Order)
            .where(Order.is_deleted.is_(False), Order.status.in_(("NEW", "PREPARING")), Order.order_date < cutoff)
        )
        return self.session.execute(stmt).scalar_one()

    def search(self, keyword: str, limit: int = 5) -> list[Order]:
        """SRS UI v1.1 통합검색: 플랫폼 주문번호로 검색한다."""
        stmt = (
            select(Order)
            .where(Order.is_deleted.is_(False), Order.platform_order_no.ilike(f"%{keyword}%"))
            .order_by(Order.id.desc())
            .limit(limit)
        )
        return list(self.session.execute(stmt).scalars().all())

    def list_items(self, order_id: int) -> list[OrderItem]:
        stmt = select(OrderItem).where(OrderItem.order_id == order_id)
        return list(self.session.execute(stmt).scalars().all())

    def list_items_by_order_ids(self, order_ids: list[int]) -> list[OrderItem]:
        """여러 주문의 items를 한 번의 쿼리로 조회한다(N+1 방지)."""
        if not order_ids:
            return []
        stmt = select(OrderItem).where(OrderItem.order_id.in_(order_ids))
        return list(self.session.execute(stmt).scalars().all())

    def get_item_by_id(self, order_item_id: int) -> Optional[OrderItem]:
        return self.session.get(OrderItem, order_item_id)

    def get_item_by_platform_order_item_no(self, order_id: int, platform_order_item_no: str) -> Optional[OrderItem]:
        """클레임(취소/반품/교환) 수집 시 채널 라인 식별자(예: 쿠팡 vendorItemId)로
        어느 주문상품에 대한 클레임인지 연결한다(services.claim_sync_service 참고)."""
        stmt = select(OrderItem).where(
            OrderItem.order_id == order_id, OrderItem.platform_order_item_no == platform_order_item_no
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def has_items_for_option(self, product_option_id: int) -> bool:
        """이 옵션(SKU)을 참조하는 실제 주문상품이 하나라도 있는지 확인한다.

        옵션 삭제(하드 삭제)를 허용하기 전에 실주문 이력을 참조 중인지 검사하기
        위함 - 참조 중이면 삭제를 막아 order_items의 FK 무결성/과거 손익 이력을
        보호한다.
        """
        stmt = select(func.count()).select_from(OrderItem).where(OrderItem.product_option_id == product_option_id)
        return self.session.execute(stmt).scalar_one() > 0

    def list_order_ids_with_cs(self, order_ids: list[int]) -> set[int]:
        """교환/반품/취소가 하나라도 접수된 주문 id 집합(막힌 사유 판정 N+1 방지)."""
        if not order_ids:
            return set()
        found: set[int] = set()
        for model in (Exchange, Return, Cancellation):
            stmt = select(model.order_id).where(model.order_id.in_(order_ids)).distinct()
            found.update(self.session.execute(stmt).scalars().all())
        return found

    def total_quantity_and_last_order_date(self, product_option_id: int) -> tuple[int, Optional[datetime]]:
        """상품 상세 화면 통계용: 이 옵션의 총 판매수량과 최근 주문일을 한 번에 조회한다."""
        stmt = select(func.coalesce(func.sum(OrderItem.quantity), 0), func.max(Order.order_date)).where(
            OrderItem.product_option_id == product_option_id, OrderItem.order_id == Order.id
        )
        total_quantity, last_order_date = self.session.execute(stmt).one()
        return int(total_quantity), last_order_date


class ShipmentRepository(BaseRepository[Shipment]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Shipment)

    def get_by_order(self, order_id: int) -> Optional[Shipment]:
        """이 주문에 연결된 배송 중 가장 최근 1건.

        분할배송이면 여러 건일 수 있으므로 전체가 필요하면 list_by_order()를 쓴다.
        (기존 1:1 시절 호출부와의 호환을 위해 시그니처를 유지한다)
        """
        shipments = self.list_by_order(order_id)
        return shipments[0] if shipments else None

    def list_by_order(self, order_id: int) -> list[Shipment]:
        """이 주문에 연결된 배송 전체(분할배송 대응). 최신순."""
        stmt = (
            select(Shipment)
            .join(ShipmentItem, ShipmentItem.shipment_id == Shipment.id)
            .where(ShipmentItem.order_id == order_id)
            .order_by(Shipment.id.desc())
            .distinct()
        )
        return list(self.session.execute(stmt).scalars().all())

    def list_by_orders(self, order_ids: list[int]) -> list[tuple[int, Shipment]]:
        """여러 주문의 배송을 (order_id, Shipment) 쌍으로 한 번에 조회한다(N+1 방지)."""
        if not order_ids:
            return []
        stmt = (
            select(ShipmentItem.order_id, Shipment)
            .join(Shipment, Shipment.id == ShipmentItem.shipment_id)
            .where(ShipmentItem.order_id.in_(order_ids))
        )
        return [(row[0], row[1]) for row in self.session.execute(stmt).all()]

    def list_orders_of_shipment(self, shipment_id: int) -> list[int]:
        """이 배송에 묶인 주문 id 목록(합포장 대응)."""
        stmt = select(ShipmentItem.order_id).where(ShipmentItem.shipment_id == shipment_id).distinct()
        return list(self.session.execute(stmt).scalars().all())

    def link_order(
        self, shipment_id: int, order_id: int, order_item_id: Optional[int] = None, quantity: Optional[int] = None
    ) -> ShipmentItem:
        """배송에 주문(또는 주문품목)을 연결한다. 합포장은 이 호출을 반복하면 된다."""
        link = ShipmentItem(shipment_id=shipment_id, order_id=order_id, order_item_id=order_item_id, quantity=quantity)
        self.session.add(link)
        self.session.flush()
        return link

    def list_by_status(self, status: str) -> list[Shipment]:
        stmt = select(Shipment).where(Shipment.status == status)
        return list(self.session.execute(stmt).scalars().all())

    def list_by_carrier(self, carrier: str) -> list[Shipment]:
        """상용 ERP 확장(5단계, A묶음) - 같은 택배사의 기존 송장번호와 중복인지
        확인하기 위한 조회. tracking_no는 등록 경로마다 공백/하이픈 표기가 다를 수
        있어(기존 단건 등록 API는 정규화하지 않는다) DB 쪽에서 정확히 비교하지
        않고, 호출부(services.fulfillment_service)가 정규화한 값끼리 비교한다."""
        stmt = select(Shipment).where(Shipment.carrier == carrier, Shipment.tracking_no.is_not(None))
        return list(self.session.execute(stmt).scalars().all())

    def _filtered_stmt(
        self, status: Optional[str] = None, order_id: Optional[int] = None, search: Optional[str] = None
    ) -> Select[Any]:
        stmt = select(Shipment)
        if status is not None:
            stmt = stmt.where(Shipment.status == status)
        if order_id is not None:
            stmt = stmt.where(
                exists().where(and_(ShipmentItem.shipment_id == Shipment.id, ShipmentItem.order_id == order_id))
            )
        if search:
            like = f"%{search}%"
            stmt = stmt.where((Shipment.tracking_no.ilike(like)) | (Shipment.carrier.ilike(like)))
        return stmt

    def list_filtered(
        self,
        status: Optional[str] = None,
        order_id: Optional[int] = None,
        search: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Shipment]:
        stmt = self._filtered_stmt(status, order_id, search).order_by(Shipment.id.desc()).limit(limit).offset(offset)
        return list(self.session.execute(stmt).scalars().all())

    def count_filtered(
        self, status: Optional[str] = None, order_id: Optional[int] = None, search: Optional[str] = None
    ) -> int:
        stmt = select(func.count()).select_from(self._filtered_stmt(status, order_id, search).subquery())
        return self.session.execute(stmt).scalar_one()


class ExchangeRepository(BaseRepository[Exchange]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Exchange)

    def list_by_status(self, status: str) -> list[Exchange]:
        stmt = select(Exchange).where(Exchange.status == status)
        return list(self.session.execute(stmt).scalars().all())

    def get_by_order_and_claim_id(self, order_id: int, platform_claim_id: str) -> Optional[Exchange]:
        """재수집 시 이미 있는 클레임인지 확인한다(uq_exchange_order_claim_id와 짝)."""
        stmt = select(Exchange).where(Exchange.order_id == order_id, Exchange.platform_claim_id == platform_claim_id)
        return self.session.execute(stmt).scalar_one_or_none()

    def list_filtered(
        self,
        status: Optional[str] = None,
        order_id: Optional[int] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        search: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Exchange]:
        stmt = (
            _filtered_stmt(Exchange, status, order_id, start_date, end_date, search)
            .order_by(Exchange.requested_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.execute(stmt).scalars().all())

    def count_filtered(
        self,
        status: Optional[str] = None,
        order_id: Optional[int] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        search: Optional[str] = None,
    ) -> int:
        stmt = select(func.count()).select_from(
            _filtered_stmt(Exchange, status, order_id, start_date, end_date, search).subquery()
        )
        return self.session.execute(stmt).scalar_one()


class ReturnRepository(BaseRepository[Return]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Return)

    def list_by_status(self, status: str) -> list[Return]:
        stmt = select(Return).where(Return.status == status)
        return list(self.session.execute(stmt).scalars().all())

    def get_by_order_and_claim_id(self, order_id: int, platform_claim_id: str) -> Optional[Return]:
        """재수집 시 이미 있는 클레임인지 확인한다(uq_return_order_claim_id와 짝)."""
        stmt = select(Return).where(Return.order_id == order_id, Return.platform_claim_id == platform_claim_id)
        return self.session.execute(stmt).scalar_one_or_none()

    def list_filtered(
        self,
        status: Optional[str] = None,
        order_id: Optional[int] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        search: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Return]:
        stmt = (
            _filtered_stmt(Return, status, order_id, start_date, end_date, search)
            .order_by(Return.requested_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.execute(stmt).scalars().all())

    def count_filtered(
        self,
        status: Optional[str] = None,
        order_id: Optional[int] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        search: Optional[str] = None,
    ) -> int:
        stmt = select(func.count()).select_from(
            _filtered_stmt(Return, status, order_id, start_date, end_date, search).subquery()
        )
        return self.session.execute(stmt).scalar_one()


class CancellationRepository(BaseRepository[Cancellation]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Cancellation)

    def list_by_status(self, status: str) -> list[Cancellation]:
        stmt = select(Cancellation).where(Cancellation.status == status)
        return list(self.session.execute(stmt).scalars().all())

    def get_by_order_and_claim_id(self, order_id: int, platform_claim_id: str) -> Optional[Cancellation]:
        """재수집 시 이미 있는 클레임인지 확인한다(uq_cancellation_order_claim_id와 짝)."""
        stmt = select(Cancellation).where(
            Cancellation.order_id == order_id, Cancellation.platform_claim_id == platform_claim_id
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def list_filtered(
        self,
        status: Optional[str] = None,
        order_id: Optional[int] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        search: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Cancellation]:
        stmt = (
            _filtered_stmt(Cancellation, status, order_id, start_date, end_date, search)
            .order_by(Cancellation.requested_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.execute(stmt).scalars().all())

    def count_filtered(
        self,
        status: Optional[str] = None,
        order_id: Optional[int] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        search: Optional[str] = None,
    ) -> int:
        stmt = select(func.count()).select_from(
            _filtered_stmt(Cancellation, status, order_id, start_date, end_date, search).subquery()
        )
        return self.session.execute(stmt).scalar_one()


class ClaimUnmatchedRepository(BaseRepository[ClaimUnmatched]):
    """아직 수집되지 않은 주문의 클레임 보존소(models.order.ClaimUnmatched 참고)."""

    def __init__(self, session: Session) -> None:
        super().__init__(session, ClaimUnmatched)

    def get_by_key(self, platform_id: int, claim_type: str, platform_claim_id: Optional[str], platform_order_no: str):
        stmt = select(ClaimUnmatched).where(
            ClaimUnmatched.platform_id == platform_id,
            ClaimUnmatched.claim_type == claim_type,
            ClaimUnmatched.platform_claim_id == platform_claim_id,
            ClaimUnmatched.platform_order_no == platform_order_no,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def list_unresolved(self, platform_id: Optional[int] = None) -> list[ClaimUnmatched]:
        stmt = select(ClaimUnmatched).where(ClaimUnmatched.resolved_at.is_(None))
        if platform_id is not None:
            stmt = stmt.where(ClaimUnmatched.platform_id == platform_id)
        return list(self.session.execute(stmt.order_by(ClaimUnmatched.detected_at.desc())).scalars())


class ClaimCollectionCursorRepository(BaseRepository[ClaimCollectionCursor]):
    """ "후보 주문" 기반 클레임 조회의 회전식 순회 체크포인트(models.order.
    ClaimCollectionCursor 참고)."""

    def __init__(self, session: Session) -> None:
        super().__init__(session, ClaimCollectionCursor)

    def get_or_create(self, platform_id: int, claim_type: str) -> ClaimCollectionCursor:
        stmt = select(ClaimCollectionCursor).where(
            ClaimCollectionCursor.platform_id == platform_id, ClaimCollectionCursor.claim_type == claim_type
        )
        existing = self.session.execute(stmt).scalar_one_or_none()
        if existing is not None:
            return existing
        return self.add(
            ClaimCollectionCursor(
                platform_id=platform_id, claim_type=claim_type, last_order_id=0, updated_at=datetime.now(timezone.utc)
            )
        )
