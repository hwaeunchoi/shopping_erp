"""
services/order_sync_service.py
----------------------------------
쇼핑몰 커넥터(integrations/malls)로 주문을 수집하여 orders/order_items/
order_status_history/customers에 반영하는 업무로직.

- SRS FR-MALL-04(플랫폼별 주문번호 중복 수집 방지)는
  OrderRepository.get_by_platform_order_no()로 보장한다.
- 신규 주문은 order_items를 만들고 재고를 예약(InventoryService.reserve)하며,
  이미 배송 이후 상태로 들어온 주문은 곧바로 출고 차감한다.
- 기존 주문의 상태가 바뀌면 order_status_history를 남기고, 미출고 상태에서
  출고 상태로 넘어갈 때 재고를 차감하며, 미출고 상태에서 취소로 바뀌면
  예약을 해제한다.
- 주문을 반영한 고객은 CustomerStatsService.refresh()로 캐시 통계를 갱신한다.

상품 자동등록/자동매칭 로직은 services.product_sync_service.ProductSyncService로
분리했다 - 이 서비스는 더 이상 상품을 생성하지 않는다(어떤 플랫폼이든 마찬가지다).
product_platform_map에 매핑이 없는 주문상품을 만나면 ProductSyncService.
match_unmapped_item()에 위임해 기존 상품에 자동매칭만 시도하고(3단계: 플랫폼옵션번호
-> 플랫폼상품번호 -> 판매자상품코드, 전부 실제 고유ID 기반 - 상품명/옵션명 유사도는
사용하지 않는다), 실패하면 미매칭 상품으로 기록될 뿐 새 상품은 만들지 않는다.
네이버 스마트스토어 상품은 이제 반드시
ProductSyncService.sync_products_from_naver()(네이버 "상품" API)를 통해서만 생성/
갱신되므로, 주문 수집을 실행하기 전에 네이버 상품 동기화를 먼저 실행해야 매핑이
채워진다.

이미 수집된 주문이라도 재수집 시마다 _sync_items()로 매핑을 다시 확인한다
- 최초 수집 당시엔 매핑이 없어 건너뛴 상품이라도, 그 사이 네이버 상품 동기화나
사용자의 수동 연결로 매핑이 채워졌다면 다음 재수집에서 자동으로 채워진다
(이미 채워진 상품은 product_option_id로 중복 검사해 다시 만들지 않는다).

SRS FR-LOG-01(API 수집이력): sync_orders()의 성공/실패를 system_logs에
log_type="API_COLLECT"로 남긴다. 실패 시에는 이번 호출에서 세션에 쌓인
미완료 변경분을 롤백한 뒤(어차피 호출부가 commit()하지 않으므로 유실되는
내용은 없다) 실패 로그만 커밋하고 원래 예외를 그대로 다시 던진다 - 호출부
입장에서 성공/예외 동작은 이 로깅 추가 전과 완전히 동일하다.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from integrations.malls.base_mall_connector import BaseMallConnector
from models.customer import Customer
from models.order import Order, OrderItem, OrderStatusHistory
from models.system import SystemLog
from repositories.customer_repository import CustomerRepository
from repositories.order_repository import OrderRepository
from repositories.product_repository import ProductCostHistoryRepository, ProductPlatformMapRepository
from repositories.system_repository import SystemLogRepository
from services.customer_stats_service import CustomerStatsService
from services.inventory_service import InsufficientStockError, InventoryInvariantError, InventoryService
from services.product_sync_service import ProductSyncService

logger = logging.getLogger(__name__)

SHIPPED_STATUSES = {"SHIPPING", "DELIVERED", "EXCHANGED", "RETURNED", "REFUNDED"}
UNSHIPPED_STATUSES = {"NEW", "PREPARING"}


def _clip(value: Any, length: int) -> Optional[str]:
    """문자열을 컬럼 길이에 맞춰 자른다(마켓 데이터가 길어도 저장 실패하지 않도록)."""
    if value is None:
        return None
    text = str(value)
    return text[:length] if len(text) > length else text


class OrderSyncService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.order_repo = OrderRepository(session)
        self.customer_repo = CustomerRepository(session)
        self.platform_map_repo = ProductPlatformMapRepository(session)
        self.cost_history_repo = ProductCostHistoryRepository(session)
        self.inventory_service = InventoryService(session)
        self.customer_stats_service = CustomerStatsService(session)
        self.system_log_repo = SystemLogRepository(session)
        self.product_sync_service = ProductSyncService(session)

    def sync_orders(
        self, connector: BaseMallConnector, platform_id: int, warehouse_id: int, start_date, end_date
    ) -> dict[str, int]:
        try:
            raw_orders = connector.fetch_orders(start_date, end_date)
            created = updated = skipped_items = auto_matched_products = 0
            touched_customer_ids: set[int] = set()

            for raw in raw_orders:
                existing = self.order_repo.get_by_platform_order_no(platform_id, raw["platform_order_no"])
                if existing:
                    # 이전에 수집된 주문에도 배송지 정보가 비어 있으면 채운다(재수집 시 backfill).
                    self._fill_receiver_info(existing, raw)
                    if existing.status != raw["status"]:
                        self.apply_status_change(existing, raw["status"], warehouse_id)
                        updated += 1
                    n_skipped, n_matched = self._sync_items(existing, platform_id, warehouse_id, raw)
                    skipped_items += n_skipped
                    auto_matched_products += n_matched
                    if existing.customer_id:
                        touched_customer_ids.add(existing.customer_id)
                else:
                    order, n_skipped, n_matched = self._create_order(platform_id, warehouse_id, raw)
                    skipped_items += n_skipped
                    auto_matched_products += n_matched
                    created += 1
                    if order.customer_id:
                        touched_customer_ids.add(order.customer_id)

            for customer_id in touched_customer_ids:
                self.customer_stats_service.refresh(customer_id)

            result = {
                "total": len(raw_orders),
                "created": created,
                "updated": updated,
                "skipped_items": skipped_items,
                "auto_matched_products": auto_matched_products,
            }
            self._log_collection(platform_id, "SUCCESS", result)
            self.session.flush()
            return result
        except Exception as e:
            self.session.rollback()
            self._log_collection(platform_id, "FAILED", None, error=str(e))
            self.session.commit()
            raise

    def _log_collection(
        self, platform_id: int, result_status: str, result: Optional[dict[str, int]], error: Optional[str] = None
    ) -> None:
        message = f"주문 수집 {result_status}: platform_id={platform_id}"
        if result is not None:
            message += (
                f", 총 {result['total']}건(신규 {result['created']}, 갱신 {result['updated']}, "
                f"매핑없음 {result['skipped_items']}, 상품자동매칭 {result['auto_matched_products']})"
            )
        if error is not None:
            message += f", 에러: {error}"
        self.system_log_repo.add(
            SystemLog(
                log_type="API_COLLECT",
                source="order_sync",
                level="INFO" if result_status == "SUCCESS" else "ERROR",
                message=message[:2000],
                created_at=datetime.now(timezone.utc),
            )
        )

    @staticmethod
    def _fill_receiver_info(order: Order, raw: dict[str, Any]) -> None:
        """이미 존재하는 주문에 배송지(수취인) 정보가 비어 있으면 커넥터 값으로 채운다.

        이미 값이 있으면 덮어쓰지 않는다(운영자가 수정했을 수 있으므로).
        """
        if order.receiver_name is None and raw.get("receiver_name"):
            order.receiver_name = _clip(raw.get("receiver_name"), 50)
        if order.receiver_phone is None and raw.get("receiver_phone"):
            order.receiver_phone = _clip(raw.get("receiver_phone"), 20)
        if order.receiver_zipcode is None and raw.get("receiver_zipcode"):
            order.receiver_zipcode = _clip(raw.get("receiver_zipcode"), 10)
        if order.receiver_address is None and raw.get("receiver_address"):
            order.receiver_address = _clip(raw.get("receiver_address"), 500)
        if order.delivery_message is None and raw.get("delivery_message"):
            order.delivery_message = _clip(raw.get("delivery_message"), 500)

    def _create_order(self, platform_id: int, warehouse_id: int, raw: dict[str, Any]) -> tuple[Order, int, int]:
        customer = self._get_or_create_customer(platform_id, raw)

        order = Order(
            platform_id=platform_id,
            platform_order_no=raw["platform_order_no"],
            customer_id=customer.id if customer else None,
            status=raw["status"],
            order_date=raw["order_date"],
            payment_date=raw["order_date"],
            delivery_completed_date=raw["order_date"] if raw["status"] == "DELIVERED" else None,
            total_amount=raw["total_amount"],
            discount_amount=raw.get("discount_amount", 0.0),
            # 배송지(수취인) 정보 - 커넥터가 수집한 값을 저장한다(없으면 None).
            receiver_name=_clip(raw.get("receiver_name"), 50),
            receiver_phone=_clip(raw.get("receiver_phone"), 20),
            receiver_zipcode=_clip(raw.get("receiver_zipcode"), 10),
            receiver_address=_clip(raw.get("receiver_address"), 500),
            delivery_message=_clip(raw.get("delivery_message"), 500),
        )
        self.session.add(order)
        self.session.flush()

        skipped_items, auto_matched = self._sync_items(order, platform_id, warehouse_id, raw)

        self.session.add(
            OrderStatusHistory(
                order_id=order.id, from_status=None, to_status=raw["status"], changed_at=raw["order_date"]
            )
        )
        self.session.flush()
        return order, skipped_items, auto_matched

    def _sync_items(self, order: Order, platform_id: int, warehouse_id: int, raw: dict[str, Any]) -> tuple[int, int]:
        """raw["items"]를 order에 반영한다.

        (매핑이 끝내 없어 건너뛴 건수, 기존 상품에 자동 매칭한 건수)를 반환한다.
        매핑이 없는 상품은 ProductSyncService.match_unmapped_item()에 위임해
        기존 상품에 자동매칭만 시도한다 - 이 메서드는 절대 새 상품을 만들지 않는다.
        이미 반영된 상품(product_option_id 기준)은 다시 만들지 않는다 - 신규 주문
        생성 시(_create_order)뿐 아니라 이미 존재하는 주문을 재수집할 때도 이
        메서드를 호출하므로, 최초 수집 당시엔 매핑이 없어 건너뛴 상품이 이후
        네이버 상품 동기화나 상품관리 화면에서 매핑이 채워지면(또는 이번 호출에서
        자동 매칭되면) 다음 재수집 시 자동으로 채워진다.
        """
        existing_items = self.order_repo.list_items(order.id)
        incoming_has_poin = any((it.get("platform_order_item_no") or None) for it in raw["items"])
        existing_has_null_poin = any(x.platform_order_item_no is None for x in existing_items)

        # 보호장치(재수집 안전): 이미 아이템이 있고 그 중 상품주문번호(NULL)인 라인이 있는데
        # 이번 응답에 상품주문번호가 있으면, 상품주문번호 기준으로 라인을 새로 만들 경우
        # 기존 NULL 라인 옆에 중복 라인이 생겨 수량·금액·재고가 중복될 수 있다. 그래서
        # 이 주문의 아이템 동기화는 안전하게 스킵하고 구조화 경고만 남긴다(개인정보·전체
        # 상품주문번호 미출력). 주문 자체(상태/수취인)는 상위에서 별도 처리된다.
        # 로그는 주문당 1회(라인별 아님)라 과다 기록되지 않는다.
        # TODO: 향후 "상품주문번호 매칭 필요" 상태 컬럼을 두면 반복 경고를 억제할 수 있다.
        if existing_items and existing_has_null_poin and incoming_has_poin:
            logger.warning(
                "상품주문번호 매칭 필요로 아이템 동기화를 스킵합니다(수량·금액 중복 방지): "
                "order_id=%s, 기존라인수=%d",
                order.id,
                len(existing_items),
            )
            return 0, 0

        existing_by_poin = {x.platform_order_item_no: x for x in existing_items if x.platform_order_item_no}
        existing_option_ids = {x.product_option_id for x in existing_items}
        seen_poin_in_batch: set[str] = set()
        skipped_items = 0
        auto_matched = 0
        for item in raw["items"]:
            poin = item.get("platform_order_item_no") or None
            mapping = self.platform_map_repo.get_by_option_id(platform_id, item["platform_option_id"])
            if mapping is None:
                mapping = self.product_sync_service.match_unmapped_item(platform_id, item, raw["platform_order_no"])
                if mapping is not None:
                    auto_matched += 1
                else:
                    skipped_items += 1
                    continue
            if poin is not None:
                # 배치 내 동일 상품주문번호가 두 라인에 연결되면 오류로 보고 두 번째는 스킵.
                if poin in seen_poin_in_batch:
                    logger.warning(
                        "동일 상품주문번호가 한 주문의 두 라인에 연결됨 - 두 번째 라인 스킵: order_id=%s", order.id
                    )
                    continue
                seen_poin_in_batch.add(poin)
                if poin in existing_by_poin:
                    continue  # 이미 수집된 상품주문 라인 - 재사용(중복 생성/덮어쓰기 안 함)
                # 새 상품주문번호 -> 별도 라인 생성(같은 SKU라도 상품주문번호가 다르면 별개 라인)
            else:
                # 상품주문번호 미제공(다른 채널/과거 데이터) -> 기존 SKU 기반 호환 dedup
                if mapping.product_option_id in existing_option_ids:
                    continue
                existing_option_ids.add(mapping.product_option_id)
            self._create_order_item(order, warehouse_id, raw, mapping, item, poin)
        self.session.flush()
        return skipped_items, auto_matched

    def _create_order_item(self, order: Order, warehouse_id: int, raw: dict[str, Any], mapping, item, poin) -> None:
        """OrderItem 1행 생성 + 상태에 따른 재고 반영(예약/차감). 기존 로직 보존."""
        cost_record = self.cost_history_repo.get_effective_cost(mapping.product_option_id, raw["order_date"])
        self.session.add(
            OrderItem(
                order_id=order.id,
                product_option_id=mapping.product_option_id,
                platform_order_item_no=poin,
                quantity=item["quantity"],
                unit_price=item["unit_price"],
                cost_price_snapshot=cost_record.cost_price if cost_record else None,
                line_amount=round(item["quantity"] * item["unit_price"], 2),
            )
        )
        try:
            if raw["status"] in SHIPPED_STATUSES:
                self.inventory_service.deduct_on_shipment(
                    mapping.product_option_id,
                    warehouse_id,
                    item["quantity"],
                    reference_id=order.id,
                    release_reserved=False,
                )
            elif raw["status"] in UNSHIPPED_STATUSES:
                self.inventory_service.reserve(mapping.product_option_id, warehouse_id, item["quantity"])
        except ValueError:
            # 재고관리 화면에 이 SKU-창고 조합의 재고 레코드가 아직 등록되지 않은 경우
            # (InventoryService._get_inventory가 ValueError를 던진다). 주문상품 자체는
            # 정상 반영하고 재고 반영만 건너뛴다 - SKU 매핑이 없는 경우와 마찬가지로
            # 외부 데이터/설정 미비를 이유로 전체 동기화를 중단시키지 않는다.
            logger.warning(
                "재고 레코드가 없어 재고 반영을 건너뜁니다: option=%s, warehouse=%s, order_id=%s",
                mapping.product_option_id,
                warehouse_id,
                order.id,
            )
        except (InventoryInvariantError, InsufficientStockError) as e:
            # 채널 재고와 내부 재고가 어긋나 예약/차감이 불변조건을 위반하는 경우.
            # 외부에서 이미 성립된 주문이므로 주문 자체는 반드시 남겨야 하고,
            # 재고 반영만 건너뛴다. 여기서 예외를 통과시키면 sync_orders()의
            # 최상위 except가 rollback을 수행해 **이번 회차에 수집한 모든 주문이
            # 통째로 사라진다**(주문 1건의 재고 문제로 정상 주문 수십 건 유실).
            logger.warning(
                "재고 불변조건 위반으로 재고 반영을 건너뜁니다(주문은 정상 수집): "
                "option=%s, warehouse=%s, order_id=%s, 사유=%s",
                mapping.product_option_id,
                warehouse_id,
                order.id,
                e,
            )

    def apply_status_change(self, order: Order, new_status: str, warehouse_id: Optional[int] = None) -> None:
        """주문 상태를 바꾸고 order_status_history를 남긴 뒤, 필요하면 재고에 반영한다.

        sync_orders()(커넥터 수집)뿐 아니라 배송/교환/반품/취소 관리 화면에서
        수동으로 주문 상태를 바꿀 때도 이 메서드를 공용으로 사용한다(Order.status
        전이 시 이력 기록 + 재고 반영 로직을 한 곳에서만 관리하기 위함).

        warehouse_id가 없으면(예: 배송 완료 이후 단계의 교환/반품/취소 처리처럼
        재고 이동이 필요 없는 경우) 재고 반영 없이 상태/이력만 갱신한다. 다만
        실제로 재고 이동이 필요한 전이(미출고 -> 출고, 미출고 -> 취소)인데
        warehouse_id가 없으면 재고가 반영되지 않으므로 경고 로그를 남긴다.

        재고 레코드가 아직 없는 SKU-창고 조합(InventoryService._get_inventory가
        ValueError를 던지는 경우)을 만나도 이 주문상품 하나만 건너뛰고 나머지
        주문상품/전체 동기화는 계속 진행한다 - _sync_items()의 재고 반영과 동일한
        방어 로직이다(실사용에서 확인된 오류로, 재고 미등록 SKU 하나 때문에 전체
        주문 동기화가 중단되던 문제 재발 방지).
        """
        old_status = order.status
        order.status = new_status
        order.updated_at = datetime.now(timezone.utc)
        if new_status == "DELIVERED" and order.delivery_completed_date is None:
            order.delivery_completed_date = datetime.now(timezone.utc)
        self.session.add(
            OrderStatusHistory(
                order_id=order.id, from_status=old_status, to_status=new_status, changed_at=datetime.now(timezone.utc)
            )
        )

        needs_inventory_change = (old_status in UNSHIPPED_STATUSES and new_status in SHIPPED_STATUSES) or (
            old_status in UNSHIPPED_STATUSES and new_status == "CANCELED"
        )
        if needs_inventory_change and warehouse_id is None:
            logger.warning(
                "재고 반영이 필요한 주문 상태 전이인데 warehouse_id가 없어 재고를 반영하지 않습니다: "
                "order_id=%s, %s -> %s",
                order.id,
                old_status,
                new_status,
            )
        elif warehouse_id is not None:
            items = self.order_repo.list_items(order.id)
            if old_status in UNSHIPPED_STATUSES and new_status in SHIPPED_STATUSES:
                for item in items:
                    try:
                        self.inventory_service.deduct_on_shipment(
                            item.product_option_id, warehouse_id, item.quantity, reference_id=order.id
                        )
                    except ValueError:
                        logger.warning(
                            "재고 레코드가 없어 재고 반영을 건너뜁니다: option=%s, warehouse=%s, order_id=%s",
                            item.product_option_id,
                            warehouse_id,
                            order.id,
                        )
            elif old_status in UNSHIPPED_STATUSES and new_status == "CANCELED":
                for item in items:
                    try:
                        self.inventory_service.release_reservation(item.product_option_id, warehouse_id, item.quantity)
                    except ValueError:
                        logger.warning(
                            "재고 레코드가 없어 재고 반영을 건너뜁니다: option=%s, warehouse=%s, order_id=%s",
                            item.product_option_id,
                            warehouse_id,
                            order.id,
                        )

        self.session.flush()

    def _get_or_create_customer(self, platform_id: int, raw: dict[str, Any]) -> Customer:
        customer = self.customer_repo.get_by_platform_key(platform_id, raw["customer_key"])
        if customer:
            return customer
        customer = Customer(
            platform_id=platform_id,
            platform_customer_key=raw["customer_key"],
            name=raw.get("customer_name"),
            phone=raw.get("customer_phone"),
        )
        return self.customer_repo.add(customer)
