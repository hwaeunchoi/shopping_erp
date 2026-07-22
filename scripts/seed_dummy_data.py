"""
scripts/seed_dummy_data.py
-----------------------------
2단계: 더미 데이터 생성 스크립트.

1단계(scripts/init_db.py)로 스키마와 기준정보(역할/권한/관리자 계정/
5개 쇼핑몰 플랫폼/기본 창고)가 이미 생성되어 있다는 전제 하에,
상품-재고-주문(교환/반품/취소 포함)-정산-비용-광고-매출/손익분석까지
이어지는 핵심 업무 흐름을 화면에서 검증할 수 있도록 더미 데이터를 채운다.

대상 테이블 (총 24개):
  suppliers, supplier_contacts, product_supplier_map,
  products, product_options, product_images, product_platform_map,
  product_cost_history,
  warehouses(기존 데이터 재사용), inventory, inventory_transactions,
  customers,
  orders, order_items, order_status_history, shipments,
  exchanges, returns, cancellations,
  settlements, settlement_details,
  costs,
  ad_campaigns, ad_performance_daily,
  profit_loss_summary, product_performance_summary, kpi_targets

시스템 운영 중에만 발생하는 로그성 테이블(system_logs, notifications,
audit_logs, memos, attachments, task_execution_history 등)은 이
스크립트의 대상이 아니다. 그 테이블들은 실제 사용 과정에서 채워지는
운영 데이터이지 "더미 초기 데이터"가 아니기 때문이다.

집계 테이블(profit_loss_summary/product_performance_summary)은
정식 운영에서는 5단계(계산엔진) 배치가 채우지만, 화면 검증을 위해
이 스크립트가 생성한 주문/비용/광고 데이터를 그대로 집계하여 채운다
(계산 로직은 이후 서비스 레이어 구현 시 동일 정의로 대체될 임시 버전).

실행 방법 (Windows, 프로젝트 루트에서, init_db.py 실행 후):
    python scripts\\seed_dummy_data.py
"""

import random
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, TypedDict

from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging_config import setup_logging  # noqa: E402
from config.settings import settings  # noqa: E402
from core.database import session_scope  # noqa: E402
from models.ad import AdCampaign, AdPerformanceDaily  # noqa: E402
from models.analytics import KpiTarget, ProductPerformanceSummary, ProfitLossSummary  # noqa: E402
from models.cost import Cost  # noqa: E402
from models.customer import Customer  # noqa: E402
from models.inventory import Inventory, InventoryStatus, InventoryTransaction, Warehouse  # noqa: E402
from models.order import (  # noqa: E402
    Cancellation,
    Exchange,
    Order,
    OrderItem,
    OrderStatusHistory,
    Return,
    Shipment,
    ShipmentItem,
)
from models.platform import Platform  # noqa: E402
from models.product import Product, ProductCostHistory, ProductImage, ProductOption, ProductPlatformMap  # noqa: E402
from models.settlement import Settlement, SettlementDetail  # noqa: E402
from models.supplier import ProductSupplierMap, Supplier, SupplierContact  # noqa: E402
from models.user import User  # noqa: E402

random.seed(42)
NOW = datetime.now(timezone.utc)

NUM_CUSTOMERS = 60
NUM_ORDERS = 400
NUM_COSTS = 80
NUM_AD_CAMPAIGNS = 6
ORDER_DAYS_BACK = 90
AD_PERF_DAYS_BACK = 30
PL_SUMMARY_DAYS_BACK = 30

SUPPLIER_NAMES = ["대한상사", "한빛무역", "서울패션공급", "그린라이프물류", "코리아굿즈"]
BANK_NAMES = ["국민은행", "신한은행", "우리은행", "하나은행"]

# (category, name, base_price, cost_ratio) - cost_ratio는 base_price 대비 매입원가 비율
PRODUCT_CATALOG = [
    ("의류", "베이직 크루넥 티셔츠", 19900, 0.45),
    ("의류", "와이드 데님 팬츠", 39900, 0.50),
    ("의류", "경량 패딩 조끼", 59900, 0.55),
    ("의류", "기능성 레깅스", 27900, 0.42),
    ("잡화", "심플 크로스백", 29900, 0.40),
    ("잡화", "가죽 카드지갑", 15900, 0.38),
    ("잡화", "접이식 우산", 12900, 0.35),
    ("뷰티", "수분크림 50ml", 24900, 0.30),
    ("뷰티", "선크림 SPF50", 18900, 0.28),
    ("뷰티", "립밤 3종 세트", 13900, 0.32),
    ("식품", "유기농 견과류 세트", 22900, 0.55),
    ("식품", "프리미엄 원두커피 1kg", 28900, 0.48),
    ("생활용품", "스테인리스 텀블러", 16900, 0.40),
    ("생활용품", "무선 충전 거치대", 25900, 0.45),
    ("생활용품", "실리콘 주방용품 세트", 21900, 0.42),
]
COLORS = ["블랙", "화이트", "그레이", "네이비", "베이지"]
SIZES = ["S", "M", "L", "FREE"]

