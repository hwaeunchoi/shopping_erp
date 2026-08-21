"""
integrations/malls/coupang_connector.py
-------------------------------------------
쿠팡 커넥터 (Platform.connector_class == "CoupangConnector"와 매핑).

네이버(NaverSmartstoreConnector)와 동일한 원칙을 따른다:
api_credentials(SETTINGS_MANAGE 화면에서 등록)에 owner_type="PLATFORM",
owner_id=<이 플랫폼의 id>로 "access_key"/"secret_key"/"vendor_id" 3개가
모두 등록돼 있으면 실제 쿠팡 WING Open API(발주서 목록 조회)를 호출한다.
인증정보가 없거나 불완전하면 더미로 폴백하지 않고 MarketplaceCredentialMissingError를
던진다(운영 경로에 더미 주문이 유입되지 않게 한다). 주문상세·정산·상품은 아직
미구현이라 MarketplaceCapabilityUnsupportedError를 던진다(빈 목록으로 위장하지 않는다).

쿠팡 API의 특성(네이버와 다른 점):
1. 인증은 OAuth 토큰이 아니라 요청마다 만드는 HMAC-SHA256 서명(CEA)이다.
   서명 대상 메시지 = signed-date(GMT, yyMMddTHHmmssZ) + HTTP메서드 + 경로 + 쿼리.
   서명에 쓴 쿼리 문자열과 실제 전송 쿼리가 한 글자라도 다르면 401이 나므로,
   httpx의 파라미터 재정렬을 피하려 쿼리를 직접 만들어 URL에 붙인다.
2. 주문조회 경로에 vendorId(업체코드, 예: "A00012345")가 반드시 들어간다 -
   access_key/secret_key만으로는 경로를 구성할 수 없다.
3. 응답 data[]의 각 원소는 "배송 묶음(shipment box)" 1건이며, 그 안의
   orderItems[]가 라인아이템이다. 하나의 주문(orderId)이 여러 배송묶음으로
   나뉠 수 있으므로 orderId로 그룹핑해 ERP의 Order(1):OrderItem(N)에 맞춘다.
4. 쿠팡은 개인정보 보호로 "구매자 고유 ID"를 제공하지 않는다. 그래서
   customer_key로 쓸 안정적 식별자가 없어, 부득이 orderId를 customer_key로
   사용한다 - 이 경우 주문 1건이 곧 고객 1명으로 잡혀 고객 통계가 부정확해질
   수 있다(⚠️ 알려진 한계, 네이버처럼 cross-order 식별자가 없음). 향후 쿠팡이
   구매자 식별자를 제공하거나 수취인 정보 기반 매칭 정책이 정해지면 여기만 고친다.
"""

import hashlib
import hmac
import logging
import time as time_module
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from integrations.malls.base_mall_connector import BaseMallConnector
from integrations.malls.errors import (
    MarketplaceCapabilityUnsupportedError,
    MarketplaceCredentialMissingError,
    MarketplaceExternalAPIError,
    external_call,
    raise_for_status,
)
from services.settings_service import ApiCredentialService

logger = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))

# 실제 쿠팡 배송묶음 status -> ERP 표준 상태(models.order.Order.status).
# 쿠팡 상태값: ACCEPT(결제완료)/INSTRUCT(상품준비중)/DEPARTURE(배송지시)/
# DELIVERING(배송중)/FINAL_DELIVERY(배송완료)/CANCEL(취소).
_LIVE_STATUS_TO_STD = {
    "ACCEPT": "NEW",
    "INSTRUCT": "PREPARING",
    "DEPARTURE": "SHIPPING",
    "DELIVERING": "SHIPPING",
    "FINAL_DELIVERY": "DELIVERED",
    "CANCEL": "CANCELED",
}

