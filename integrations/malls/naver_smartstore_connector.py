"""
integrations/malls/naver_smartstore_connector.py
-----------------------------------------------------
네이버 스마트스토어 커넥터 (models.platform.Platform.connector_class ==
"NaverSmartstoreConnector"와 매핑).

운영 준비 5순위-2단계(실제 쇼핑몰 API 연동) 대상으로 이 플랫폼 하나를
선정했다. api_credentials(SETTINGS_MANAGE 화면에서 등록)에 owner_type=
"PLATFORM", owner_id=<이 플랫폼의 id>로 "client_id"/"client_secret" 키가
모두 등록돼 있으면 실제 네이버 커머스 API(OAuth2 client_credentials)를
호출하고, 없으면(개발/테스트 환경 기본값) 더미 데이터로 폴백한다.

실제 운영 환경(실 API 키)으로 검증하며 확인한 사항(더미로는 알 수 없었던 것들):
1. 날짜 파라미터(from/to)는 날짜만으로는 400(ISO-8601 포맷 오류) - 전체
   datetime(밀리초+KST 오프셋, 예: "2026-07-01T00:00:00.000+09:00")이 필요하다.
2. from/to는 최대 24시간 차이만 허용한다 - 여러 날짜 조회는 하루 단위로
   나눠 호출해야 한다(_fetch_raw_orders_live 참고).
3. 응답은 payload.data.orders가 아니라 payload.data.contents 배열이며, 이
   배열의 각 원소는 "주문 전체"가 아니라 "개별 상품주문(라인아이템) 1건"이다
   (content.order = 주문 전체 공통정보, content.productOrder = 그 라인아이템
   1건의 상품/금액/배송 정보). 실제 "주문번호"(엑셀 내보내기의 "주문번호"
   컬럼과 일치)는 content.order.orderId이며, content.productOrder.
   productOrderId는 그 주문 내 낱개 상품주문번호(하나의 주문에 여러 개
   있을 수 있음)다. 그래서 정규화 시 content.order.orderId로 그룹핑해
   ERP의 Order(1) : OrderItem(N) 구조에 맞춘다(_normalize_live_orders).
4. 상품 검색 API(fetch_products, /external/v1/products/search)의 실제 응답은
   당초 가정했던 detailAttribute.optionInfo.optionCombinations 구조가 아니라
   {groupProductNo, originProductNo, channelProducts: [...]} 형태이고, 색상/
   사이즈가 다른 변형은 이미 서로 다른 originProductNo를 가진 별도 항목으로
   내려오며 groupProductNo가 그 변형들을 묶는 그룹 키다(_normalize_live_products
   참고). channelProductNo가 주문 API의 itemNo와 실제로 대응하는지는 아직
   미검증이다(레이트 리밋으로 추가 확인 보류) - 상품 동기화 후 주문 자동매칭이
   계속 실패하면 이 대응 관계부터 의심해야 한다.
"""

import base64
import json
import logging
import time as time_module
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import bcrypt
import httpx

from integrations.malls.base_mall_connector import BaseMallConnector
from services.settings_service import ApiCredentialService

logger = logging.getLogger(__name__)

# 네이버 커머스 API는 날짜만으로는(예: "2026-07-01") 400(날짜 포맷 오류)을 반환한다 -
# 실제 운영 환경에서 확인된 오류: "날짜 포맷을 확인해주세요. 유효한 ISO-8601 포맷이
# 아닙니다." 전체 datetime(밀리초+KST 오프셋)을 요구한다.
_KST = timezone(timedelta(hours=9))

# 더미 커넥터(개발/테스트 기본값) 전용 상태 매핑 - 실 API 상태 매핑은 _LIVE_STATUS_TO_STD 참고.
_STATUS_TO_RAW = {
    "NEW": "PAYED",
    "PREPARING": "PREPARED",
    "SHIPPING": "DELIVERING",
    "DELIVERED": "DELIVERED",
    "CANCELED": "CANCELED",
}
_RAW_TO_STATUS = {raw: std for std, raw in _STATUS_TO_RAW.items()}