CUSTOMER_LAST_NAMES = ["김", "이", "박", "최", "정", "강", "조", "윤", "장", "임"]
CUSTOMER_FIRST_NAMES = [
    "민준",
    "서연",
    "도윤",
    "지우",
    "하은",
    "시우",
    "수아",
    "예준",
    "지호",
    "채원",
    "은우",
    "다은",
    "현우",
    "소율",
    "건우",
]

CARRIERS = ["CJ대한통운", "롯데택배", "한진택배"]
AD_PLATFORM_CODES = ["naver_search_ad", "naver_shopping_ad", "coupang_ad"]

FEE_RATE_BY_PLATFORM_CODE = {
    "naver_smartstore": 0.035,
    "coupang": 0.10,
    "esm": 0.12,
    "elevenst": 0.12,
    "kakao_shopping": 0.035,
}

CANCEL_REASONS = ["단순 변심", "주문 실수", "배송 지연", "타 상점 재구매"]
EXCHANGE_REASONS = ["사이즈 교환", "색상 교환", "상품 불량"]
RETURN_REASONS = ["상품 불량", "이미지와 상이", "단순 변심", "배송 파손"]
DEFECTIVE_INSPECTION_RATE = 0.3  # 반품 검수에서 불합격(불량 재고로 분류)되는 비율

# 주문 상태 분포 (가중치) - status: (order.status, ORDER_STATUS_WEIGHTS의 확률)
ORDER_STATUS_WEIGHTS = [
    ("DELIVERED", 42),
    ("SHIPPING", 10),
    ("PREPARING", 8),
    ("NEW", 7),
    ("CANCELED", 10),
    ("EXCHANGED", 5),
    ("RETURNED", 8),
    ("REFUNDED", 10),
]


def weighted_choice(pairs: list[tuple[str, int]]) -> str:
    labels = [p[0] for p in pairs]
    weights = [p[1] for p in pairs]
    return random.choices(labels, weights=weights, k=1)[0]


def random_time_between(start: datetime, end: datetime) -> datetime:
    if end <= start:
        return start
    seconds = random.uniform(0, (end - start).total_seconds())
    return start + timedelta(seconds=seconds)


def build_status_timeline(order_date: datetime, final_status: str) -> list[tuple[str, datetime]]:
    """order_date부터 최종 상태까지의 상태변경 이력(시각 포함)을 생성한다."""
    timeline = [("NEW", order_date)]
    if final_status == "NEW":
        return timeline

    t = order_date + timedelta(hours=random.uniform(1, 5))
    timeline.append(("PREPARING", t))
    if final_status == "PREPARING":
        return timeline
    if final_status == "CANCELED":
        t = t + timedelta(hours=random.uniform(1, 10))
        timeline.append(("CANCELED", t))
        return timeline

    t = t + timedelta(hours=random.uniform(3, 24))
    timeline.append(("SHIPPING", t))
    if final_status == "SHIPPING":
        return timeline

    t = t + timedelta(hours=random.uniform(24, 72))
    timeline.append(("DELIVERED", t))
    if final_status == "DELIVERED":
        return timeline

    t = t + timedelta(days=random.uniform(1, 7))
    timeline.append((final_status, t))  # EXCHANGED/RETURNED/REFUNDED
    return timeline


def timeline_time_for(timeline: list[tuple[str, datetime]], status: str) -> datetime | None:
    return next((t for s, t in timeline if s == status), None)


def seed_suppliers(db) -> list[Supplier]:
    suppliers = []
    for name in SUPPLIER_NAMES:
        s = Supplier(
            name=name,
            business_no=f"{random.randint(100, 999)}-{random.randint(10, 99)}-{random.randint(10000, 99999)}",
            bank_name=random.choice(BANK_NAMES),
            bank_account_no=f"{random.randint(100, 999)}-{random.randint(100000, 999999)}-{random.randint(10, 99)}",
            bank_account_holder=name,
            payment_terms="월말 마감 익월 10일 지급",
            is_active=True,
            created_at=NOW - timedelta(days=150),
            updated_at=NOW - timedelta(days=150),
        )
        db.add(s)
        suppliers.append(s)
    db.flush()
    for s in suppliers:
        db.add(
            SupplierContact(
                supplier_id=s.id,
                name=f"{s.name} 담당자",
                phone=f"010-{random.randint(1000, 9999)}-{random.randint(1000, 9999)}",
                email=f"contact{s.id}@supplier.example.com",
                is_primary=True,
            )
        )
    print(f"[OK] 공급처 {len(suppliers)}개 생성 완료")
    return suppliers