COUPANG_API_BASE = "https://api-gateway.coupang.com"
ORDERSHEET_PATH_TMPL = "/v2/providers/openapi/apis/api/v4/vendors/{vendor_id}/ordersheets"
ORDERSHEET_MAX_PER_PAGE = 50
# 쿠팡 발주서(timeFrame) 조회는 from/to 최대 31일 범위만 허용한다 - 더 긴 범위는
# 31일 단위로 잘라 호출한다.
ORDERSHEET_MAX_RANGE_DAYS = 31
# 쿠팡 발주서 조회는 status가 **필수**이며, 한 번의 호출은 한 상태의 주문만 준다.
# 그래서 신규~배송완료까지 상태별로 각각 조회해 합친다(같은 주문이 여러 배송묶음으로
# 상태가 갈리면 normalize 단계에서 orderId로 묶는다). 취소는 발주서에 나오지 않아
# 여기 포함하지 않는다(쿠팡의 별도 취소 API 소관).
ORDERSHEET_STATUSES = ["ACCEPT", "INSTRUCT", "DEPARTURE", "DELIVERING", "FINAL_DELIVERY"]

# HTTP 429(Rate Limit) 재시도 정책: 1s -> 2s -> 4s -> 8s -> 16s 지수 백오프.
RATE_LIMIT_MAX_RETRIES = 5
RATE_LIMIT_BACKOFF_BASE_SECONDS = 1.0