# 실제 네이버 커머스 API productOrderStatus -> ERP 표준 상태(models.order.Order.status).
# 실운영 환경에서 관측된 값: PAYED/DELIVERING/PURCHASE_DECIDED/DELIVERED/RETURNED/CANCELED.
_LIVE_STATUS_TO_STD = {
    "PAYED": "NEW",
    "DISPATCH_REQUESTED": "PREPARING",  # 발송 준비(관측되진 않았으나 공식 문서상 존재하는 상태) - 확인 필요
    "DELIVERING": "SHIPPING",
    "DELIVERED": "DELIVERED",
    "PURCHASE_DECIDED": "DELIVERED",  # 구매확정 - ERP에는 별도 상태가 없어 DELIVERED로 취급
    "CANCELED": "CANCELED",
    "RETURNED": "RETURNED",
}

NAVER_API_BASE = "https://api.commerce.naver.com"
TOKEN_PATH = "/external/v1/oauth2/token"
ORDER_LIST_PATH = "/external/v1/pay-order/seller/product-orders"
# 네이버 커머스 API 공식 문서 기준으로 작성했으나, 주문 API와 달리 아직 실제 계정으로
# 응답 스키마를 검증하지 못했다 - 실 연동 시 필드명/페이지네이션 방식이 다르면
# _fetch_raw_products_live/_normalize_live_products만 수정하면 된다(서비스 계층은
# 이 커넥터가 반환하는 정규화된 dict 형식에만 의존한다).
PRODUCT_SEARCH_PATH = "/external/v1/products/search"
PRODUCT_PAGE_SIZE = 100

# HTTP 429(Rate Limit) 재시도 정책: Retry-After 헤더가 있으면 그 값을, 없으면
# 1s -> 2s -> 4s -> 8s -> 16s 지수 백오프로 대기 후 재시도한다.
RATE_LIMIT_MAX_RETRIES = 5
RATE_LIMIT_BACKOFF_BASE_SECONDS = 1.0


