"""
integrations/malls/base_mall_connector.py
---------------------------------------------
쇼핑몰 플랫폼 커넥터 공통 인터페이스.

Scheduler/Service 계층은 구체 커넥터 클래스가 아니라 이 인터페이스에만
의존한다. Platform.connector_class 값(예: "CoupangConnector")으로 실제
구현체를 동적으로 로딩할 때는 integrations.malls.get_mall_connector()를 사용한다.

반환 형식(정규화된 표준 딕셔너리)은 플랫폼마다 다른 원본 API 응답 형식을
흡수하여 이 표준 형태로 맞추는 것이 커넥터의 핵심 책임이다:

fetch_orders()/fetch_order_detail() 반환 항목:
    {
        "platform_order_no": str,
        "order_date": datetime,
        "status": str,  # NEW/PREPARING/SHIPPING/DELIVERED/CANCELED (models.order.Order.status와 동일 값)
        "customer_key": str,             # Customer.platform_customer_key에 대응
        "customer_name": Optional[str],
        "customer_phone": Optional[str],
        "total_amount": float,
        "discount_amount": float,
        "items": [
            {"platform_option_id": str, "quantity": int, "unit_price": float,
             "platform_shipment_box_id": Optional[str]},  # 쿠팡 배송묶음 ID(라인 단위). 없는 채널은 생략/None.
            ...
        ],
    }

fetch_settlements() 반환 항목:
    {
        "settlement_cycle": str, "settlement_type": Optional[str], "scheduled_date": date,
        "settled_date": Optional[date], "expected_amount": Decimal, "settled_amount": Decimal, "status": str,
    }

fetch_settlement_details() 반환 항목(상용 ERP 확장 2단계 - 정산 회차의 주문별 상세,
채널이 회차 요약과 별도 API로 준다면 그 결과를 그대로 정규화한다):
    {
        "platform_order_no": str, "platform_order_item_no": Optional[str],
        "sale_type": "SALE"|"REFUND", "recognition_date": Optional[date], "settled_date": Optional[date],
        "gross_amount": Decimal, "fee_amount": Decimal, "net_amount": Decimal,
    }
    금액 부호는 채널이 준 값을 그대로 따른다(REFUND가 음수일 수 있다 - 임의 반전 금지).

실제 API 키가 없으면(또는 session/platform_id가 주어지지 않으면) 각
구현체는 위 형식에 맞는 더미(가짜) 데이터를 생성한다. session과 platform_id를
함께 넘기면(운영 환경 등) settings.api_credentials에 등록된 실제 키가 있는지
확인해 있으면 실제 HTTP 연동을, 없으면 더미 폴백을 시도한다(NaverSmartstoreConnector
참고 - 나머지 플랫폼은 아직 더미 전용이며, 동일한 패턴으로 확장 가능).
"""

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from integrations.malls.errors import MarketplaceCapabilityUnsupportedError

ORDER_STATUSES = ["NEW", "PREPARING", "SHIPPING", "DELIVERED", "CANCELED"]
DUMMY_CUSTOMER_NAMES = ["김민준", "이서연", "박도윤", "최지우", "정하은", "강시우", "조수아"]
DUMMY_PRODUCT_PRICES = [12900.0, 15900.0, 19900.0, 24900.0, 29900.0, 39900.0, 59900.0]


@dataclass
class ShipmentSubmitResult:
    """submit_shipment()의 안전한 결과 요약 - 원본 응답 전문은 담지 않는다."""

    accepted: bool
    platform_result_code: Optional[str] = None  # 예: "OK" / 실패 사유 코드(짧은 문자열)