def seed_products(db, admin: User) -> tuple[list[Product], list[ProductOption], dict[int, dict]]:
    products = []
    options = []
    option_meta: dict[int, dict] = {}  # option_id -> {base_price, cost_price, product_id}
    created_at = NOW - timedelta(days=120)

    for category, name, base_price, cost_ratio in PRODUCT_CATALOG:
        product = Product(
            name=name,
            category=category,
            base_price=base_price,
            status="ACTIVE",
            created_at=created_at,
            updated_at=created_at,
        )
        db.add(product)
        db.flush()
        products.append(product)

        db.add(
            ProductImage(
                product_id=product.id,
                image_url=f"/static/products/{product.id}/thumb.jpg",
                is_thumbnail=True,
                sort_order=0,
            )
        )

        num_options = random.randint(1, 3)
        picked_colors = random.sample(COLORS, k=min(num_options, len(COLORS)))
        for idx, color in enumerate(picked_colors, start=1):
            size = random.choice(SIZES)
            sku_code = f"SKU-{product.id:03d}-{idx:02d}"
            option = ProductOption(
                product_id=product.id,
                option_name=f"{color}/{size}",
                color=color,
                size=size,
                sku_code=sku_code,
                barcode=f"880{product.id:05d}{idx:02d}",
                is_active=True,
            )
            db.add(option)
            db.flush()
            options.append(option)

            cost_price = round(base_price * cost_ratio, 2)
            db.add(
                ProductCostHistory(
                    product_option_id=option.id,
                    supplier_id=None,
                    cost_price=cost_price,
                    effective_from=created_at,
                    effective_to=None,
                    created_by=admin.id,
                )
            )
            option_meta[option.id] = {
                "base_price": float(base_price),
                "cost_price": cost_price,
                "product_id": product.id,
            }

    print(f"[OK] 상품 {len(products)}개 / 옵션(SKU) {len(options)}개 생성 완료")
    return products, options, option_meta


def seed_product_platform_maps(db, options: list[ProductOption], platforms: list[Platform]) -> dict[int, list[int]]:
    options_by_platform: dict[int, list[int]] = defaultdict(list)
    count = 0
    for option in options:
        for platform in random.sample(platforms, k=random.randint(2, min(3, len(platforms)))):
            db.add(
                ProductPlatformMap(
                    product_option_id=option.id,
                    platform_id=platform.id,
                    platform_option_id=f"{platform.code[:3].upper()}{option.id:06d}",
                )
            )
            options_by_platform[platform.id].append(option.id)
            count += 1
    print(f"[OK] 상품-플랫폼 매핑 {count}건 생성 완료")
    return options_by_platform


def seed_product_supplier_map(db, options: list[ProductOption], suppliers: list[Supplier]) -> None:
    for option in options:
        supplier = random.choice(suppliers)
        db.add(ProductSupplierMap(product_option_id=option.id, supplier_id=supplier.id, is_primary=True))
    print(f"[OK] 상품-공급처 매핑 {len(options)}건 생성 완료")


def seed_inventory(db, options: list[ProductOption], warehouse: Warehouse) -> dict[int, Inventory]:
    inventory_by_option: dict[int, Inventory] = {}
    stocked_at = NOW - timedelta(days=110)
    for option in options:
        initial_stock = random.randint(300, 600)
        inv = Inventory(
            product_option_id=option.id,
            warehouse_id=warehouse.id,
            sellable_stock=initial_stock,
            reserved_stock=0,
            safety_stock=20,
            updated_at=stocked_at,
        )
        db.add(inv)
        inventory_by_option[option.id] = inv
        db.add(
            InventoryTransaction(
                product_option_id=option.id,
                warehouse_id=warehouse.id,
                type="IN",
                quantity=initial_stock,
                from_status=None,  # 창고 외부에서 유입
                to_status=InventoryStatus.SELLABLE,
                reference_type="MANUAL",
                reference_id=None,
                memo="초기 입고(더미 데이터)",
                created_at=stocked_at,
            )
        )
    db.flush()
    print(f"[OK] 재고 {len(options)}건(SKU x 창고) 및 초기 입고 이력 생성 완료")
    return inventory_by_option


def seed_customers(db, platforms: list[Platform]) -> list[Customer]:
    customers = []
    for i in range(NUM_CUSTOMERS):
        platform = random.choice(platforms)
        name = random.choice(CUSTOMER_LAST_NAMES) + random.choice(CUSTOMER_FIRST_NAMES)
        created_at = NOW - timedelta(days=random.uniform(30, 180))
        customer = Customer(
            platform_id=platform.id,
            platform_customer_key=f"CUST-{i + 1:05d}",
            name=name,
            phone=f"010-{random.randint(1000, 9999)}-{random.randint(1000, 9999)}",
            email=f"customer{i + 1}@example.com",
            address=f"서울시 강남구 테헤란로 {random.randint(1, 400)}길",
            grade="일반",
            is_vip=False,
            total_purchase_amount=0,
            order_count=0,
            first_order_at=None,
            last_order_at=None,
            is_dormant=False,
            created_at=created_at,
            updated_at=created_at,
        )
        db.add(customer)
        customers.append(customer)
    db.flush()
    print(f"[OK] 고객 {len(customers)}명 생성 완료")
    return customers


class _CustomerStats(TypedDict):
    amount: float
    count: int
    first: Optional[datetime]
    last: Optional[datetime]