class NaverSmartstoreConnector(BaseMallConnector):
    platform_code = "naver_smartstore"

    def __init__(
        self, session: Any = None, platform_id: Optional[int] = None, http_client: Optional[httpx.Client] = None
    ) -> None:
        super().__init__(session=session, platform_id=platform_id)
        # http_client는 단위 테스트에서 실제 네트워크 호출 없이 모의(mock) 응답을 주입하기 위한 것이다.
        self._http_client = http_client
        # 동일 커넥터 인스턴스(=한 번의 실행) 내에서 fetch_products()가 여러 번 호출돼도
        # groupProductNo 기준으로 이미 조회한 상품은 API를 다시 호출하지 않도록 캐시한다.
        self._product_cache: Optional[list[dict[str, Any]]] = None

    def fetch_orders(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        credentials = self._get_credentials()
        if credentials is not None:
            client_id, client_secret = credentials
            raw_contents = self._fetch_raw_orders_live(start_date, end_date, client_id, client_secret)
            return self._normalize_live_orders(raw_contents)
        raw_orders = self._fetch_raw_orders_dummy(start_date, end_date)
        return [self._normalize_dummy(raw) for raw in raw_orders]

    def fetch_order_detail(self, platform_order_no: str) -> dict[str, Any]:
        return self._dummy_single_order(platform_order_no)

    def update_shipment(self, platform_order_no: str, carrier: str, tracking_no: str) -> bool:
        return True

    def fetch_settlements(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        return self._dummy_settlements(start_date, end_date, cycle_days=7)

    def fetch_products(self) -> list[dict[str, Any]]:
        """상품(원본상품 + 채널상품 + 옵션조합) 목록을 정규화된 형식으로 조회한다.

        services.product_sync_service.ProductSyncService.sync_products_from_naver가
        이 결과를 사용해 상품/옵션/플랫폼매핑/이미지를 등록·갱신한다 - 상품이 생기는
        유일한 경로다(주문 API에서는 더 이상 상품이 생기지 않는다).
        """
        if self._product_cache is not None:
            return self._product_cache
        credentials = self._get_credentials()
        if credentials is not None:
            client_id, client_secret = credentials
            raw_pages = self._fetch_raw_products_live(client_id, client_secret)
            result = self._normalize_live_products(raw_pages)
        else:
            result = self._dummy_products()
        self._product_cache = result
        return result

    # --- 실제 API 연동 ---

    def _get_credentials(self) -> Optional[tuple[str, str]]:
        """api_credentials에 client_id/client_secret이 모두 등록돼 있으면 반환하고, 아니면 None(더미 폴백)."""
        if self.session is None or self.platform_id is None:
            return None
        credential_service = ApiCredentialService(self.session)
        client_id = credential_service.get_decrypted("PLATFORM", self.platform_id, "client_id")
        client_secret = credential_service.get_decrypted("PLATFORM", self.platform_id, "client_secret")
        if not client_id or not client_secret:
            return None
        return client_id, client_secret

    def _http(self) -> httpx.Client:
        if self._http_client is None:
            self._http_client = httpx.Client(base_url=NAVER_API_BASE, timeout=10.0)
        return self._http_client

    def _request_with_retry(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """HTTP 429(Rate Limit)에 대해 Retry-After 헤더(있으면) 또는 지수 백오프
        (1s -> 2s -> 4s -> 8s -> 16s)로 최대 RATE_LIMIT_MAX_RETRIES회 재시도한다.
        429가 아닌 응답은 그대로(재시도 없이) 반환한다 - 상태코드 처리는 호출부 책임."""
        attempt = 0
        while True:
            response = self._http().request(method, path, **kwargs)
            if response.status_code != 429:
                return response
            attempt += 1
            if attempt > RATE_LIMIT_MAX_RETRIES:
                return response
            retry_after = response.headers.get("Retry-After")
            if retry_after is not None:
                try:
                    wait_seconds = float(retry_after)
                except ValueError:
                    wait_seconds = RATE_LIMIT_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            else:
                wait_seconds = RATE_LIMIT_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "네이버 커머스 API 429(Rate Limit) - %s초 대기 후 재시도 (%d/%d): %s",
                wait_seconds,
                attempt,
                RATE_LIMIT_MAX_RETRIES,
                path,
            )
            time_module.sleep(wait_seconds)

    @staticmethod
    def _sign(client_id: str, client_secret: str, timestamp_ms: str) -> str:
        """네이버 커머스 API의 client_credentials 서명 규칙: bcrypt(client_id_timestamp, salt=client_secret)."""
        password = f"{client_id}_{timestamp_ms}".encode("utf-8")
        hashed = bcrypt.hashpw(password, client_secret.encode("utf-8"))
        return base64.urlsafe_b64encode(hashed).decode("utf-8")

    def _fetch_access_token(self, client_id: str, client_secret: str) -> str:
        timestamp_ms = str(int(time_module.time() * 1000))
        signature = self._sign(client_id, client_secret, timestamp_ms)
        response = self._request_with_retry(
            "POST",
            TOKEN_PATH,
            data={
                "client_id": client_id,
                "timestamp": timestamp_ms,
                "client_secret_sign": signature,
                "grant_type": "client_credentials",
                "type": "SELF",
            },
        )
        if response.status_code != 200:
            raise RuntimeError(f"네이버 커머스 API 토큰 발급 실패: HTTP {response.status_code} {response.text[:200]}")
        access_token = response.json().get("access_token")
        if not access_token:
            raise RuntimeError("네이버 커머스 API 토큰 응답에 access_token이 없습니다.")
        return access_token

    @staticmethod
    def _to_naver_datetime(d: date) -> str:
        """네이버 커머스 API가 요구하는 전체 ISO-8601(밀리초+KST 오프셋) 포맷으로 변환한다.

        예: date(2026, 7, 1) -> "2026-07-01T00:00:00.000+09:00"
        """
        dt = datetime.combine(d, datetime.min.time(), tzinfo=_KST)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000") + "+09:00"

    def _fetch_raw_orders_live(
        self, start_date: date, end_date: date, client_id: str, client_secret: str
    ) -> list[dict[str, Any]]:
        """실운영 환경에서 확인된 제약: from/to는 최대 24시간 차이만 허용한다("최대 24시간
        차이로 설정해야 합니다" 오류로 확인됨). 여러 날짜에 걸친 조회는 하루(24시간) 단위로
        나눠 호출한 뒤 결과를 합친다."""
        access_token = self._fetch_access_token(client_id, client_secret)
        all_payloads = []
        raw_orders: list[dict[str, Any]] = []

        day = start_date
        while day < end_date:
            next_day = day + timedelta(days=1)
            response = self._request_with_retry(
                "GET",
                ORDER_LIST_PATH,
                headers={"Authorization": f"Bearer {access_token}"},
                params={"from": self._to_naver_datetime(day), "to": self._to_naver_datetime(next_day)},
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f"네이버 커머스 API 주문 조회 실패: HTTP {response.status_code} {response.text[:200]}"
                )
            payload = response.json()
            all_payloads.append(payload)
            raw_orders.extend(payload.get("data", {}).get("contents", []))
            day = next_day

        logger.info("===== NAVER ORDER RESPONSE =====\n%s", json.dumps(all_payloads, ensure_ascii=False, indent=2))
        try:
            with open("/app/logs/naver_order_response.json", "w", encoding="utf-8") as f:
                json.dump(all_payloads, f, ensure_ascii=False, indent=2)
        except OSError:
            logger.exception("네이버 주문 응답을 /app/logs/naver_order_response.json에 저장하지 못했습니다.")

        return raw_orders

    def _fetch_raw_orders_dummy(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        """네이버 스마트스토어 API 원본 응답 형식(더미)을 흉내 낸다."""
        raw_orders = []
        for seed in self._dummy_seed_orders(start_date, end_date):
            raw_orders.append(
                {
                    "productOrderId": f"N{seed['order_date']:%Y%m%d}{seed['seq']:04d}",
                    "orderDate": seed["order_date"].isoformat(),
                    "productOrderStatus": _STATUS_TO_RAW[seed["status"]],
                    "ordererId": seed["customer_key"],
                    "ordererName": seed["customer_name"],
                    "ordererTel": seed["customer_phone"],
                    "productOptionCode": seed["product_code"],
                    "quantity": seed["quantity"],
                    "unitPrice": seed["unit_price"],
                }
            )
        return raw_orders

    @staticmethod
    def _normalize_dummy(raw: dict[str, Any]) -> dict[str, Any]:
        total_amount = round(raw["unitPrice"] * raw["quantity"], 2)
        return {
            "platform_order_no": raw["productOrderId"],
            "order_date": datetime.fromisoformat(raw["orderDate"]),
            "status": _RAW_TO_STATUS[raw["productOrderStatus"]],
            "customer_key": raw["ordererId"],
            "customer_name": raw["ordererName"],
            "customer_phone": raw["ordererTel"],
            "total_amount": total_amount,
            "discount_amount": 0.0,
            "items": [
                {
                    "platform_option_id": raw["productOptionCode"],
                    "quantity": raw["quantity"],
                    "unit_price": raw["unitPrice"],
                }
            ],
        }

    @staticmethod
    def _normalize_live_orders(contents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """실 API 응답(data.contents, 라인아이템 단위)을 content.order.orderId로 그룹핑해
        ERP의 Order(1) : OrderItem(N) 구조로 정규화한다.

        상태(status)는 주문(orderId) 단위가 아니라 라인아이템(productOrder) 단위로
        내려온다(예: 한 주문의 일부 상품만 반품된 경우). ERP의 Order.status는
        단일 값이라 이 경우 첫 번째 라인아이템의 상태를 대표값으로 사용한다 - 라인아이템별
        상태가 갈리는 주문은 이 단순화로 인해 부정확할 수 있다는 점을 알려드린다(향후
        라인아이템 단위 상태 추적이 필요해지면 OrderItem에 상태 컬럼을 추가해야 한다).
        """
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        order_meta: dict[str, dict[str, Any]] = {}
        for content in contents:
            order = content.get("content", {}).get("order", {})
            order_id = order.get("orderId")
            if order_id is None:
                continue
            grouped[order_id].append(content.get("content", {}).get("productOrder", {}))
            order_meta.setdefault(order_id, order)

        normalized = []
        for order_id, product_orders in grouped.items():
            order = order_meta[order_id]
            first_status = product_orders[0].get("productOrderStatus") or ""
            std_status = _LIVE_STATUS_TO_STD.get(first_status)
            if std_status is None:
                logger.warning(
                    "알 수 없는 네이버 productOrderStatus: %s (orderId=%s) - NEW로 처리", first_status, order_id
                )
                std_status = "NEW"
            normalized.append(
                {
                    "platform_order_no": order_id,
                    "order_date": datetime.fromisoformat(order["orderDate"]),
                    "status": std_status,
                    "customer_key": order.get("ordererNo") or order.get("ordererId"),
                    "customer_name": order.get("ordererName"),
                    "customer_phone": order.get("ordererTel"),
                    "total_amount": float(order.get("generalPaymentAmount", 0)),
                    "discount_amount": float(order.get("orderDiscountAmount", 0)),
                    "items": [
                        {
                            # 옵션(채널상품) 단위 식별자 - product_platform_map.platform_option_id와
                            # 매칭 키가 일치해야 한다.
                            "platform_option_id": po.get("itemNo"),
                            "quantity": po.get("quantity"),
                            "unit_price": float(po.get("unitPrice", 0)),
                            # 매핑이 없는 주문상품을 기존 상품에 자동매칭할 때 쓰이는 참고 정보
                            # (services.product_sync_service.ProductSyncService.match_unmapped_item
                            # 참고) - 주문 API에서는 더 이상 이 정보로 새 상품을 만들지 않는다.
                            # productId(상품번호)는 itemNo(옵션번호)의 상위 개념 - 여러 옵션이
                            # 같은 productId를 공유할 수 있다(비유니크, 자동매칭 2순위로만 사용).
                            "platform_product_id": po.get("productId"),
                            "product_name": po.get("productName"),
                            "option_name": po.get("productOption"),
                            "seller_product_code": po.get("sellerProductCode"),
                            # 네이버 주문(product-orders) API 응답에는 카테고리/브랜드/제조사/
                            # 이미지 정보가 포함되지 않는다(실제 응답으로 확인됨) - 별도의 네이버
                            # 상품상세 API 연동 전까지는 아래 값들이 항상 없다(None).
                            "category": None,
                            "brand": None,
                            "manufacturer": None,
                        }
                        for po in product_orders
                    ],
                }
            )
        return normalized

    def _fetch_raw_products_live(self, client_id: str, client_secret: str) -> list[dict[str, Any]]:
        """상품 검색 API를 페이지 단위로 끝까지 순회해 원본상품 목록을 모은다.

        페이지네이션 방식(page/size)과 응답 필드명은 네이버 커머스 API 공식 문서
        기준으로 작성했으나, 주문 API 때와 달리 아직 실제 계정으로 검증되지
        않았다 - 실 연동 후 오류가 나면 이 메서드와 _normalize_live_products만
        고치면 된다.
        """
        access_token = self._fetch_access_token(client_id, client_secret)
        all_contents: list[dict[str, Any]] = []
        page = 1
        while True:
            response = self._request_with_retry(
                "POST",
                PRODUCT_SEARCH_PATH,
                headers={"Authorization": f"Bearer {access_token}"},
                json={"page": page, "size": PRODUCT_PAGE_SIZE},
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f"네이버 커머스 API 상품 조회 실패: HTTP {response.status_code} {response.text[:200]}"
                )
            payload = response.json()
            contents = payload.get("contents", [])
            all_contents.extend(contents)
            if len(contents) < PRODUCT_PAGE_SIZE:
                break
            page += 1
        return all_contents

    @staticmethod
    def _normalize_live_products(contents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """실 운영 환경에서 실제로 확인한 상품 검색 API 응답 스키마(2026-07-07 확인):

            {"groupProductNo", "originProductNo",
             "channelProducts": [{"channelProductNo", "categoryId", "name",
                 "sellerManagementCode", "statusType"("SALE"=판매중),
                 "channelProductDisplayStatusType"("ON"=노출중), "brandName",
                 "manufacturerName", "representativeImage": {"url"}, ...}]}

        당초 예상했던 detailAttribute.optionInfo.optionCombinations(색상/사이즈
        옵션조합) 구조는 이 API(상품 검색)에는 없다 - 색상/사이즈가 다른 변형은
        이미 서로 다른 originProductNo/channelProductNo를 가진 별도 항목으로
        내려오며, groupProductNo가 그 변형들을 묶는 실제 그룹 키임을 실제 응답으로
        확인했다(예: 같은 상품의 "아이보리"/"블루" 색상 옵션이 동일한
        groupProductNo를 공유). 그래서 groupProductNo로 그룹핑해 하나의 ERP
        상품 아래 여러 옵션(색상별 항목)으로 등록한다.

        ⚠️ 미검증: 이 API의 channelProductNo가 주문 API(product-orders)의
        itemNo와 실제로 같은 값 체계인지는 아직 실제 주문 건과 교차 확인하지
        못했다(레이트 리밋으로 추가 검증 보류) - 우선 channelProductNo를
        옵션번호(platform_option_id)로 채택했지만, 상품 동기화 후 실제
        주문 수집에서 매핑이 잘 안 맞으면(자동매칭이 계속 실패하면) 이 부분을
        가장 먼저 의심해야 한다. 이 API는 옵션명을 별도로 안 주므로(색상 등은
        상품명에 포함되어 내려온다) option_name은 채널상품명을 그대로 사용한다.
        """
        grouped: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        for content in contents:
            group_no = content.get("groupProductNo") or content.get("originProductNo")
            if group_no is None:
                continue
            grouped[group_no].append(content)

        normalized = []
        for group_no, group_contents in grouped.items():
            first_channel_product = (group_contents[0].get("channelProducts") or [{}])[0]
            representative_url = (first_channel_product.get("representativeImage") or {}).get("url")

            items = []
            for content in group_contents:
                channel_product = (content.get("channelProducts") or [{}])[0]
                channel_product_no = channel_product.get("channelProductNo")
                items.append(
                    {
                        # 옵션(채널상품) 단위 식별자 - product_platform_map.platform_option_id의
                        # 매핑 키(유니크)가 된다.
                        "platform_option_id": str(channel_product_no) if channel_product_no is not None else None,
                        "platform_product_id": str(group_no),
                        "option_name": channel_product.get("name"),
                        "seller_product_code": channel_product.get("sellerManagementCode"),
                        "sale_price": channel_product.get("salePrice"),
                        "is_selling": (
                            channel_product.get("statusType") == "SALE"
                            and channel_product.get("channelProductDisplayStatusType") == "ON"
                        ),
                        "option_image_url": None,
                    }
                )

            normalized.append(
                {
                    "product_name": first_channel_product.get("name"),
                    "category": first_channel_product.get("categoryId"),
                    "brand": first_channel_product.get("brandName"),
                    "manufacturer": first_channel_product.get("manufacturerName"),
                    "images": {"representative_url": representative_url, "optional_urls": []},
                    "items": items,
                }
            )
        return normalized

    def _dummy_products(self) -> list[dict[str, Any]]:
        """개발/테스트 기본값(실 API 키 없음) - 결정적인 더미 상품 1개를 반환한다."""
        return [
            {
                "product_name": "네이버 더미 상품",
                "category": None,
                "brand": None,
                "manufacturer": None,
                "images": {"representative_url": None, "optional_urls": []},
                "items": [
                    {
                        "platform_option_id": "N-DUMMY-ITEM-001",
                        "platform_product_id": "N-DUMMY-PRODUCT-001",
                        "option_name": "기본",
                        "seller_product_code": None,
                        "sale_price": 9900,
                        "is_selling": True,
                        "option_image_url": None,
                    }
                ],
            }
        ]