class BaseMallConnector(ABC):
    """모든 쇼핑몰 커넥터가 구현해야 하는 공통 인터페이스."""

    platform_code: str  # models.platform.Platform.code와 일치해야 한다.

    # 클레임(취소/반품/교환) 수집 지원 여부 - 채널별로 독립. 기본 False(미지원).
    # 실제 공식 API 구현이 있는 커넥터만 True로 오버라이드한다. 서비스는 이 플래그가
    # False면 fetch_* 를 호출하지 않는다(미지원과 "결과 0건"을 구분하기 위함).
    supports_cancellation_sync: bool = False
    supports_return_sync: bool = False
    supports_exchange_sync: bool = False
    # 송장(발송처리) 실 전송 지원 여부. 공식 API로 실구현이 있는 커넥터만 True로
    # 오버라이드한다 - services.shipment_dispatch_service가 이 플래그로 채널을
    # 건너뛸지(CapabilityUnsupported) 판단한다.
    supports_shipment_submit: bool = False
    # 정산 회차 요약/상세(주문 단위) 수집 지원 여부 - 상용 ERP 확장(2단계). 취소/반품/
    # 교환과 같은 원칙: capability가 False면 서비스가 fetch_*를 호출하지 않는다.
    supports_settlement_sync: bool = False
    supports_settlement_detail_sync: bool = False

    def _marketplace_code(self) -> str:
        """오류 메시지용 안전한 채널 식별자(Secret/PII 아님). platform_code가 없으면 클래스명."""
        return getattr(self, "platform_code", type(self).__name__)

    def __init__(self, session: Any = None, platform_id: Optional[int] = None) -> None:
        """session/platform_id는 실제 API 연동을 지원하는 커넥터(예: 네이버)만 사용한다.

        둘 다 주어지지 않으면(기본값) 기존과 동일하게 더미 데이터만 생성하므로
        이 파라미터를 모르는 기존 호출부/테스트는 전혀 영향받지 않는다.
        """
        self.session = session
        self.platform_id = platform_id

    @abstractmethod
    def fetch_orders(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        """기간 내 신규/변경 주문 목록을 정규화된 형식으로 조회한다."""
        raise NotImplementedError

    @abstractmethod
    def fetch_order_detail(self, platform_order_no: str) -> dict[str, Any]:
        """단일 주문 상세(주문상품 포함)를 정규화된 형식으로 조회한다."""
        raise NotImplementedError

    @abstractmethod
    def update_shipment(self, platform_order_no: str, carrier: str, tracking_no: str) -> bool:
        """(레거시) 송장 등록/배송 상태를 플랫폼에 반영한다. 성공 여부를 반환한다.

        상용 ERP 확장(1단계) 이후 신규 코드는 submit_shipment()를 사용한다 -
        이 메서드는 주문 단위(platform_order_no)만 받아 라인아이템 단위 부분출고를
        표현할 수 없다. 기존 abstract 계약이라 시그니처는 유지한다."""
        raise NotImplementedError

    def submit_shipment(
        self,
        platform_order_item_no: str,
        carrier_code: str,
        tracking_no: str,
        dispatch_date: date,
        platform_order_no: Optional[str] = None,
        platform_shipment_box_id: Optional[str] = None,
    ) -> ShipmentSubmitResult:
        """상품주문(라인아이템) 단위로 송장 정보를 채널에 전송한다(부분출고/분할배송 지원).

        기본 구현은 미지원 오류를 던진다(성공 위장 없음). supports_shipment_submit=True인
        커넥터만 오버라이드한다. carrier_code는 이미 채널별 코드로 정규화된 값이어야
        한다(integrations.malls.carrier_codes.normalize_carrier_code 참고) - 이 메서드는
        내부 코드를 다시 변환하지 않는다.

        platform_order_no/platform_shipment_box_id는 채널마다 필요 여부가 다르다
        (네이버는 productOrderId 하나로 충분하지만, 쿠팡은 orderId+shipmentBoxId+
        vendorItemId 세 값이 모두 필요하다 - CoupangConnector.submit_shipment 참고).
        필요 없는 채널은 무시한다."""
        raise MarketplaceCapabilityUnsupportedError(self._marketplace_code(), "shipment_submit")

    @abstractmethod
    def fetch_settlements(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        """기간 내 정산 내역을 정규화된 형식으로 조회한다."""
        raise NotImplementedError

    def fetch_cancellations(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        """기간 내 주문취소 내역을 정규화된 형식으로 조회한다.

        기본 구현은 미지원 오류를 던진다(빈 목록으로 "결과 0건"을 위장하지 않는다).
        supports_cancellation_sync=True인 커넥터만 오버라이드한다. 서비스는 capability가
        False면 이 메서드를 호출하지 않으므로, 이 raise는 직접 호출에 대한 2차 방어다.

        반환 형식(정규화, *는 2단계에서 추가된 선택 필드 - 채널이 제공하는 경우에만
        채우고, 제공하지 않으면 생략하거나 None으로 둔다. 임의로 추정해 채우지 않는다):
            {"platform_order_no": str, "reason": Optional[str],
             "status": "REQUESTED"|"COMPLETED"|"REVIEW", "requested_at": datetime,
             "refund_amount": Optional[float],
             "platform_claim_id": Optional[str],       # * 채널의 클레임 고유 ID(재수집 갱신/부분클레임 구분용)
             "raw_status": Optional[str],               # * 채널 원본 상태 코드(정규화 이전)
             "platform_order_item_no": Optional[str],   # * 상품주문/라인 식별자(OrderItem 연결용)
             "quantity": Optional[int],
             "shipping_fee": Optional[float],
             "fault_type": Optional[str]}              # * 귀책 주체(채널이 제공하는 경우에만)
        """
        raise MarketplaceCapabilityUnsupportedError(self._marketplace_code(), "cancellation_sync")

    def fetch_returns(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        """기간 내 반품 내역을 정규화된 형식으로 조회한다(기본: 미지원 오류).

        반환 형식(정규화, *는 fetch_cancellations 참고와 동일한 2단계 선택 필드):
            {"platform_order_no": str, "reason": Optional[str],
             "status": "REQUESTED"|"APPROVED"|"RECEIVED"|"REFUNDED"|"REJECTED"|"REVIEW",
             "requested_at": datetime, "refund_amount": Optional[float],
             "platform_claim_id": Optional[str], "raw_status": Optional[str],
             "platform_order_item_no": Optional[str], "quantity": Optional[int],
             "shipping_fee": Optional[float], "fault_type": Optional[str]}
        """
        raise MarketplaceCapabilityUnsupportedError(self._marketplace_code(), "return_sync")

    def fetch_exchanges(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        """기간 내 교환 내역을 정규화된 형식으로 조회한다(기본: 미지원 오류).

        반환 형식(정규화, *는 fetch_cancellations 참고와 동일한 2단계 선택 필드.
        fault_type은 교환 전용 - 채널이 귀책 주체를 제공하는 경우에만 원본값 그대로):
            {"platform_order_no": str, "reason": Optional[str],
             "status": "REQUESTED"|"APPROVED"|"SHIPPED"|"COMPLETED"|"REJECTED"|"REVIEW",
             "requested_at": datetime,
             "platform_claim_id": Optional[str], "raw_status": Optional[str],
             "platform_order_item_no": Optional[str], "quantity": Optional[int],
             "shipping_fee": Optional[float], "fault_type": Optional[str]}
        """
        raise MarketplaceCapabilityUnsupportedError(self._marketplace_code(), "exchange_sync")

    def fetch_settlement_details(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        """기간 내 정산 상세(주문 단위) 내역을 정규화된 형식으로 조회한다(기본: 미지원
        오류). supports_settlement_detail_sync=True인 커넥터만 오버라이드한다.

        반환 형식은 모듈 docstring의 fetch_settlement_details() 항목 참고."""
        raise MarketplaceCapabilityUnsupportedError(self._marketplace_code(), "settlement_detail_sync")

    def fetch_products(self) -> list[dict[str, Any]]:
        """상품 목록을 정규화된 형식으로 조회한다(services.product_sync_service.
        ProductSyncService.sync_products_from_naver 참고). 상품은 이 메서드를 통해서만
        생성/갱신되어야 하며, 주문 API(fetch_orders)에서는 더 이상 상품을 만들지 않는다.

        상품 API 연동을 아직 지원하지 않는 플랫폼(더미 커넥터 등)은 기본값인 이
        구현을 그대로 상속해 예외를 던진다 - abstractmethod로 강제하면 아직
        상품 API가 없는 플랫폼(쿠팡/카카오/11번가/ESM)의 더미 커넥터도 전부 구현체를
        추가해야 해서, 실제 지원하는 커넥터(네이버)만 오버라이드하는 편이 낫다.

        반환 형식: [{"product_name", "category", "brand", "manufacturer",
        "images": {"representative_url", "optional_urls": [...]},
        "items": [{"platform_option_id"(옵션번호), "platform_product_id"(상품번호),
                   "option_name", "seller_product_code", "sale_price", "is_selling",
                   "option_image_url"}, ...]}, ...]
        """
        raise NotImplementedError(f"{self.platform_code} 플랫폼은 아직 상품 API 연동을 지원하지 않습니다.")

    def _dummy_seed_orders(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        """개발/테스트용 더미 주문 시드를 생성한다 (플랫폼 공통 내부 유틸).

        실제 fetch_orders()는 이 시드를 각 플랫폼 고유의 원본 응답 형식으로
        감싼 뒤(raw), 다시 표준 형식으로 정규화(normalize)하는 두 단계를 거친다.
        실제 API 연동 시에는 이 메서드 호출부만 실제 HTTP 요청으로 교체하면 된다.
        """
        rng = random.Random(f"{self.platform_code}:{start_date.isoformat()}:{end_date.isoformat()}")
        days = max((end_date - start_date).days, 1)
        seeds = []
        for i in range(rng.randint(0, 3) * days):
            offset_days = rng.randint(0, max(days - 1, 0))
            order_date = datetime.combine(
                start_date + timedelta(days=offset_days), datetime.min.time(), tzinfo=timezone.utc
            ) + timedelta(hours=rng.uniform(0, 23), minutes=rng.uniform(0, 59))
            seeds.append(
                {
                    "seq": i,
                    "order_date": order_date,
                    "status": rng.choice(ORDER_STATUSES),
                    "customer_key": f"{self.platform_code.upper()}-{rng.randint(10000, 99999)}",
                    "customer_name": rng.choice(DUMMY_CUSTOMER_NAMES),
                    "customer_phone": f"010-{rng.randint(1000, 9999)}-{rng.randint(1000, 9999)}",
                    "product_code": f"{self.platform_code.upper()}-{rng.randint(100000, 999999)}",
                    "quantity": rng.randint(1, 3),
                    "unit_price": rng.choice(DUMMY_PRODUCT_PRICES),
                }
            )
        return seeds

    def _dummy_single_order(self, platform_order_no: str) -> dict[str, Any]:
        """platform_order_no로부터 결정적(deterministic)인 더미 주문 1건을 만든다.

        같은 주문번호로 다시 조회해도 항상 같은 결과가 나오도록 시드를 고정한다.
        """
        rng = random.Random(f"{self.platform_code}:{platform_order_no}")
        order_date = datetime.now(timezone.utc) - timedelta(days=rng.uniform(0, 30))
        n_items = rng.randint(1, 3)
        items: list[dict[str, Any]] = [
            {
                "platform_option_id": f"{self.platform_code.upper()}-{rng.randint(100000, 999999)}",
                "quantity": rng.randint(1, 3),
                "unit_price": rng.choice(DUMMY_PRODUCT_PRICES),
            }
            for _ in range(n_items)
        ]
        total_amount = round(sum(it["quantity"] * it["unit_price"] for it in items), 2)
        return {
            "platform_order_no": platform_order_no,
            "order_date": order_date,
            "status": rng.choice(ORDER_STATUSES),
            "customer_key": f"{self.platform_code.upper()}-{rng.randint(10000, 99999)}",
            "customer_name": rng.choice(DUMMY_CUSTOMER_NAMES),
            "customer_phone": f"010-{rng.randint(1000, 9999)}-{rng.randint(1000, 9999)}",
            "total_amount": total_amount,
            "discount_amount": 0.0,
            "items": items,
        }

    def _dummy_settlements(self, start_date: date, end_date: date, cycle_days: int) -> list[dict[str, Any]]:
        rng = random.Random(f"{self.platform_code}:settlement:{start_date.isoformat()}:{end_date.isoformat()}")
        settlements = []
        cursor = start_date
        while cursor < end_date:
            cycle_end = min(cursor + timedelta(days=cycle_days), end_date)
            amount = round(rng.uniform(500000, 5000000), 2)
            settlements.append(
                {
                    "settlement_cycle": f"{cursor.isoformat()}~{cycle_end.isoformat()}",
                    "scheduled_date": cycle_end + timedelta(days=3),
                    "settled_date": cycle_end + timedelta(days=3) if cycle_end < end_date else None,
                    "expected_amount": amount,
                    "settled_amount": amount if cycle_end < end_date else 0.0,
                    "status": "COMPLETED" if cycle_end < end_date else "SCHEDULED",
                }
            )
            cursor = cycle_end
        return settlements