def seed_orders(
    db,
    customers: list[Customer],
    platforms: list[Platform],
    options_by_platform: dict[int, list[int]],
    option_meta: dict[int, dict],
    warehouse: Warehouse,
    inventory_by_option: dict[int, Inventory],
) -> list[dict]:
    customers_by_platform: dict[int, list[Customer]] = defaultdict(list)
    for c in customers:
        customers_by_platform[c.platform_id].append(c)

    all_option_ids = list(option_meta.keys())
    customer_stats: dict[int, _CustomerStats] = {
        c.id: {"amount": 0.0, "count": 0, "first": None, "last": None} for c in customers
    }
    reserved_qty: dict[int, int] = defaultdict(int)
    shipped_qty: dict[int, int] = defaultdict(int)
    # 반품 검수 결과(정책 5) - 합격은 sellable로, 불합격은 defective로 들어간다.
    inspected_pass_qty: dict[int, int] = defaultdict(int)
    inspected_defective_qty: dict[int, int] = defaultdict(int)

    created_orders: list[dict] = []
    range_start = NOW - timedelta(days=ORDER_DAYS_BACK)

    for seq in range(1, NUM_ORDERS + 1):
        platform = random.choice(platforms)
        order_date = random_time_between(range_start, NOW)
        status = weighted_choice(ORDER_STATUS_WEIGHTS)
        timeline = build_status_timeline(order_date, status)

        candidates = options_by_platform.get(platform.id) or all_option_ids
        n_items = random.randint(1, 3)
        chosen_option_ids = random.sample(candidates, k=min(n_items, len(candidates)))

        items = []
        subtotal = 0.0
        for option_id in chosen_option_ids:
            qty = random.randint(1, 3)
            meta = option_meta[option_id]
            unit_price = meta["base_price"]
            line_amount = round(unit_price * qty, 2)
            items.append(
                {
                    "option_id": option_id,
                    "qty": qty,
                    "unit_price": unit_price,
                    "cost_price": meta["cost_price"],
                    "line_amount": line_amount,
                }
            )
            subtotal += line_amount

        discount = round(subtotal * random.uniform(0.05, 0.15), 2) if random.random() < 0.3 else 0.0
        total_amount = round(subtotal - discount, 2)

        pool = customers_by_platform.get(platform.id)
        customer = random.choice(pool) if pool else None

        delivered_at = timeline_time_for(timeline, "DELIVERED")
        order = Order(
            platform_id=platform.id,
            platform_order_no=f"{platform.code[:4].upper()}-{seq:06d}",
            customer_id=customer.id if customer else None,
            status=status,
            order_date=order_date,
            payment_date=order_date + timedelta(minutes=random.uniform(1, 30)),
            delivery_completed_date=delivered_at,
            total_amount=total_amount,
            discount_amount=discount,
            created_at=order_date,
            updated_at=timeline[-1][1],
        )
        db.add(order)
        db.flush()

        item_objs = []
        for it in items:
            oi = OrderItem(
                order_id=order.id,
                product_option_id=it["option_id"],
                quantity=it["qty"],
                unit_price=it["unit_price"],
                cost_price_snapshot=it["cost_price"],
                line_amount=it["line_amount"],
            )
            db.add(oi)
            item_objs.append(oi)
        db.flush()

        prev_status = None
        for s, t in timeline:
            db.add(OrderStatusHistory(order_id=order.id, from_status=prev_status, to_status=s, changed_at=t))
            prev_status = s

        shipped = status in ("SHIPPING", "DELIVERED", "EXCHANGED", "RETURNED", "REFUNDED")
        if shipped:
            ship_time = timeline_time_for(timeline, "SHIPPING")
            # 배송은 주문과 1:1이 아니라 shipment_items를 통해 N:M으로 연결한다
            # (합포장/분할배송 지원). 더미 데이터는 주문 1건을 통째로 1회 발송하는
            # 가장 단순한 경우이므로, order_item_id를 비워 "주문 전체"를 의미하게 한다.
            db.add(
                Shipment(
                    carrier=random.choice(CARRIERS),
                    tracking_no=str(random.randint(100000000000, 999999999999)),
                    shipped_at=ship_time,
                    delivered_at=delivered_at,
                    status="DELIVERED" if delivered_at else "SHIPPING",
                    items=[ShipmentItem(order_id=order.id, order_item_id=None, quantity=None)],
                )
            )
            for it in items:
                shipped_qty[it["option_id"]] += it["qty"]
                db.add(
                    InventoryTransaction(
                        product_option_id=it["option_id"],
                        warehouse_id=warehouse.id,
                        type="OUT",
                        quantity=-it["qty"],
                        from_status=InventoryStatus.SELLABLE,
                        to_status=None,  # 창고 외부로 유출
                        reference_type="ORDER",
                        reference_id=order.id,
                        created_at=ship_time,
                    )
                )
        else:
            for it in items:
                reserved_qty[it["option_id"]] += it["qty"]

        if status == "CANCELED":
            db.add(
                Cancellation(
                    order_id=order.id,
                    reason=random.choice(CANCEL_REASONS),
                    refund_amount=total_amount,
                    status="COMPLETED",
                    requested_at=timeline[-1][1],
                    completed_at=timeline[-1][1] + timedelta(hours=1),
                )
            )
        elif status == "EXCHANGED":
            db.add(
                Exchange(
                    order_id=order.id,
                    order_item_id=item_objs[0].id,
                    reason=random.choice(EXCHANGE_REASONS),
                    status="COMPLETED",
                    requested_at=timeline[-1][1] - timedelta(days=1),
                    completed_at=timeline[-1][1],
                )
            )
        elif status in ("RETURNED", "REFUNDED"):
            ret_status = "RECEIVED" if status == "RETURNED" else "REFUNDED"
            ret = Return(
                order_id=order.id,
                order_item_id=item_objs[0].id,
                reason=random.choice(RETURN_REASONS),
                refund_amount=total_amount,
                status=ret_status,
                requested_at=timeline[-1][1] - timedelta(days=1),
                completed_at=timeline[-1][1],
            )
            db.add(ret)
            db.flush()
            # 정책 5: RECEIVED(회수 도착)는 재고를 움직이지 않는다. 검수를 통과해야
            # 비로소 재고가 된다. 더미 데이터도 이 순서를 그대로 따른다 -
            # RETURNED(=RECEIVED, 검수 대기)는 이력 없음, REFUNDED(검수 완료 후
            # 환불)만 INSPECT 이력을 남긴다.
            if ret_status == "REFUNDED":
                option_id = item_objs[0].product_option_id
                qty = item_objs[0].quantity
                # 검수 결과는 전량 양품이 아니다 - 일부는 불량으로 빠진다.
                is_defective = random.random() < DEFECTIVE_INSPECTION_RATE
                if is_defective:
                    inspected_defective_qty[option_id] += qty
                else:
                    inspected_pass_qty[option_id] += qty
                db.add(
                    InventoryTransaction(
                        product_option_id=option_id,
                        warehouse_id=warehouse.id,
                        type="INSPECT",
                        quantity=qty,
                        from_status=None,  # 검수 전 반품품은 아직 재고가 아니다
                        to_status=InventoryStatus.DEFECTIVE if is_defective else InventoryStatus.SELLABLE,
                        reference_type="RETURN",
                        reference_id=ret.id,
                        memo="반품 검수 불합격(더미 데이터)" if is_defective else "반품 검수 합격(더미 데이터)",
                        created_at=timeline[-1][1],
                    )
                )

        if customer:
            cs = customer_stats[customer.id]
            cs["amount"] += total_amount
            cs["count"] += 1
            if cs["first"] is None or order_date < cs["first"]:
                cs["first"] = order_date
            if cs["last"] is None or order_date > cs["last"]:
                cs["last"] = order_date

        created_orders.append(
            {
                "id": order.id,
                "platform_id": platform.id,
                "platform_code": platform.code,
                "order_date": order_date,
                "status": status,
                "total_amount": total_amount,
                "discount_amount": discount,
                "customer_id": customer.id if customer else None,
                "items": items,
            }
        )

    # 고객 캐시 통계 반영 (실제 서비스 레이어가 주문 확정/취소 시 갱신할 값의 스냅샷)
    dormant_days = settings.dormant_customer_days
    for customer in customers:
        stats = customer_stats[customer.id]
        customer.total_purchase_amount = round(stats["amount"], 2)
        customer.order_count = stats["count"]
        customer.first_order_at = stats["first"]
        customer.last_order_at = stats["last"]
        if stats["amount"] >= 500000:
            customer.grade = "VIP"
        elif stats["amount"] >= 200000:
            customer.grade = "우수"
        else:
            customer.grade = "일반"
        customer.is_vip = customer.grade == "VIP"
        customer.is_dormant = bool(stats["last"] and (NOW - stats["last"]).days >= dormant_days)

    # 재고 캐시 반영 - 위에서 만든 이력(IN/OUT/INSPECT)과 합이 맞아야 한다.
    #   sellable  = 초기입고 - 출고 + 검수합격
    #   defective = 검수불합격
    #   reserved  = 미출고 주문분
    # 불변조건 I1(reserved <= sellable)이 깨지지 않도록 reserved를 sellable로 상한한다.
    for option_id, inv in inventory_by_option.items():
        inv.sellable_stock = max(inv.sellable_stock - shipped_qty.get(option_id, 0), 0) + inspected_pass_qty.get(
            option_id, 0
        )
        inv.defective_stock = inspected_defective_qty.get(option_id, 0)
        inv.reserved_stock = min(reserved_qty.get(option_id, 0), inv.sellable_stock)
        inv.updated_at = NOW

    print(f"[OK] 주문 {len(created_orders)}건(+주문상품/상태이력/배송/교환/반품/취소/재고이동) 생성 완료")
    return created_orders