class CoupangConnector(BaseMallConnector):
    platform_code = "coupang"

    def __init__(
        self, session: Any = None, platform_id: Optional[int] = None, http_client: Optional[httpx.Client] = None
    ) -> None:
        super().__init__(session=session, platform_id=platform_id)
        # http_client는 단위 테스트에서 실제 네트워크 호출 없이 모의 응답을 주입하기 위한 것이다.
        self._http_client = http_client

    def fetch_orders(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        credentials = self._get_credentials()
        if credentials is None:
            raise MarketplaceCredentialMissingError("coupang")
        access_key, secret_key, vendor_id = credentials
        raw_boxes = self._fetch_raw_orders_live(start_date, end_date, access_key, secret_key, vendor_id)
        return self._normalize_live_orders(raw_boxes)

    def fetch_order_detail(self, platform_order_no: str) -> dict[str, Any]:
        # 주문 상세 단건 조회는 아직 미구현 - 더미로 위장하지 않고 미지원 오류를 던진다.
        raise MarketplaceCapabilityUnsupportedError("coupang", "order_detail")

    def update_shipment(self, platform_order_no: str, carrier: str, tracking_no: str) -> bool:
        # ⚠️ 미구현 스텁(실 API 미호출). 현재 자동 호출 경로 없음 - 안전화는 S3에서 처리한다.
        return True

    def fetch_settlements(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        # 정산 연동 미구현 - 더미로 위장하지 않고 미지원 오류를 던진다.
        raise MarketplaceCapabilityUnsupportedError("coupang", "settlement")

    def fetch_products(self) -> list[dict[str, Any]]:
        # 상품 연동 미구현 - 빈 목록(정상 0건 위장) 대신 미지원 오류를 던진다.
        raise MarketplaceCapabilityUnsupportedError("coupang", "products")

    # --- 실제 API 연동 ---

    def _get_credentials(self) -> Optional[tuple[str, str, str]]:
        """access_key/secret_key/vendor_id가 모두 등록돼 있으면 반환하고, 아니면 None.

        None인 경우 호출부(fetch_orders)가 더미로 폴백하지 않고 인증정보 누락 오류를 던진다."""
        if self.session is None or self.platform_id is None:
            return None
        credential_service = ApiCredentialService(self.session)
        access_key = credential_service.get_decrypted("PLATFORM", self.platform_id, "access_key")
        secret_key = credential_service.get_decrypted("PLATFORM", self.platform_id, "secret_key")
        vendor_id = credential_service.get_decrypted("PLATFORM", self.platform_id, "vendor_id")
        if not access_key or not secret_key or not vendor_id:
            return None
        return access_key, secret_key, vendor_id

    def _http(self) -> httpx.Client:
        if self._http_client is None:
            self._http_client = httpx.Client(base_url=COUPANG_API_BASE, timeout=10.0)
        return self._http_client

    @staticmethod
    def _authorization(access_key: str, secret_key: str, method: str, path: str, query: str) -> str:
        """쿠팡 HMAC 서명(CEA) 헤더를 만든다.

        서명 대상 메시지 = signed-date + method + path + query. signed-date는
        GMT 기준 'yyMMddTHHmmssZ' 포맷이고, query는 '?'를 뺀 쿼리 문자열이다
        (실제 전송하는 쿼리와 반드시 동일해야 한다).
        """
        signed_date = datetime.now(timezone.utc).strftime("%y%m%dT%H%M%SZ")
        message = signed_date + method + path + query
        signature = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
        return (
            f"CEA algorithm=HmacSHA256, access-key={access_key}, " f"signed-date={signed_date}, signature={signature}"
        )

    def _request_with_retry(self, method: str, url: str, headers: dict[str, str]) -> httpx.Response:
        """HTTP 429(Rate Limit)에 대해 지수 백오프(1s -> 2s -> 4s -> 8s -> 16s)로
        최대 RATE_LIMIT_MAX_RETRIES회 재시도한다. 재시도 소진 시 RATE_LIMITED 오류를
        던진다. 네트워크/연결 오류는 external_call이 안전한 오류로 변환한다.

        로그에는 요청 URL(경로에 vendorId 포함)을 남기지 않는다 - 안전한 메타데이터만."""
        attempt = 0
        while True:
            with external_call("coupang"):
                response = self._http().request(method, url, headers=headers)
            if response.status_code != 429:
                return response
            attempt += 1
            if attempt > RATE_LIMIT_MAX_RETRIES:
                raise MarketplaceExternalAPIError("coupang", "RATE_LIMITED", True, http_status=429)
            retry_after = response.headers.get("Retry-After")
            if retry_after is not None:
                try:
                    wait_seconds = float(retry_after)
                except ValueError:
                    wait_seconds = RATE_LIMIT_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            else:
                wait_seconds = RATE_LIMIT_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "쿠팡 API 429(Rate Limit) - %s초 대기 후 재시도 (%d/%d)", wait_seconds, attempt, RATE_LIMIT_MAX_RETRIES
            )
            time_module.sleep(wait_seconds)

    def _fetch_raw_orders_live(
        self, start_date: date, end_date: date, access_key: str, secret_key: str, vendor_id: str
    ) -> list[dict[str, Any]]:
        """발주서(배송묶음) 목록을 실제 쿠팡 API로 조회한다.

        쿠팡 발주서 조회는 status가 필수이고 한 번에 한 상태만 준다. 그래서
        상태(ORDERSHEET_STATUSES)별로 각각, from/to 최대 31일 제약에 맞춰 31일
        단위로 나눠, nextToken 페이지네이션으로 끝까지 순회한다.
        """
        path = ORDERSHEET_PATH_TMPL.format(vendor_id=vendor_id)
        raw_boxes: list[dict[str, Any]] = []

        for status_value in ORDERSHEET_STATUSES:
            window_start = start_date
            while window_start < end_date:
                window_end = min(window_start + timedelta(days=ORDERSHEET_MAX_RANGE_DAYS), end_date)
                next_token = ""
                while True:
                    # 쿼리 파라미터 순서를 고정한다 - 서명에 쓴 쿼리와 전송 쿼리가 동일해야 한다.
                    params: list[tuple[str, str]] = [
                        ("createdAtFrom", window_start.isoformat()),
                        ("createdAtTo", window_end.isoformat()),
                        ("status", status_value),
                        ("maxPerPage", str(ORDERSHEET_MAX_PER_PAGE)),
                    ]
                    if next_token:
                        params.append(("nextToken", next_token))
                    query = urlencode(params)
                    authorization = self._authorization(access_key, secret_key, "GET", path, query)
                    response = self._request_with_retry(
                        "GET",
                        f"{path}?{query}",
                        headers={"Authorization": authorization, "X-EXTENDED-Timeout": "90000"},
                    )
                    # 비200은 응답 본문을 노출하지 않고 안전한 외부 API 오류로 변환한다.
                    raise_for_status("coupang", response.status_code)
                    with external_call("coupang"):
                        payload = response.json()
                        raw_boxes.extend(payload.get("data", []) or [])
                        next_token = payload.get("nextToken") or ""
                    if not next_token:
                        break
                window_start = window_end

        return raw_boxes

    @staticmethod
    def _normalize_live_orders(boxes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """실 API 응답(배송묶음 단위)을 orderId로 그룹핑해 Order(1):OrderItem(N)으로 정규화한다.

        상태는 배송묶음 단위로 내려오므로(한 주문이 여러 배송묶음으로 나뉘면 상태가
        갈릴 수 있음), ERP의 단일 Order.status에는 첫 배송묶음의 상태를 대표값으로 쓴다.
        """
        grouped: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        for box in boxes:
            order_id = box.get("orderId")
            if order_id is None:
                continue
            grouped[order_id].append(box)

        normalized = []
        for order_id, order_boxes in grouped.items():
            first_box = order_boxes[0]
            raw_status = first_box.get("status") or ""
            std_status = _LIVE_STATUS_TO_STD.get(raw_status)
            if std_status is None:
                # 주문번호(orderId)는 로그에 남기지 않는다 - 상태값만 안전하게 기록한다.
                logger.warning("알 수 없는 쿠팡 status: %s - NEW로 처리", raw_status)
                std_status = "NEW"

            orderer = first_box.get("orderer") or {}
            items = []
            total_amount = 0.0
            discount_amount = 0.0
            for box in order_boxes:
                for oi in box.get("orderItems", []) or []:
                    quantity = int(oi.get("shippingCount", 0) or 0)
                    unit_price = float(oi.get("salesPrice", 0) or 0)
                    total_amount += unit_price * quantity
                    discount_amount += float(oi.get("discountPrice", 0) or 0)
                    items.append(
                        {
                            # 옵션 단위 식별자 - product_platform_map.platform_option_id와 매칭 키가 일치해야 한다.
                            "platform_option_id": (
                                str(oi["vendorItemId"]) if oi.get("vendorItemId") is not None else None
                            ),
                            "quantity": quantity,
                            "unit_price": unit_price,
                            # 자동매칭 참고 정보(주문 API에서는 새 상품을 만들지 않는다).
                            "platform_product_id": (
                                str(oi["sellerProductId"]) if oi.get("sellerProductId") is not None else None
                            ),
                            "product_name": oi.get("sellerProductName"),
                            "option_name": oi.get("vendorItemName") or oi.get("sellerProductItemName"),
                            "seller_product_code": oi.get("externalVendorSkuCode"),
                            "category": None,
                            "brand": None,
                            "manufacturer": None,
                        }
                    )

            ordered_at = first_box.get("orderedAt") or first_box.get("paidAt")
            # 배송지(수취인) 정보 - 쿠팡 배송묶음의 receiver. 주소는 addr1+addr2를 합친다.
            receiver = first_box.get("receiver") or {}
            addr = " ".join(x for x in [receiver.get("addr1"), receiver.get("addr2")] if x) or None
            normalized.append(
                {
                    "platform_order_no": str(order_id),
                    "order_date": _parse_coupang_datetime(ordered_at),
                    "status": std_status,
                    # 쿠팡은 구매자 고유 ID를 주지 않는다(위 docstring 4번 참고) - orderId를 대용한다.
                    "customer_key": str(order_id),
                    "customer_name": orderer.get("name"),
                    "customer_phone": orderer.get("safeNumber"),
                    "total_amount": round(total_amount, 2),
                    "discount_amount": round(discount_amount, 2),
                    "receiver_name": receiver.get("name"),
                    "receiver_phone": receiver.get("safeNumber") or receiver.get("receiverNumber"),
                    "receiver_zipcode": receiver.get("postCode"),
                    "receiver_address": addr,
                    "delivery_message": first_box.get("parcelPrintMessage"),
                    "items": items,
                }
            )
        return normalized


def _parse_coupang_datetime(value: Optional[str]) -> datetime:
    """쿠팡 orderedAt(예: "2026-07-01T09:00:00", KST·오프셋 없음)을 KST datetime으로 파싱한다.

    오프셋이 붙어 있으면 그대로 존중하고, 없으면 KST로 간주한다. 값이 없으면
    현재 시각(KST)으로 폴백한다.
    """
    if not value:
        return datetime.now(_KST)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_KST)
    return parsed