def seed_settlements(db, platforms: list[Platform], created_orders: list[dict]) -> None:
    orders_by_platform: dict[int, list[dict]] = defaultdict(list)
    for o in created_orders:
        if o["status"] in ("DELIVERED", "REFUNDED"):
            orders_by_platform[o["platform_id"]].append(o)

    settlement_count = 0
    detail_count = 0
    for platform in platforms:
        cycle_days = platform.settlement_cycle_days or 14
        fee_rate = FEE_RATE_BY_PLATFORM_CODE.get(platform.code, 0.10)
        cycle_end = NOW.date()
        for cycle_idx in range(3):
            cycle_start = cycle_end - timedelta(days=cycle_days)
            orders_in_cycle = [
                o for o in orders_by_platform.get(platform.id, []) if cycle_start <= o["order_date"].date() < cycle_end
            ]
            status = "COMPLETED" if cycle_idx > 0 else "SCHEDULED"
            gross_total = round(sum(o["total_amount"] for o in orders_in_cycle), 2)
            fee_total = round(gross_total * fee_rate, 2)
            net_total = round(gross_total - fee_total, 2)

            settlement = Settlement(
                platform_id=platform.id,
                settlement_cycle=f"{cycle_start.isoformat()}~{(cycle_end - timedelta(days=1)).isoformat()}",
                scheduled_date=cycle_end + timedelta(days=3),
                settled_date=cycle_end + timedelta(days=3) if status == "COMPLETED" else None,
                expected_amount=net_total,
                settled_amount=net_total if status == "COMPLETED" else 0.0,
                unsettled_amount=0.0 if status == "COMPLETED" else net_total,
                discrepancy_amount=0.0,
                status=status,
                created_at=NOW,
            )
            db.add(settlement)
            db.flush()
            settlement_count += 1

            for o in orders_in_cycle:
                order_fee = round(o["total_amount"] * fee_rate, 2)
                db.add(
                    SettlementDetail(
                        settlement_id=settlement.id,
                        order_id=o["id"],
                        order_item_id=None,
                        gross_amount=o["total_amount"],
                        fee_amount=order_fee,
                        net_amount=round(o["total_amount"] - order_fee, 2),
                    )
                )
                detail_count += 1

            cycle_end = cycle_start

    print(f"[OK] 정산 {settlement_count}건(정산 상세 {detail_count}건) 생성 완료")


def seed_costs(db, platforms: list[Platform], option_meta: dict[int, dict], created_orders: list[dict]) -> list[dict]:
    order_ids = [o["id"] for o in created_orders]
    option_ids = list(option_meta.keys())
    created_costs = []

    cost_defs = [
        ("SHIPPING", "VARIABLE", 2500, 3500),
        ("PACKAGING", "VARIABLE", 300, 800),
        ("PLATFORM_FEE", "VARIABLE", 5000, 50000),
        ("AD_AGENCY_FEE", "FIXED", 100000, 300000),
        ("RETURN_SHIPPING", "VARIABLE", 3000, 5000),
        ("ETC", "FIXED", 10000, 100000),
    ]
    for _ in range(NUM_COSTS):
        category, cost_type, low, high = random.choice(cost_defs)
        incurred_date = (NOW - timedelta(days=random.uniform(0, ORDER_DAYS_BACK))).date()
        amount = round(random.uniform(low, high), 2)
        platform_id = random.choice(platforms).id if category in ("PLATFORM_FEE", "AD_AGENCY_FEE") else None
        product_option_id = (
            random.choice(option_ids) if category in ("SHIPPING", "PACKAGING") and random.random() < 0.5 else None
        )
        order_id = (
            random.choice(order_ids) if category in ("SHIPPING", "RETURN_SHIPPING") and random.random() < 0.5 else None
        )

        db.add(
            Cost(
                category=category,
                cost_type=cost_type,
                platform_id=platform_id,
                product_option_id=product_option_id,
                order_id=order_id,
                amount=amount,
                incurred_date=incurred_date,
                memo=None,
                created_at=datetime.combine(incurred_date, datetime.min.time(), tzinfo=timezone.utc),
            )
        )
        created_costs.append({"category": category, "amount": amount, "incurred_date": incurred_date})

    print(f"[OK] 비용 {len(created_costs)}건 생성 완료")
    return created_costs


def seed_ad_campaigns(db, option_meta: dict[int, dict]) -> list[dict]:
    option_ids = list(option_meta.keys())
    campaigns = []
    for i in range(1, NUM_AD_CAMPAIGNS + 1):
        ad_platform_code = random.choice(AD_PLATFORM_CODES)
        option_id = random.choice(option_ids)
        campaign = AdCampaign(
            ad_platform_code=ad_platform_code,
            platform_campaign_id=f"CMP-{i:04d}",
            name=f"{ad_platform_code} 캠페인 {i}",
            product_option_id=option_id,
            is_active=True,
        )
        db.add(campaign)
        db.flush()
        campaigns.append({"id": campaign.id, "product_option_id": option_id})
    print(f"[OK] 광고 캠페인 {len(campaigns)}개 생성 완료")
    return campaigns


def seed_ad_performance(db, campaigns: list[dict]) -> list[dict]:
    created_perf = []
    for day_offset in range(AD_PERF_DAYS_BACK):
        stat_date = (NOW - timedelta(days=day_offset)).date()
        for campaign in campaigns:
            impressions = random.randint(300, 2500)
            clicks = random.randint(int(impressions * 0.01), int(impressions * 0.05))
            cost = round(clicks * random.uniform(100, 250), 2)
            conversions = random.randint(0, max(int(clicks * 0.08), 1))
            conversion_amount = round(conversions * random.uniform(15000, 40000), 2)

            db.add(
                AdPerformanceDaily(
                    campaign_id=campaign["id"],
                    stat_date=stat_date,
                    impressions=impressions,
                    clicks=clicks,
                    cost=cost,
                    conversions=conversions,
                    conversion_amount=conversion_amount,
                )
            )
            created_perf.append(
                {
                    "campaign_id": campaign["id"],
                    "product_option_id": campaign["product_option_id"],
                    "stat_date": stat_date,
                    "cost": cost,
                    "conversion_amount": conversion_amount,
                }
            )
    print(f"[OK] 일별 광고 성과 {len(created_perf)}건 생성 완료")
    return created_perf


def build_profit_loss_summary(
    db, created_orders: list[dict], created_costs: list[dict], created_ad_perf: list[dict]
) -> None:
    daily_revenue: dict[date, dict] = defaultdict(
        lambda: {"gross": 0.0, "net": 0.0, "count": 0, "cogs": 0.0, "fee": 0.0}
    )
    for o in created_orders:
        d = o["order_date"].date()
        stats = daily_revenue[d]
        stats["gross"] += o["total_amount"] + o["discount_amount"]
        stats["net"] += o["total_amount"]
        stats["count"] += 1
        stats["cogs"] += sum(it["cost_price"] * it["qty"] for it in o["items"])
        stats["fee"] += o["total_amount"] * FEE_RATE_BY_PLATFORM_CODE.get(o["platform_code"], 0.10)

    daily_cost: dict[date, dict] = defaultdict(lambda: {"shipping": 0.0, "packaging": 0.0, "other": 0.0})
    for c in created_costs:
        d = c["incurred_date"]
        if c["category"] == "SHIPPING":
            daily_cost[d]["shipping"] += c["amount"]
        elif c["category"] == "PACKAGING":
            daily_cost[d]["packaging"] += c["amount"]
        else:
            daily_cost[d]["other"] += c["amount"]

    daily_ad: dict[date, dict] = defaultdict(lambda: {"cost": 0.0, "conv_revenue": 0.0})
    for p in created_ad_perf:
        d = p["stat_date"]
        daily_ad[d]["cost"] += p["cost"]
        daily_ad[d]["conv_revenue"] += p["conversion_amount"]

    count = 0
    for day_offset in range(PL_SUMMARY_DAYS_BACK):
        d = (NOW - timedelta(days=day_offset)).date()
        rev = daily_revenue.get(d, {"gross": 0.0, "net": 0.0, "count": 0, "cogs": 0.0, "fee": 0.0})
        cost = daily_cost.get(d, {"shipping": 0.0, "packaging": 0.0, "other": 0.0})
        ad = daily_ad.get(d, {"cost": 0.0, "conv_revenue": 0.0})

        total_cost = round(rev["cogs"] + rev["fee"] + cost["shipping"] + cost["packaging"] + cost["other"], 2)
        net_profit = round(rev["net"] - total_cost - ad["cost"], 2)
        net_profit_rate = round((net_profit / rev["net"] * 100), 2) if rev["net"] > 0 else 0.0

        db.add(
            ProfitLossSummary(
                period_type="DAILY",
                basis_type="ORDER_DATE",
                period_key=d.isoformat(),
                platform_id=None,
                product_option_id=None,
                gross_revenue=round(rev["gross"], 2),
                net_revenue=round(rev["net"], 2),
                order_count=rev["count"],
                ad_cost=round(ad["cost"], 2),
                ad_conversion_revenue=round(ad["conv_revenue"], 2),
                cost_of_goods=round(rev["cogs"], 2),
                platform_fee=round(rev["fee"], 2),
                shipping_cost=round(cost["shipping"], 2),
                packaging_cost=round(cost["packaging"], 2),
                other_cost=round(cost["other"], 2),
                total_cost=total_cost,
                net_profit=net_profit,
                net_profit_rate=net_profit_rate,
                generated_at=NOW,
            )
        )
        count += 1
    print(f"[OK] 일별 매출/손익 요약(profit_loss_summary) {count}건 생성 완료")


def build_product_performance_summary(
    db, created_orders: list[dict], created_ad_perf: list[dict], option_meta: dict[int, dict]
) -> None:
    # 달력상 "이번 달"로 자르면 월초에는 거의 데이터가 없으므로,
    # 최근 30일 롤링 윈도우를 이번 달(period_key) 실적으로 집계한다.
    period_key = NOW.strftime("%Y-%m")
    window_start = (NOW - timedelta(days=PL_SUMMARY_DAYS_BACK)).date()

    stats: dict[int, dict] = defaultdict(
        lambda: {"qty": 0, "revenue": 0.0, "cogs": 0.0, "orders": set(), "returns": 0, "exchanges": 0, "cancels": 0}
    )
    for o in created_orders:
        if o["order_date"].date() < window_start:
            continue
        for it in o["items"]:
            s = stats[it["option_id"]]
            s["qty"] += it["qty"]
            s["revenue"] += it["line_amount"]
            s["cogs"] += it["cost_price"] * it["qty"]
            s["orders"].add(o["id"])
            if o["status"] == "RETURNED":
                s["returns"] += 1
            elif o["status"] == "EXCHANGED":
                s["exchanges"] += 1
            elif o["status"] == "CANCELED":
                s["cancels"] += 1

    ad_by_option: dict[int, dict] = defaultdict(lambda: {"cost": 0.0, "conv_revenue": 0.0})
    for p in created_ad_perf:
        if p["stat_date"] >= window_start:
            a = ad_by_option[p["product_option_id"]]
            a["cost"] += p["cost"]
            a["conv_revenue"] += p["conversion_amount"]

    count = 0
    for option_id, s in stats.items():
        order_total = len(s["orders"]) or 1
        ad = ad_by_option.get(option_id, {"cost": 0.0, "conv_revenue": 0.0})
        net_profit = round(s["revenue"] - s["cogs"], 2)
        roas = round((ad["conv_revenue"] / ad["cost"] * 100), 2) if ad["cost"] > 0 else 0.0

        db.add(
            ProductPerformanceSummary(
                period_type="MONTHLY",
                period_key=period_key,
                product_option_id=option_id,
                product_id=option_meta[option_id]["product_id"],
                platform_id=None,
                sales_qty=s["qty"],
                revenue=round(s["revenue"], 2),
                net_profit=net_profit,
                ad_cost=round(ad["cost"], 2),
                roas=roas,
                return_rate=round(s["returns"] / order_total * 100, 2),
                exchange_rate=round(s["exchanges"] / order_total * 100, 2),
                cancel_rate=round(s["cancels"] / order_total * 100, 2),
                generated_at=NOW,
            )
        )
        count += 1
    print(f"[OK] 상품별 성과 요약(product_performance_summary) {count}건 생성 완료")


def build_kpi_targets(db, created_orders: list[dict]) -> None:
    period_key = NOW.strftime("%Y-%m")
    window_start = (NOW - timedelta(days=PL_SUMMARY_DAYS_BACK)).date()
    month_orders = [o for o in created_orders if o["order_date"].date() >= window_start]
    revenue = sum(o["total_amount"] for o in month_orders)
    order_count = len(month_orders)
    aov = revenue / order_count if order_count else 0.0

    targets = [
        ("REVENUE", round(revenue * 1.2, 2)),
        ("NET_PROFIT", round(revenue * 0.15, 2)),
        ("AD_COST", round(revenue * 0.08, 2)),
        ("ROAS", 300.0),
        ("ORDER_COUNT", float(max(order_count, 1) * 1.2)),
        ("AOV", round(aov * 1.05, 2) if aov else 30000.0),
    ]
    for metric, target_value in targets:
        db.add(KpiTarget(period_key=period_key, metric=metric, target_value=target_value, created_at=NOW))
    print(f"[OK] 이달의 KPI 목표(kpi_targets) {len(targets)}건 생성 완료")


def main() -> None:
    setup_logging()
    print(f"[{NOW.isoformat()}] 더미 데이터 생성을 시작합니다.")

    with session_scope() as db:
        if db.execute(select(func.count()).select_from(Product)).scalar_one() > 0:
            print("[SKIP] 더미 데이터가 이미 존재합니다 (products 테이블에 데이터 있음).")
            return

        platforms = list(db.execute(select(Platform)).scalars().all())
        warehouse = db.execute(select(Warehouse)).scalars().first()
        admin = db.execute(select(User).where(User.username == "admin")).scalar_one_or_none()
        if not platforms or not warehouse or not admin:
            raise RuntimeError("기준 데이터가 없습니다. scripts/init_db.py를 먼저 실행하세요.")

        suppliers = seed_suppliers(db)
        products, options, option_meta = seed_products(db, admin)
        options_by_platform = seed_product_platform_maps(db, options, platforms)
        seed_product_supplier_map(db, options, suppliers)
        inventory_by_option = seed_inventory(db, options, warehouse)

        customers = seed_customers(db, platforms)
        created_orders = seed_orders(
            db, customers, platforms, options_by_platform, option_meta, warehouse, inventory_by_option
        )
        seed_settlements(db, platforms, created_orders)
        created_costs = seed_costs(db, platforms, option_meta, created_orders)
        campaigns = seed_ad_campaigns(db, option_meta)
        created_ad_perf = seed_ad_performance(db, campaigns)

        build_profit_loss_summary(db, created_orders, created_costs, created_ad_perf)
        build_product_performance_summary(db, created_orders, created_ad_perf, option_meta)
        build_kpi_targets(db, created_orders)

    print("더미 데이터 생성이 완료되었습니다.")


if __name__ == "__main__":
    main()
