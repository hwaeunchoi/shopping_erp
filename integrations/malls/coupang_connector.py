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
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from integrations.malls.base_mall_connector import (
    SALE_STATUS_ON_SALE,
    SALE_STATUS_SUSPENDED,
    BaseMallConnector,
    CategoryRequirement,
    CategoryRequirements,
    ProductCreateResult,
    ProductOptionItemResult,
    ProductOptionRegistrationStatus,
    ProductOptionsCreateResult,
    ProductSyncActionResult,
    ShipmentSubmitResult,
)
from integrations.malls.errors import (
    MarketplaceCapabilityUnsupportedError,
    MarketplaceCredentialMissingError,
    MarketplaceExternalAPIError,
    MarketplaceValidationError,
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
# 송장업로드 처리 API(공식 문서: developers.coupang.com/ko/api/shipments/uploading-waybills,
# 2026-09 조회) - 요청 바디 최상위 키는 vendorId + orderSheetInvoiceApplyDtos(배열).
INVOICE_PATH_TMPL = "/v2/providers/openapi/apis/api/v4/vendors/{vendor_id}/orders/invoices"

# HTTP 429(Rate Limit) 재시도 정책: 1s -> 2s -> 4s -> 8s -> 16s 지수 백오프.
RATE_LIMIT_MAX_RETRIES = 5
RATE_LIMIT_BACKOFF_BASE_SECONDS = 1.0

# --- 반품/취소 요청 목록 조회 (공식 문서: developers.coupang.com/ko/api/returns/
# return-cancellation-request-list-query, 2026-09 조회) ---
RETURN_REQUEST_PATH_TMPL = "/v2/providers/openapi/apis/api/v6/vendors/{vendor_id}/returnRequests"
# status 쿼리 파라미터 코드(공식 문서 파라미터 표 원문): RU(출고중지요청)/UC(반품접수)/
# CC(반품완료)/PR(쿠팡확인요청). status를 생략하면 orderId가 필수가 되고, cancelType=
# CANCEL은 status 자체를 받을 수 없어(제거해야 함) 결과적으로 orderId 없이는(기간만
# 으로는) 취소 목록을 조회할 공식 경로가 없다 - 그래서 취소(CANCEL)는 미지원으로 남기고
# (supports_cancellation_sync는 기본값 False를 그대로 상속), 반품(RETURN, cancelType
# 기본값)만 상태코드별로 순회해 기간 기반 대량 수집을 구현한다(주문 조회와 동일 이유로
# 동일한 패턴 - 상태가 필수이며 한 번에 한 상태만 준다).
RETURN_STATUS_CODES = ["RU", "UC", "CC", "PR"]
RETURN_MAX_PER_PAGE = 50
RETURN_MAX_RANGE_DAYS = 31

# --- 콜센터 문의(CS) 조회 (공식 문서: developers.coupang.com/hc/en-us/articles/
# 360033645354-Query-of-Coupang-Contact-Center-Inquiries, 2026-09 조회) ---
# 상품별 문의(onlineInquiries)도 조회 계약 자체는 같은 문서군에서 확인되지만, 이번
# 단계는 콜센터 문의만 구현한다(범위 관리 - docs/COMMERCIAL_ERP_ROADMAP.md 5-B단계
# 절 참고). 답변(쓰기) API(POST .../replies)의 요청 바디 필드(vendorId/inquiryId/
# content/replyBy/parentAnswerId)는 존재가 확인되지만, parentAnswerId가 "신규
# 답변(transfer 아님)" 케이스에서 어떤 값이어야 하는지는 문서에서 확정할 수 없어
# 이번 단계에서 답변 전송은 구현하지 않는다(fetch만, 조회 전용).
CALL_CENTER_INQUIRY_PATH_TMPL = "/v2/providers/openapi/apis/api/v5/vendors/{vendor_id}/callCenterInquiries"
# partnerCounselingStatus는 필수 파라미터이며 한 번에 한 상태만 준다(공식 문서
# 파라미터 표: NONE/ANSWER/NO_ANSWER/TRANSFER) - 그래서 4종을 순회해 합친다.
CALL_CENTER_INQUIRY_STATUSES = ["NONE", "ANSWER", "NO_ANSWER", "TRANSFER"]
CALL_CENTER_INQUIRY_MAX_PER_PAGE = 30
CALL_CENTER_INQUIRY_MAX_RANGE_DAYS = 7

# 응답 필드 receiptStatus(응답 예시로 확인, 파라미터 표의 코드와는 다른 표기) -> 내부
# 정규화 상태. 실 응답 예시에서 확인된 값만 매핑하고 나머지는 REVIEW로 보존한다(완료로
# 추정 금지) - services.claim_state_machine 참고.
_RETURN_RAW_TO_STATUS = {
    "RELEASE_STOP_UNCHECKED": "REQUESTED",  # 출고중지요청
    "RETURNS_UNCHECKED": "REQUESTED",  # 반품접수
    "VENDOR_WAREHOUSE_CONFIRM": "RECEIVED",  # 입고완료
    "REQUEST_COUPANG_CHECK": "APPROVED",  # 쿠팡확인요청
    "RETURNS_COMPLETED": "REFUNDED",  # 반품완료
}

# --- 취소 "후보 주문" 단건 조회 (상용 ERP 확장 2단계-A 보완) ---
# 공식 문서(developers.coupang.com/ko/api/returns/return-cancellation-request-list-query,
# 2026-09 재조회) 파라미터 표 원문: "orderId: 주문번호 / status 파라메터를 제외하고
# 조회할 경우에는 orderId가 파라메터에 포함되어야 합니다." + "cancelType=CANCEL일
# 경우 status는 지원하지 않는 파라메터입니다." 즉 cancelType=CANCEL 조회는 status를
# 쓸 수 없어 orderId가 항상 필수가 된다 - 그래서 날짜range만으로의 대량(bulk) 취소
# 수집은 여전히 불가능하지만(위 RETURN_STATUS_CODES 주석과 동일 결론), ERP가 이미
# 알고 있는 주문 하나를 대상으로 "orderId+cancelType=CANCEL+날짜range"를 조합하는
# 조회는 파라미터 표가 명시적으로 허용한다(같은 표의 orderId 행: "searchType=
# timeFrame일 경우 지원하지 않는 파라메터" - 즉 searchType을 쓰지 않는 이 모드에서는
# orderId 사용이 정상 경로). 이 정합성은 공식 문서 파라미터 표 자체에서 확인했다
# (참고: 같은 채널의 별도 FAQ "결제완료 단계에서 취소된 주문 정보를 확인할 수
# 있나요?"는 "status, orderId 파라메터를 모두 제외"라고 안내해 파라미터 표와
# 서로 모순되는데, 이 모순은 "orderId 없이 날짜range만으로 대량 조회가 되는지"에만
# 관련되고, 이 기능이 실제로 구현하는 "orderId를 포함해 조회"하는 경로 자체는 두
# 문서 어디서도 부정하지 않는다 - 그래서 이 좁은 범위만 구현한다).
CANCEL_LOOKUP_WINDOW_DAYS = 31  # RETURN_MAX_RANGE_DAYS와 동일한 문서상 최대 조회기간.
_CANCEL_RAW_TO_STATUS = {
    # Cancellation 모델은 REQUESTED->COMPLETED 두 상태만 허용한다(REVIEW 제외) -
    # RETURN_RAW_TO_STATUS의 5단계 세분 상태를 이 두 상태로 보수적으로 접는다:
    # 아직 최종 처리 전(출고중지요청/반품접수/쿠팡확인요청)은 REQUESTED,
    # 최종 처리 확인(입고완료/반품완료)은 COMPLETED로 본다.
    "RELEASE_STOP_UNCHECKED": "REQUESTED",
    "RETURNS_UNCHECKED": "REQUESTED",
    "REQUEST_COUPANG_CHECK": "REQUESTED",
    "VENDOR_WAREHOUSE_CONFIRM": "COMPLETED",
    "RETURNS_COMPLETED": "COMPLETED",
}

# --- 교환 요청 목록 조회 (공식 문서: developers.coupang.com/ko/api/exchanges/
# query-a-list-of-exchange-requests, 2026-09 조회) ---
EXCHANGE_REQUEST_PATH_TMPL = "/v2/providers/openapi/apis/api/v4/vendors/{vendor_id}/exchangeRequests"
# 조회기간 최대 7일(공식 문서 확인) - 반품/주문의 31일 제약보다 짧다.
EXCHANGE_MAX_RANGE_DAYS = 7

_EXCHANGE_RAW_TO_STATUS = {
    "RECEIPT": "REQUESTED",
    "PROGRESS": "APPROVED",
    "SUCCESS": "COMPLETED",
    "REJECT": "REJECTED",
    "CANCEL": "REJECTED",  # 교환 철회 - 내부에는 별도 "철회" 상태가 없어 REJECTED로 취급.
}

# --- 정산 (공식 문서: developers.coupang.com/ko/api/settlement/settlement-detail-query
# 및 .../sales-detail-query, 2026-09 조회) ---
# 정산 회차 요약(월 단위 조회) - settlement_sync_job이 Settlement 행을 만드는 데 쓴다.
SETTLEMENT_HISTORIES_PATH = "/v2/providers/marketplace_openapi/apis/api/v1/settlement-histories"
# 주문 단위 매출/정산 상세 - SettlementDetail 행을 만드는 데 쓴다(정산 회차와는
# 별도 API라 서로 다른 메서드/capability로 분리했다: fetch_settlements vs
# fetch_settlement_details).
REVENUE_HISTORY_PATH = "/v2/providers/openapi/apis/api/v1/revenue-history"
REVENUE_HISTORY_MAX_RANGE_DAYS = 31

_SETTLEMENT_STATUS_TO_STD = {"DONE": "COMPLETED", "SUBJECT": "SCHEDULED"}

# --- 상품 아이템별 재고/판매상태 변경 (상용 ERP 확장 3단계, 첫 묶음) ---
# 공식 문서(developers.coupang.com/hc/ko/articles/360034156253, .../360034156313,
# .../360033645154, 2026-09 조회) - 셋 다 요청 바디 없이 경로 파라미터만으로 동작하고
# 응답은 동일한 {code: "SUCCESS"|"ERROR", message} 형식이다. 세 API 모두 "판매요청
# 승인 완료 후 vendorItemId가 발급된 상태"가 전제조건이다(문서 원문).
QUANTITY_PATH_TMPL = (
    "/v2/providers/seller_api/apis/api/v1/marketplace/vendor-items/{vendor_item_id}/quantities/{quantity}"
)
SALES_STOP_PATH_TMPL = "/v2/providers/seller_api/apis/api/v1/marketplace/vendor-items/{vendor_item_id}/sales/stop"
SALES_RESUME_PATH_TMPL = "/v2/providers/seller_api/apis/api/v1/marketplace/vendor-items/{vendor_item_id}/sales/resume"

# --- 신규 상품 등록 (상용 ERP 확장 3단계, 두 번째 묶음) ---
# 공식 문서(developers.coupang.com/hc/en-us/articles/360033877853-Product-Creation,
# 2026-09 조회) 확인 사항:
#   POST /v2/providers/seller_api/apis/api/v1/marketplace/seller-products
#   최상위 필수: displayCategoryCode, sellerProductName, vendorId, saleStartedAt,
#   saleEndedAt, deliveryMethod, deliveryCompanyCode, deliveryChargeType,
#   deliveryCharge, freeShipOverAmount, deliveryChargeOnReturn, remoteAreaDeliverable,
#   unionDeliveryType, returnCenterCode, returnChargeName, companyContactNumber,
#   returnZipCode, returnAddress, returnAddressDetail, returnCharge,
#   outboundShippingPlaceCode, vendorUserId, requested(bool), images(배열, 최소
#   REPRESENTATION 1장), items(배열, 최소 1개).
#   items[] 필수: itemName, originalPrice, salePrice, maximumBuyCount,
#   maximumBuyForPerson, maximumBuyForPersonPeriod, outboundShippingTimeDay,
#   unitCount, adultOnly, taxType, parallelImported, overseasPurchased, pccNeeded.
#   images[] 필수: imageOrder, imageType(REPRESENTATION 최소 1장), cdnPath 또는
#   vendorPath 중 하나(http://로 시작하면 쿠팡 CDN으로 자동 다운로드된다).
#   카테고리별 필수 속성/상품정보제공고시/인증정보는 정적으로 고정돼 있지 않고
#   카테고리마다 달라, 등록 전 "카테고리 메타정보 조회" API로 실제 조회해야 한다
#   (fetch_category_requirements 참고) - 추측/하드코딩하지 않는다.
#   응답: {"code","message","data":{"code":"SUCCESS"|"ERROR","message","data":
#   <sellerProductId:Long>}} (이중 래핑 - 공식 문서 응답 예시 원문 확인). 중요:
#   이 응답은 sellerProductId(상품 단위)만 돌려주고, ProductPlatformMap에 필요한
#   옵션 단위 식별자(vendorItemId)는 승인 완료 후 별도 조회(Querying product API)로만
#   확인할 수 있다 - 그래서 이 커넥터는 등록 성공 시 channel_option_id를 채우지
#   않는다(추측 금지, services.product_publish_service 참고).
CREATE_PRODUCT_PATH = "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products"
# 카테고리 메타정보 조회(공식 문서: developers.coupangcorp.com/hc/en-us/articles/
# 360034035713-Category-Metadata-Query, 2026-09 조회) - attributes[]/
# noticeCategories[].noticeCategoryDetailNames[]/certifications[] 각각의 required
# 필드가 "MANDATORY"/"OPTIONAL"(그 외 문서상 별도 값도 있음, "MANDATORY"만 필수로
# 취급)로 내려온다.
CATEGORY_METADATA_PATH_TMPL = (
    "/v2/providers/seller_api/apis/api/v1/marketplace/meta/category-related-metas/display-category-codes/{code}"
)
# 상품 조회(Querying product, 공식 문서 확인 - 등록/수정과 동일한 리소스 경로에 GET) -
# 승인 완료 후 이 조회로만 vendorItemId(옵션 단위 식별자)가 확정된다.
PRODUCT_QUERY_PATH_TMPL = "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{seller_product_id}"

#  sellerProductName은 draft.name, vendorId는 자격증명에서 채워지므로 이 목록에
# 넣지 않는다(operator가 channel_fields에 직접 입력하는 항목만 나열).
_COUPANG_TOP_LEVEL_REQUIRED_FIELDS = (
    "saleStartedAt",
    "saleEndedAt",
    "deliveryMethod",
    "deliveryCompanyCode",
    "deliveryChargeType",
    "deliveryCharge",
    "freeShipOverAmount",
    "deliveryChargeOnReturn",
    "remoteAreaDeliverable",
    "unionDeliveryType",
    "returnCenterCode",
    "returnChargeName",
    "companyContactNumber",
    "returnZipCode",
    "returnAddress",
    "returnAddressDetail",
    "returnCharge",
    "outboundShippingPlaceCode",
    "vendorUserId",
)
#  salePrice/maximumBuyCount는 draft.sale_price/stock_quantity에서 채워지므로
# (create_product에서 덮어쓴다) 이 목록에 넣지 않는다 - _validate_coupang_publish_draft
# 가 그 두 필드는 draft 레벨에서 별도로 검사한다.
_COUPANG_ITEM_REQUIRED_FIELDS = (
    "itemName",
    "originalPrice",
    "maximumBuyForPerson",
    "maximumBuyForPersonPeriod",
    "outboundShippingTimeDay",
    "unitCount",
    "adultOnly",
    "taxType",
    "parallelImported",
    "overseasPurchased",
)


def _validate_coupang_publish_draft(draft: dict[str, Any]) -> list[str]:
    """공식 계약상 확인된 필수 항목이 비어 있는지 검사해 누락 목록을 반환한다(값
    자체는 검증하지 않는다 - 카테고리별 필수 속성은 호출부가 별도로 검사한다)."""
    missing: list[str] = []
    if not draft.get("name"):
        missing.append("name(sellerProductName)")
    if draft.get("sale_price") is None:
        missing.append("sale_price")
    image_urls = draft.get("image_urls") or []
    if not image_urls:
        missing.append("image_urls(대표이미지 최소 1장 필요)")

    cf = draft.get("channel_fields") or {}
    for field in _COUPANG_TOP_LEVEL_REQUIRED_FIELDS:
        if cf.get(field) in (None, ""):
            missing.append(f"channel_fields.{field}")
    if cf.get("requested") is None:
        missing.append("channel_fields.requested")

    item = cf.get("item") or {}
    for field in _COUPANG_ITEM_REQUIRED_FIELDS:
        if item.get(field) in (None, ""):
            missing.append(f"channel_fields.item.{field}")
    if draft.get("stock_quantity") is None:
        missing.append("stock_quantity(maximumBuyCount)")
    return missing


# --- 옵션 조합 상품 등록 (상용 ERP 확장 3단계, 세 번째 묶음) ---
# 공식 문서(Product Creation, developers.coupang.com/hc/en-us/articles/360033877853,
# 2026-09 조회): items[] 하나하나가 곧 SKU다 - "판매가"(items[].salePrice)는 그
# SKU의 절대 판매가다(네이버 옵션가처럼 기준가에 대한 추가금이 아니다 - models.
# product.ProductPublishOptionGroupDraft 모듈 docstring 참고). "Can add up to
# max 200 options"(상품당 옵션 최대 200개). itemName은 "Input for each item so
# that there is no overlap"(항목마다 겹치지 않게 입력) - 옵션값 조합을 그대로
# 이어붙여 자동 생성한다(운영자가 별도 문구를 입력할 필요가 없고, 조합 자체가
# 유니크하므로 겹치지 않는다).
COUPANG_MAX_OPTION_ITEMS = 200
_COUPANG_ITEM_NAME_MAX_LENGTH = 150
# 그룹(옵션조합) 등록의 item 템플릿 필드 - 단일 등록과 달리 itemName/salePrice/
# maximumBuyCount는 SKU마다 다르므로 공통 템플릿(channel_fields.item)에서 검사하지
# 않는다(각 품목에서 채운다).
_COUPANG_GROUP_ITEM_TEMPLATE_REQUIRED_FIELDS = tuple(f for f in _COUPANG_ITEM_REQUIRED_FIELDS if f != "itemName")


def _validate_coupang_option_group_draft(draft: dict[str, Any]) -> list[str]:
    """공식 계약상 확인된, 네트워크 호출 없이 검사 가능한 필수 항목만 검사한다
    (카테고리별 필수 속성은 호출부가 fetch_category_requirements로 조회해 별도
    검사한다 - create_product와 동일 원칙). 품목 간 중복(조합/판매자코드)은 여기서
    즉시 차단한다."""
    missing: list[str] = []
    if not draft.get("name"):
        missing.append("name(sellerProductName)")
    image_urls = draft.get("image_urls") or []
    if not image_urls:
        missing.append("image_urls(대표이미지 최소 1장 필요)")

    cf = draft.get("channel_fields") or {}
    for field in _COUPANG_TOP_LEVEL_REQUIRED_FIELDS:
        if cf.get(field) in (None, ""):
            missing.append(f"channel_fields.{field}")
    if cf.get("requested") is None:
        missing.append("channel_fields.requested")

    item_template = cf.get("item") or {}
    for field in _COUPANG_GROUP_ITEM_TEMPLATE_REQUIRED_FIELDS:
        if item_template.get(field) in (None, ""):
            missing.append(f"channel_fields.item.{field}")

    items = draft.get("items") or []
    if not items:
        missing.append("items(등록할 SKU가 1개 이상 필요)")
        return missing
    if len(items) > COUPANG_MAX_OPTION_ITEMS:
        missing.append(f"items(쿠팡 상품당 옵션 최대 {COUPANG_MAX_OPTION_ITEMS}개, 현재 {len(items)}개)")

    seen_codes: set[str] = set()
    seen_combos: set[tuple[str, ...]] = set()
    for item in items:
        option_values = item.get("option_values") or []
        if not option_values:
            missing.append(f"items[product_option_id={item.get('product_option_id')}].option_values")
        combo_key = tuple(str(v) for _a, v in option_values)
        if combo_key in seen_combos:
            raise MarketplaceValidationError(f"중복된 옵션 조합입니다(추측 금지): {combo_key}")
        seen_combos.add(combo_key)

        code = item.get("seller_product_code")
        if not code:
            missing.append(f"items[product_option_id={item.get('product_option_id')}].seller_product_code")
        elif code in seen_codes:
            raise MarketplaceValidationError(f"같은 초안 안에 판매자 관리코드가 중복됩니다: {code}")
        else:
            seen_codes.add(code)
        if item.get("sale_price") is None:
            missing.append(f"items[product_option_id={item.get('product_option_id')}].sale_price")
        if item.get("stock_quantity") is None:
            missing.append(f"items[product_option_id={item.get('product_option_id')}].stock_quantity")
    return missing


class CoupangConnector(BaseMallConnector):
    platform_code = "coupang"
    supports_shipment_submit = True
    supports_return_sync = True
    supports_exchange_sync = True
    supports_settlement_sync = True
    supports_settlement_detail_sync = True
    # 취소(CANCEL)는 기간만으로 대량 조회할 공식 API 경로가 없다(RETURN_STATUS_CODES
    # 주석 참고) - supports_cancellation_sync는 base 기본값(False)을 그대로 상속한다.
    # 대신 "후보 주문 단건 조회"는 지원한다(CANCEL_LOOKUP_WINDOW_DAYS 주석 참고).
    supports_cancellation_lookup_by_order = True
    supports_inventory_update = True
    supports_sale_status_update = True
    # 등록 접수 + 카테고리 메타 기반 필수값 차단 + 심사상태 조회까지만 지원한다 -
    # 등록 응답이 옵션 단위 식별자(vendorItemId)를 돌려주지 않아(모듈 상단
    # CREATE_PRODUCT_PATH 주석 참고) ProductPlatformMap 자동 생성과 그에 따른
    # 정보수정(update_product_info)은 이번 라운드 범위 밖이다(다음 단계로 이월) -
    # supports_product_info_update는 base 기본값(False)을 그대로 상속한다.
    supports_product_create = True
    supports_product_option_create = True
    supports_inquiry_sync = True

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
        # 레거시 인터페이스 - 신규 코드는 submit_shipment()를 사용한다(base 클래스 참고).
        raise MarketplaceCapabilityUnsupportedError("coupang", "shipment_update")

    def submit_shipment(
        self,
        platform_order_item_no: str,
        carrier_code: str,
        tracking_no: str,
        dispatch_date: date,
        platform_order_no: Optional[str] = None,
        platform_shipment_box_id: Optional[str] = None,
    ) -> ShipmentSubmitResult:
        """송장업로드 처리(공식 문서: developers.coupang.com/ko/api/shipments/
        uploading-waybills, 2026-09 조회). 쿠팡은 shipmentBoxId+orderId+vendorItemId
        세 값이 모두 필요하다 - platform_order_no(orderId)/platform_shipment_box_id가
        없으면(과거 데이터 등) 안전하게 CapabilityUnsupported로 거부한다(추측 금지)."""
        if platform_order_no is None or platform_shipment_box_id is None:
            raise MarketplaceCapabilityUnsupportedError("coupang", "shipment_submit_missing_box_id")
        credentials = self._get_credentials()
        if credentials is None:
            raise MarketplaceCredentialMissingError("coupang")
        access_key, secret_key, vendor_id = credentials

        path = INVOICE_PATH_TMPL.format(vendor_id=vendor_id)
        url = f"{COUPANG_API_BASE}{path}"
        headers = {
            "Authorization": self._authorization(access_key, secret_key, "POST", path, ""),
            "Content-Type": "application/json;charset=UTF-8",
        }
        body = {
            "vendorId": vendor_id,
            "orderSheetInvoiceApplyDtos": [
                {
                    "shipmentBoxId": int(platform_shipment_box_id),
                    "orderId": int(platform_order_no),
                    "vendorItemId": int(platform_order_item_no),
                    "deliveryCompanyCode": carrier_code,
                    "invoiceNumber": tracking_no,
                    "splitShipping": False,
                    "preSplitShipped": False,
                    "estimatedShippingDate": "",
                }
            ],
        }
        response = self._request_with_retry("POST", url, headers=headers, json_body=body)
        raise_for_status("coupang", response.status_code)
        with external_call("coupang"):
            payload = response.json()
            response_list = payload.get("responseList", []) or payload.get("data", [])
        # 개별 결과가 없으면(스키마 차이) 최상위 responseCode만으로 판단한다 -
        # 실 계정 검증 전까지는 응답 스키마를 100% 확정할 수 없다(모듈 docstring 참고).
        if response_list:
            first = response_list[0]
            succeed = bool(first.get("succeed", False))
            result_code = str(first.get("resultCode", "UNKNOWN"))
            return ShipmentSubmitResult(accepted=succeed, platform_result_code=result_code)
        succeed = payload.get("responseCode") == 0
        return ShipmentSubmitResult(accepted=succeed, platform_result_code=str(payload.get("responseCode")))

    def fetch_settlements(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        """정산 회차 요약 조회(공식 문서: developers.coupang.com/ko/api/settlement/
        settlement-detail-query, 2026-09 조회) - revenueRecognitionYearMonth(YYYY-MM)
        단위로만 조회 가능해, [start_date, end_date]가 걸치는 각 월을 순회해 호출한다."""
        credentials = self._get_credentials()
        if credentials is None:
            raise MarketplaceCredentialMissingError("coupang")
        access_key, secret_key, _vendor_id = credentials
        raw_items = self._fetch_raw_settlement_histories(start_date, end_date, access_key, secret_key)
        return self._normalize_settlement_histories(raw_items)

    def fetch_returns(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        """반품 목록 조회(공식 문서: developers.coupang.com/ko/api/returns/
        return-cancellation-request-list-query, 2026-09 조회) - status 코드별로 순회해
        기간 내 전체를 모은다(모듈 상수 RETURN_STATUS_CODES 참고)."""
        credentials = self._get_credentials()
        if credentials is None:
            raise MarketplaceCredentialMissingError("coupang")
        access_key, secret_key, vendor_id = credentials
        raw_items = self._fetch_raw_return_requests(start_date, end_date, access_key, secret_key, vendor_id)
        return self._normalize_return_requests(raw_items)

    def fetch_cancellation_status(self, platform_order_no: str, since: date) -> Optional[dict[str, Any]]:
        """지정한 주문 하나의 취소 여부를 조회한다(모듈 상단 CANCEL_LOOKUP_WINDOW_DAYS
        주석의 공식 문서 근거 참고) - orderId+cancelType=CANCEL 조합, status 파라미터
        제외. 조회기간은 [since, 오늘] 중 최근 CANCEL_LOOKUP_WINDOW_DAYS일로 제한한다
        (그보다 오래전에 발생한 취소는 이 방식으로 확인할 수 없다는 한계가 있다 -
        services.claim_sync_service의 후보 선정 자체가 배송 전 상태의 주문만 대상으로
        하므로 실무적으로는 이 범위 밖 사례가 드물 것으로 본다)."""
        credentials = self._get_credentials()
        if credentials is None:
            raise MarketplaceCredentialMissingError("coupang")
        access_key, secret_key, vendor_id = credentials
        today = date.today()
        window_start = max(since, today - timedelta(days=CANCEL_LOOKUP_WINDOW_DAYS))
        raw_items = self._fetch_raw_cancel_by_order(
            window_start, today, platform_order_no, access_key, secret_key, vendor_id
        )
        normalized = self._normalize_cancel_requests(raw_items)
        return normalized[0] if normalized else None

    def fetch_inquiries(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        """콜센터 문의(CS) 목록 조회 - 상품별 문의(onlineInquiries)는 이번 단계
        범위 밖(모듈 상단 CALL_CENTER_INQUIRY_PATH_TMPL 주석 참고)."""
        credentials = self._get_credentials()
        if credentials is None:
            raise MarketplaceCredentialMissingError("coupang")
        access_key, secret_key, vendor_id = credentials
        raw_items = self._fetch_raw_call_center_inquiries(start_date, end_date, access_key, secret_key, vendor_id)
        return self._normalize_call_center_inquiries(raw_items)

    def fetch_exchanges(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        """교환 목록 조회(공식 문서: developers.coupang.com/ko/api/exchanges/
        query-a-list-of-exchange-requests, 2026-09 조회) - 조회기간 최대 7일이라
        내부적으로 7일 단위로 나눠 호출한다."""
        credentials = self._get_credentials()
        if credentials is None:
            raise MarketplaceCredentialMissingError("coupang")
        access_key, secret_key, vendor_id = credentials
        raw_items = self._fetch_raw_exchange_requests(start_date, end_date, access_key, secret_key, vendor_id)
        return self._normalize_exchange_requests(raw_items)

    def fetch_settlement_details(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        """매출내역(주문 단위 정산 상세) 조회(공식 문서: developers.coupang.com/ko/api/
        settlement/sales-detail-query, 2026-09 조회) - recognitionDate(매출인식일) 기준
        최대 31일 범위만 허용해 31일 단위로 나눠 token 페이지네이션으로 끝까지 순회한다."""
        credentials = self._get_credentials()
        if credentials is None:
            raise MarketplaceCredentialMissingError("coupang")
        access_key, secret_key, vendor_id = credentials
        raw_items = self._fetch_raw_revenue_history(start_date, end_date, access_key, secret_key, vendor_id)
        return self._normalize_revenue_history(raw_items)

    def fetch_products(self) -> list[dict[str, Any]]:
        # 상품 연동 미구현 - 빈 목록(정상 0건 위장) 대신 미지원 오류를 던진다.
        raise MarketplaceCapabilityUnsupportedError("coupang", "products")

    def update_inventory(
        self, platform_option_id: str, quantity: int, platform_origin_product_id: Optional[str] = None
    ) -> ProductSyncActionResult:
        """상품 아이템별 수량 변경(공식 문서: developers.coupang.com/hc/ko/articles/
        360034156253, 2026-09 조회) - PUT .../vendor-items/{vendorItemId}/quantities/
        {quantity}, 요청 바디 없음. platform_origin_product_id는 쿠팡에서 쓰지 않는다
        (vendorItemId 하나로 충분 - 공식 문서 원문: "Option ID. It is a unique
        identifier for the vendor item.")."""
        credentials = self._get_credentials()
        if credentials is None:
            raise MarketplaceCredentialMissingError("coupang")
        access_key, secret_key, _vendor_id = credentials
        path = QUANTITY_PATH_TMPL.format(vendor_item_id=platform_option_id, quantity=quantity)
        return self._call_product_action(path, access_key, secret_key)

    def update_sale_status(
        self, platform_option_id: str, target_status: str, platform_origin_product_id: Optional[str] = None
    ) -> ProductSyncActionResult:
        """상품 아이템별 판매 재개/중지(공식 문서: developers.coupang.com/hc/ko/articles/
        360033645154(재개)/360034156313(중지), 2026-09 조회) - 둘 다 PUT
        .../vendor-items/{vendorItemId}/sales/{resume|stop}, 요청 바디 없음."""
        credentials = self._get_credentials()
        if credentials is None:
            raise MarketplaceCredentialMissingError("coupang")
        access_key, secret_key, _vendor_id = credentials
        if target_status == SALE_STATUS_ON_SALE:
            path = SALES_RESUME_PATH_TMPL.format(vendor_item_id=platform_option_id)
        elif target_status == SALE_STATUS_SUSPENDED:
            path = SALES_STOP_PATH_TMPL.format(vendor_item_id=platform_option_id)
        else:
            raise MarketplaceValidationError(f"알 수 없는 target_status입니다: {target_status}")
        return self._call_product_action(path, access_key, secret_key)

    def _call_product_action(self, path: str, access_key: str, secret_key: str) -> ProductSyncActionResult:
        """재고/판매상태 변경 API 공통 호출 - 셋 다 요청 바디 없이 PUT하고 {code,
        message} 형식으로 응답한다(공식 문서 Response 확인, 2026-09 조회)."""
        authorization = self._authorization(access_key, secret_key, "PUT", path, "")
        response = self._request_with_retry("PUT", path, headers={"Authorization": authorization})
        raise_for_status("coupang", response.status_code)
        with external_call("coupang"):
            payload = response.json()
            code = payload.get("code")
        return ProductSyncActionResult(accepted=(code == "SUCCESS"), platform_result_code=str(code))

    def fetch_category_requirements(self, category_code: str) -> CategoryRequirements:
        """카테고리 메타정보 조회(모듈 상단 CATEGORY_METADATA_PATH_TMPL 주석의 공식
        문서 근거 참고) - 이 category_code(displayCategoryCode)로 등록 시 채널이
        실제로 요구하는 필수 속성/상품정보제공고시/인증정보 항목을 조회한다."""
        credentials = self._get_credentials()
        if credentials is None:
            raise MarketplaceCredentialMissingError("coupang")
        access_key, secret_key, _vendor_id = credentials
        path = CATEGORY_METADATA_PATH_TMPL.format(code=category_code)
        authorization = self._authorization(access_key, secret_key, "GET", path, "")
        response = self._request_with_retry("GET", path, headers={"Authorization": authorization})
        raise_for_status("coupang", response.status_code)
        with external_call("coupang"):
            payload = response.json()
            data = payload.get("data") or {}
        attributes = [
            CategoryRequirement(name=a["attributeTypeName"], mandatory=a.get("required") == "MANDATORY")
            for a in data.get("attributes", []) or []
            if a.get("attributeTypeName")
        ]
        notices = [
            CategoryRequirement(
                name=detail["noticeCategoryDetailName"], mandatory=detail.get("required") == "MANDATORY"
            )
            for category in data.get("noticeCategories", []) or []
            for detail in category.get("noticeCategoryDetailNames", []) or []
            if detail.get("noticeCategoryDetailName")
        ]
        certifications = [
            CategoryRequirement(name=c["certificationType"], mandatory=c.get("required") == "MANDATORY")
            for c in data.get("certifications", []) or []
            if c.get("certificationType")
        ]
        return CategoryRequirements(attributes=attributes, notices=notices, certifications=certifications)

    def create_product(self, draft_snapshot: dict[str, Any]) -> ProductCreateResult:
        """옵션 조합 없는 단순 신규 상품을 등록 접수한다(모듈 상단 CREATE_PRODUCT_PATH
        주석의 공식 스펙 근거 참고). requested=false로 접수하면(모듈 docstring 참고)
        승인 요청 없이 저장만 된다 - channel_fields.requested를 운영자가 직접
        결정하도록 그대로 전달한다(자동으로 승인 요청하지 않는다는 뜻은 아니다 -
        운영자가 True를 입력하면 그대로 True로 보낸다)."""
        category_code = draft_snapshot.get("category_code")
        if not category_code:
            raise MarketplaceValidationError("category_code(displayCategoryCode)가 비어 있습니다.")
        core_missing = _validate_coupang_publish_draft(draft_snapshot)
        requirements = self.fetch_category_requirements(category_code)
        cf = draft_snapshot.get("channel_fields") or {}
        provided_category_values = cf.get("category_attribute_values") or {}
        category_missing = [
            f"category_attribute_values.{name}"
            for name in requirements.mandatory_names()
            if name not in provided_category_values
        ]
        missing = core_missing + category_missing
        if missing:
            raise MarketplaceValidationError(
                f"쿠팡 상품 등록에 필요한 항목이 비어 있습니다(카테고리={category_code}, 추측 금지): "
                + ", ".join(missing)
            )

        credentials = self._get_credentials()
        if credentials is None:
            raise MarketplaceCredentialMissingError("coupang")
        access_key, secret_key, vendor_id = credentials

        image_urls: list[str] = draft_snapshot["image_urls"]
        images = [{"imageOrder": 0, "imageType": "REPRESENTATION", "vendorPath": image_urls[0]}]
        images.extend(
            {"imageOrder": i, "imageType": "DETAIL", "vendorPath": url} for i, url in enumerate(image_urls[1:], start=1)
        )
        item = dict(cf["item"])
        item["salePrice"] = int(draft_snapshot["sale_price"])
        item["maximumBuyCount"] = int(draft_snapshot["stock_quantity"])
        attributes = [
            {"attributeTypeName": name, "attributeValueName": str(value)}
            for name, value in provided_category_values.items()
        ]
        if attributes:
            item["attributes"] = attributes
        item["images"] = images
        items = [item]

        body: dict[str, Any] = {"displayCategoryCode": category_code, "sellerProductName": draft_snapshot["name"]}
        for field in _COUPANG_TOP_LEVEL_REQUIRED_FIELDS:
            body[field] = cf[field]
        body["vendorId"] = vendor_id
        body["requested"] = bool(cf["requested"])
        body["images"] = images
        body["items"] = items

        path = CREATE_PRODUCT_PATH
        authorization = self._authorization(access_key, secret_key, "POST", path, "")
        response = self._request_with_retry(
            "POST",
            path,
            headers={"Authorization": authorization, "Content-Type": "application/json;charset=UTF-8"},
            json_body=body,
        )
        raise_for_status("coupang", response.status_code)
        with external_call("coupang"):
            payload = response.json()
            inner = payload.get("data") or {}
            code = inner.get("code")
            seller_product_id = inner.get("data")
        accepted = code == "SUCCESS" and seller_product_id is not None
        return ProductCreateResult(
            accepted=accepted,
            platform_result_code=str(code),
            channel_product_id=str(seller_product_id) if seller_product_id is not None else None,
            channel_option_id=None,  # 승인 후 별도 조회 필요(모듈 docstring 참고) - 추측하지 않는다.
        )

    def fetch_registration_status(self, platform_product_id: str) -> dict[str, Any]:
        """등록 접수된 상품(sellerProductId)의 심사/승인 상태를 조회한다(모듈 상단
        PRODUCT_QUERY_PATH_TMPL 주석 참고). 승인이 끝나야 items[].vendorItemId가
        채워진다 - 그 전에는 channel_option_ids를 빈 목록으로 둔다(추측 금지)."""
        credentials = self._get_credentials()
        if credentials is None:
            raise MarketplaceCredentialMissingError("coupang")
        access_key, secret_key, _vendor_id = credentials
        path = PRODUCT_QUERY_PATH_TMPL.format(seller_product_id=platform_product_id)
        authorization = self._authorization(access_key, secret_key, "GET", path, "")
        response = self._request_with_retry("GET", path, headers={"Authorization": authorization})
        raise_for_status("coupang", response.status_code)
        with external_call("coupang"):
            payload = response.json()
            data = payload.get("data") or {}
        items = data.get("items") or []
        vendor_item_ids = [str(it["vendorItemId"]) for it in items if it.get("vendorItemId") is not None]
        status_name = data.get("statusName")
        return {"status_name": str(status_name) if status_name else None, "channel_option_ids": vendor_item_ids}

    def create_product_with_options(self, draft_snapshot: dict[str, Any]) -> ProductOptionsCreateResult:
        """하나의 로컬 상품에 속한 여러 SKU를 쿠팡 items[] 하나(상품 하나, 옵션
        여러 개)로 묶어 등록 접수한다(모듈 상단 COUPANG_MAX_OPTION_ITEMS 주석의
        공식 스펙 근거 참고). create_product()와 마찬가지로 등록 응답은
        sellerProductId만 돌려주고 vendorItemId(옵션 단위 식별자)는 승인 후
        fetch_option_registration_status()로만 확인할 수 있다."""
        category_code = draft_snapshot.get("category_code")
        if not category_code:
            raise MarketplaceValidationError("category_code(displayCategoryCode)가 비어 있습니다.")
        core_missing = _validate_coupang_option_group_draft(draft_snapshot)
        requirements = self.fetch_category_requirements(category_code)
        mandatory_names = requirements.mandatory_names()
        items_snapshot: list[dict[str, Any]] = draft_snapshot.get("items") or []
        category_missing: list[str] = []
        for item in items_snapshot:
            option_value_names = {str(a) for a, _v in (item.get("option_values") or [])}
            for name in mandatory_names:
                if name not in option_value_names:
                    category_missing.append(
                        f"items[product_option_id={item.get('product_option_id')}].option_values.{name}"
                    )
        missing = core_missing + category_missing
        if missing:
            raise MarketplaceValidationError(
                f"쿠팡 옵션조합 상품 등록에 필요한 항목이 비어 있습니다(카테고리={category_code}, 추측 금지): "
                + ", ".join(missing)
            )

        credentials = self._get_credentials()
        if credentials is None:
            raise MarketplaceCredentialMissingError("coupang")
        access_key, secret_key, vendor_id = credentials

        image_urls: list[str] = draft_snapshot["image_urls"]
        images = [{"imageOrder": 0, "imageType": "REPRESENTATION", "vendorPath": image_urls[0]}]
        images.extend(
            {"imageOrder": i, "imageType": "DETAIL", "vendorPath": url} for i, url in enumerate(image_urls[1:], start=1)
        )
        item_template = dict(draft_snapshot["channel_fields"]["item"])

        items: list[dict[str, Any]] = []
        for item_snapshot in items_snapshot:
            option_values = item_snapshot["option_values"]
            item_payload = dict(item_template)
            item_payload["itemName"] = "/".join(str(v) for _a, v in option_values)[:_COUPANG_ITEM_NAME_MAX_LENGTH]
            item_payload["salePrice"] = int(item_snapshot["sale_price"])
            item_payload["maximumBuyCount"] = int(item_snapshot["stock_quantity"])
            item_payload["externalVendorSku"] = item_snapshot["seller_product_code"]
            item_payload["attributes"] = [
                {"attributeTypeName": str(axis), "attributeValueName": str(value)} for axis, value in option_values
            ]
            item_payload["images"] = images
            items.append(item_payload)

        cf = draft_snapshot["channel_fields"]
        body: dict[str, Any] = {"displayCategoryCode": category_code, "sellerProductName": draft_snapshot["name"]}
        for field in _COUPANG_TOP_LEVEL_REQUIRED_FIELDS:
            body[field] = cf[field]
        body["vendorId"] = vendor_id
        body["requested"] = bool(cf["requested"])
        body["images"] = images
        body["items"] = items

        path = CREATE_PRODUCT_PATH
        authorization = self._authorization(access_key, secret_key, "POST", path, "")
        response = self._request_with_retry(
            "POST",
            path,
            headers={"Authorization": authorization, "Content-Type": "application/json;charset=UTF-8"},
            json_body=body,
        )
        raise_for_status("coupang", response.status_code)
        with external_call("coupang"):
            payload = response.json()
            inner = payload.get("data") or {}
            code = inner.get("code")
            seller_product_id = inner.get("data")
        accepted = code == "SUCCESS" and seller_product_id is not None
        return ProductOptionsCreateResult(
            accepted=accepted,
            platform_result_code=str(code),
            channel_product_id=str(seller_product_id) if seller_product_id is not None else None,
            channel_option_id=None,  # 쿠팡은 옵션 상위의 별도 채널상품ID 개념이 없다.
            items=[
                ProductOptionItemResult(seller_product_code=item_snapshot["seller_product_code"])
                for item_snapshot in items_snapshot
            ],
        )

    def fetch_option_registration_status(
        self, channel_product_id: str, channel_option_id: Optional[str] = None
    ) -> ProductOptionRegistrationStatus:
        """등록 접수된 상품(sellerProductId)을 재조회해 품목별 externalVendorSku와
        확정된 vendorItemId(승인 전이면 null)를 함께 돌려준다(모듈 상단
        PRODUCT_QUERY_PATH_TMPL 주석 참고) - fetch_registration_status와 달리
        SKU별 결과를 그대로 보존한다(배열 순서로 추정하지 않고 호출부가
        externalVendorSku로 다시 대응시킨다)."""
        credentials = self._get_credentials()
        if credentials is None:
            raise MarketplaceCredentialMissingError("coupang")
        access_key, secret_key, _vendor_id = credentials
        path = PRODUCT_QUERY_PATH_TMPL.format(seller_product_id=channel_product_id)
        authorization = self._authorization(access_key, secret_key, "GET", path, "")
        response = self._request_with_retry("GET", path, headers={"Authorization": authorization})
        raise_for_status("coupang", response.status_code)
        with external_call("coupang"):
            payload = response.json()
            data = payload.get("data") or {}
        status_name = data.get("statusName")
        items = [
            ProductOptionItemResult(
                seller_product_code=it["externalVendorSku"],
                channel_option_id=str(it["vendorItemId"]) if it.get("vendorItemId") is not None else None,
            )
            for it in (data.get("items") or [])
            if it.get("externalVendorSku")
        ]
        return ProductOptionRegistrationStatus(status_name=str(status_name) if status_name else None, items=items)

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

    def _request_with_retry(
        self, method: str, url: str, headers: dict[str, str], json_body: Optional[dict[str, Any]] = None
    ) -> httpx.Response:
        """HTTP 429(Rate Limit)에 대해 지수 백오프(1s -> 2s -> 4s -> 8s -> 16s)로
        최대 RATE_LIMIT_MAX_RETRIES회 재시도한다. 재시도 소진 시 RATE_LIMITED 오류를
        던진다. 네트워크/연결 오류는 external_call이 안전한 오류로 변환한다.

        로그에는 요청 URL(경로에 vendorId 포함)을 남기지 않는다 - 안전한 메타데이터만.
        json_body가 있으면 그대로 요청 본문에 실린다(HMAC 서명 대상에는 포함되지
        않는다 - _authorization()의 서명 메시지는 method+path+query뿐이다)."""
        attempt = 0
        while True:
            with external_call("coupang"):
                response = self._http().request(method, url, headers=headers, json=json_body)
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
            # 배송묶음(box) ID는 라인(vendorItemId) 단위로 저장한다 - 한 주문이 여러
            # 배송묶음으로 나뉠 수 있어(docstring 3번 참고) 주문 전체 대표값 하나로는
            # 분할배송의 두 번째 이후 배송묶음 라인에 잘못된 box id가 붙게 된다.
            for box in order_boxes:
                box_id = str(box["shipmentBoxId"]) if box.get("shipmentBoxId") is not None else None
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
                            # 라인(상품주문) 단위 식별자 - 쿠팡은 별도 상품주문번호를 주지 않아
                            # vendorItemId를 대용한다(송장 전송 시 이 값을 그대로 사용, 1단계 참고).
                            "platform_order_item_no": (
                                str(oi["vendorItemId"]) if oi.get("vendorItemId") is not None else None
                            ),
                            # 이 라인이 실제로 속한 배송묶음 ID(주문 전체 대표값이 아니라 라인별 실값).
                            "platform_shipment_box_id": box_id,
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

    # --- 반품 목록 조회 ---

    def _fetch_raw_return_requests(
        self, start_date: date, end_date: date, access_key: str, secret_key: str, vendor_id: str
    ) -> list[dict[str, Any]]:
        path = RETURN_REQUEST_PATH_TMPL.format(vendor_id=vendor_id)
        raw_items: list[dict[str, Any]] = []

        for status_value in RETURN_STATUS_CODES:
            window_start = start_date
            while window_start < end_date:
                window_end = min(window_start + timedelta(days=RETURN_MAX_RANGE_DAYS), end_date)
                next_token = ""
                while True:
                    params: list[tuple[str, str]] = [
                        ("createdAtFrom", window_start.isoformat()),
                        ("createdAtTo", window_end.isoformat()),
                        ("status", status_value),
                        ("maxPerPage", str(RETURN_MAX_PER_PAGE)),
                    ]
                    if next_token:
                        params.append(("nextToken", next_token))
                    query = urlencode(params)
                    authorization = self._authorization(access_key, secret_key, "GET", path, query)
                    response = self._request_with_retry(
                        "GET", f"{path}?{query}", headers={"Authorization": authorization}
                    )
                    raise_for_status("coupang", response.status_code)
                    with external_call("coupang"):
                        payload = response.json()
                        raw_items.extend(payload.get("data", []) or [])
                        next_token = payload.get("nextToken") or ""
                    if not next_token:
                        break
                window_start = window_end

        return raw_items

    @staticmethod
    def _normalize_return_requests(raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """receiptType=="RETURN"만 대상으로 한다(CANCEL은 이 메서드의 대상이 아니다 -
        fetch_returns()가 조회한 상태코드로는 RETURN 유형만 나오는 것으로 확인했지만,
        방어적으로 한 번 더 필터링한다)."""
        return [
            _normalize_receipt_item(item, _RETURN_RAW_TO_STATUS, "반품")
            for item in raw_items
            if item.get("receiptType") == "RETURN" and item.get("orderId") is not None
        ]

    def _fetch_raw_call_center_inquiries(
        self, start_date: date, end_date: date, access_key: str, secret_key: str, vendor_id: str
    ) -> list[dict[str, Any]]:
        path = CALL_CENTER_INQUIRY_PATH_TMPL.format(vendor_id=vendor_id)
        raw_items: list[dict[str, Any]] = []

        for status_value in CALL_CENTER_INQUIRY_STATUSES:
            window_start = start_date
            while window_start <= end_date:
                window_end = min(window_start + timedelta(days=CALL_CENTER_INQUIRY_MAX_RANGE_DAYS - 1), end_date)
                page_num = 1
                while True:
                    params: list[tuple[str, str]] = [
                        ("vendorId", vendor_id),
                        ("partnerCounselingStatus", status_value),
                        ("inquiryStartAt", window_start.isoformat()),
                        ("inquiryEndAt", window_end.isoformat()),
                        ("pageNum", str(page_num)),
                        ("pageSize", str(CALL_CENTER_INQUIRY_MAX_PER_PAGE)),
                    ]
                    query = urlencode(params)
                    authorization = self._authorization(access_key, secret_key, "GET", path, query)
                    response = self._request_with_retry(
                        "GET", f"{path}?{query}", headers={"Authorization": authorization}
                    )
                    raise_for_status("coupang", response.status_code)
                    with external_call("coupang"):
                        payload = response.json()
                        data = payload.get("data") or {}
                        raw_items.extend(data.get("content", []) or [])
                        pagination = data.get("pagination") or {}
                        current_page = pagination.get("currentPage") or page_num
                        total_pages = pagination.get("totalPages") or page_num
                    if current_page >= total_pages:
                        break
                    page_num += 1
                window_start = window_end + timedelta(days=1)

        return raw_items

    @staticmethod
    def _normalize_call_center_inquiries(raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """inquiryId가 없는 항목은 dedup 키가 없어 대상에서 제외한다(방어적)."""
        normalized: list[dict[str, Any]] = []
        for item in raw_items:
            inquiry_id = item.get("inquiryId")
            if inquiry_id is None:
                continue
            order_id = item.get("orderId")
            normalized.append(
                {
                    "platform_inquiry_id": str(inquiry_id),
                    "content": item.get("content") or "",
                    "inquiry_at": _parse_coupang_datetime(item.get("inquiryAt")),
                    "raw_status": f'{item.get("inquiryStatus")}:{item.get("csPartnerCounselingStatus")}',
                    "needs_answer": item.get("csPartnerCounselingStatus") == "requestAnswer",
                    "platform_order_no": str(order_id) if order_id is not None else None,
                    "customer_phone": item.get("buyerPhone"),
                }
            )
        return normalized

    def _fetch_raw_cancel_by_order(
        self, start_date: date, end_date: date, platform_order_no: str, access_key: str, secret_key: str, vendor_id: str
    ) -> list[dict[str, Any]]:
        """단일 주문의 취소 여부를 orderId+cancelType=CANCEL로 조회한다(status는
        쿼리에서 제외 - 모듈 상단 CANCEL_LOOKUP_WINDOW_DAYS 주석 참고). 단건이라
        nextToken 반복은 방어적으로만 유지한다(사실상 1페이지로 끝난다)."""
        path = RETURN_REQUEST_PATH_TMPL.format(vendor_id=vendor_id)
        raw_items: list[dict[str, Any]] = []
        next_token = ""
        while True:
            params: list[tuple[str, str]] = [
                ("createdAtFrom", start_date.isoformat()),
                ("createdAtTo", end_date.isoformat()),
                ("cancelType", "CANCEL"),
                ("orderId", platform_order_no),
            ]
            if next_token:
                params.append(("nextToken", next_token))
            query = urlencode(params)
            authorization = self._authorization(access_key, secret_key, "GET", path, query)
            response = self._request_with_retry("GET", f"{path}?{query}", headers={"Authorization": authorization})
            raise_for_status("coupang", response.status_code)
            with external_call("coupang"):
                payload = response.json()
                raw_items.extend(payload.get("data", []) or [])
                next_token = payload.get("nextToken") or ""
            if not next_token:
                break
        return raw_items

    @staticmethod
    def _normalize_cancel_requests(raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """receiptType=="CANCEL"만 대상으로 한다(방어적 필터 - _fetch_raw_cancel_by_order가
        cancelType=CANCEL로 요청했지만 응답 필드 자체를 한 번 더 확인한다)."""
        return [
            _normalize_receipt_item(item, _CANCEL_RAW_TO_STATUS, "취소")
            for item in raw_items
            if item.get("receiptType") == "CANCEL" and item.get("orderId") is not None
        ]

    # --- 교환 목록 조회 ---

    def _fetch_raw_exchange_requests(
        self, start_date: date, end_date: date, access_key: str, secret_key: str, vendor_id: str
    ) -> list[dict[str, Any]]:
        path = EXCHANGE_REQUEST_PATH_TMPL.format(vendor_id=vendor_id)
        raw_items: list[dict[str, Any]] = []

        window_start = start_date
        while window_start < end_date:
            window_end = min(window_start + timedelta(days=EXCHANGE_MAX_RANGE_DAYS), end_date)
            next_token = ""
            while True:
                params: list[tuple[str, str]] = [
                    ("createdAtFrom", f"{window_start.isoformat()}T00:00:00"),
                    ("createdAtTo", f"{window_end.isoformat()}T00:00:00"),
                ]
                if next_token:
                    params.append(("nextToken", next_token))
                query = urlencode(params)
                authorization = self._authorization(access_key, secret_key, "GET", path, query)
                response = self._request_with_retry("GET", f"{path}?{query}", headers={"Authorization": authorization})
                raise_for_status("coupang", response.status_code)
                with external_call("coupang"):
                    payload = response.json()
                    raw_items.extend(payload.get("data", []) or [])
                    next_token = payload.get("nextToken") or ""
                if not next_token:
                    break
            window_start = window_end

        return raw_items

    @staticmethod
    def _normalize_exchange_requests(raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """vendorItemId(OrderItem 연결 키)는 exchangeItemDtoV1s가 아니라 더 깊은
        collectInformationsDto.returndeliveryItemDtos에 있다(공식 문서 Response 확인,
        2026-09) - 회수정보가 채워지기 전에는 없을 수 있어, 없으면 라인 연결 없이
        주문 단위로만 남긴다(추측 금지)."""
        normalized = []
        for item in raw_items:
            order_id = item.get("orderId")
            if order_id is None:
                continue
            raw_status = item.get("exchangeStatus")
            std_status = _EXCHANGE_RAW_TO_STATUS.get(raw_status or "")
            if std_status is None:
                logger.warning("알 수 없는 쿠팡 교환 상태 - REVIEW로 보존")
                std_status = "REVIEW"
            exchange_items = item.get("exchangeItemDtoV1s") or []
            quantity = sum(int(ei.get("quantity", 0) or 0) for ei in exchange_items) or None
            collect_items = ((item.get("collectInformationsDto") or {}).get("returndeliveryItemDtos")) or []
            vendor_item_id = collect_items[0].get("vendorItemId") if collect_items else None
            reason = item.get("reasonCodeText") or item.get("reason") or item.get("reasonCode")
            normalized.append(
                {
                    "platform_order_no": str(order_id),
                    "platform_claim_id": (str(item["exchangeId"]) if item.get("exchangeId") is not None else None),
                    "platform_order_item_no": str(vendor_item_id) if vendor_item_id is not None else None,
                    "reason": _clip_reason(reason),
                    "status": std_status,
                    "raw_status": raw_status,
                    "requested_at": _parse_coupang_offset_datetime(item.get("createdAt")),
                    "quantity": quantity,
                    # exchangeAmount("교환배송비") - 공식 문서상 숫자로 확인됐으나(2026-09),
                    # 다른 쿠팡 API처럼 {currencyCode, units, nanos} 구조로 올 가능성도
                    # 방어적으로 처리한다.
                    "shipping_fee": _to_decimal_or_money(item.get("exchangeAmount")),
                    "fault_type": item.get("faultType"),
                }
            )
        return normalized

    # --- 정산 회차 요약 조회 ---

    def _fetch_raw_settlement_histories(
        self, start_date: date, end_date: date, access_key: str, secret_key: str
    ) -> list[dict[str, Any]]:
        path = SETTLEMENT_HISTORIES_PATH
        raw_items: list[dict[str, Any]] = []
        for year_month in _iter_year_months(start_date, end_date):
            query = urlencode([("revenueRecognitionYearMonth", year_month)])
            authorization = self._authorization(access_key, secret_key, "GET", path, query)
            response = self._request_with_retry("GET", f"{path}?{query}", headers={"Authorization": authorization})
            raise_for_status("coupang", response.status_code)
            with external_call("coupang"):
                payload = response.json()
                # 문서 확인 응답은 최상위가 배열이다(2026-09 조회) - 혹시 {data:[...]}로
                # 감싸져 오는 경우도 방어적으로 처리한다.
                items = payload if isinstance(payload, list) else (payload.get("data") or [])
                raw_items.extend(items)
        return raw_items

    @staticmethod
    def _normalize_settlement_histories(raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized = []
        for item in raw_items:
            settlement_date_str = item.get("settlementDate")
            settlement_date = _parse_coupang_date_only(settlement_date_str)
            raw_status = item.get("status")
            std_status = _SETTLEMENT_STATUS_TO_STD.get(raw_status or "", "SCHEDULED")
            expected = _to_decimal(item.get("settlementTargetAmount"))
            settled = _to_decimal(item.get("finalAmount")) if std_status == "COMPLETED" else Decimal("0")
            normalized.append(
                {
                    "settlement_cycle": settlement_date_str or "",
                    "settlement_type": item.get("settlementType"),
                    "scheduled_date": settlement_date,
                    "settled_date": settlement_date if std_status == "COMPLETED" else None,
                    "expected_amount": expected,
                    "settled_amount": settled,
                    "status": std_status,
                }
            )
        return normalized

    # --- 매출내역(정산 상세) 조회 ---

    def _fetch_raw_revenue_history(
        self, start_date: date, end_date: date, access_key: str, secret_key: str, vendor_id: str
    ) -> list[dict[str, Any]]:
        path = REVENUE_HISTORY_PATH
        raw_orders: list[dict[str, Any]] = []

        window_start = start_date
        while window_start < end_date:
            window_end = min(window_start + timedelta(days=REVENUE_HISTORY_MAX_RANGE_DAYS), end_date)
            token = ""
            while True:
                params: list[tuple[str, str]] = [
                    ("vendorId", vendor_id),
                    ("recognitionDateFrom", window_start.isoformat()),
                    ("recognitionDateTo", window_end.isoformat()),
                    ("token", token),
                ]
                query = urlencode(params)
                authorization = self._authorization(access_key, secret_key, "GET", path, query)
                response = self._request_with_retry("GET", f"{path}?{query}", headers={"Authorization": authorization})
                raise_for_status("coupang", response.status_code)
                with external_call("coupang"):
                    payload = response.json()
                    raw_orders.extend(payload.get("data", []) or [])
                    has_next = bool(payload.get("hasNext"))
                    token = payload.get("nextToken") or ""
                if not has_next or not token:
                    break
            window_start = window_end

        return raw_orders

    @staticmethod
    def _normalize_revenue_history(raw_orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """data[] 각 원소는 주문 단위(orderId/saleType/recognitionDate 등 공통 필드)이고,
        그 안의 items[] 배열이 라인(vendorItemId) 단위 금액이다(공식 문서 Response
        확인, 2026-09) - 라인마다 SettlementDetail 한 행을 만든다."""
        normalized = []
        for order_entry in raw_orders:
            order_id = order_entry.get("orderId")
            if order_id is None:
                continue
            sale_type = order_entry.get("saleType")
            recognition_date = _parse_coupang_date_only(order_entry.get("recognitionDate"))
            settled_date = _parse_coupang_date_only(order_entry.get("settlementDate"))
            for line in order_entry.get("items", []) or []:
                vendor_item_id = line.get("vendorItemId")
                gross = _to_decimal(line.get("saleAmount"))
                fee = _to_decimal(line.get("serviceFee")) + _to_decimal(line.get("serviceFeeVat"))
                net = _to_decimal(line.get("settlementAmount"))
                normalized.append(
                    {
                        "platform_order_no": str(order_id),
                        "platform_order_item_no": (str(vendor_item_id) if vendor_item_id is not None else None),
                        "sale_type": sale_type,
                        "recognition_date": recognition_date,
                        "settled_date": settled_date,
                        "gross_amount": gross,
                        "fee_amount": fee,
                        "net_amount": net,
                    }
                )
        return normalized


def _normalize_receipt_item(item: dict[str, Any], status_mapping: dict[str, str], type_label: str) -> dict[str, Any]:
    """반품(RETURN)/취소(CANCEL) 응답 항목 공통 정규화 - 두 유형이 같은 응답 스키마를
    공유한다(공식 문서 Response 필드 확인, 2026-09). status_mapping만 유형별로 다르다
    (반품은 5단계 세분 상태, 취소는 REQUESTED/COMPLETED 두 상태로 접는다 -
    _CANCEL_RAW_TO_STATUS 주석 참고). 호출부가 이미 receiptType으로 필터링했다고
    가정한다(order_id는 None이 아님을 호출부가 보장)."""
    order_id = item["orderId"]
    raw_status = item.get("receiptStatus")
    std_status = status_mapping.get(raw_status or "")
    if std_status is None:
        logger.warning("알 수 없는 쿠팡 %s 상태 - REVIEW로 보존", type_label)
        std_status = "REVIEW"
    return_items = item.get("returnItems") or []
    first_item = return_items[0] if return_items else {}
    quantity = sum(int(ri.get("cancelCount", 0) or 0) for ri in return_items) or None
    shipping_fee = _money_to_decimal(item.get("returnShippingCharge"))
    reason = item.get("reasonCodeText") or item.get("cancelReasonCategory2") or item.get("reasonCode")
    return {
        "platform_order_no": str(order_id),
        "platform_claim_id": (str(item["receiptId"]) if item.get("receiptId") is not None else None),
        "platform_order_item_no": (
            str(first_item["vendorItemId"]) if first_item.get("vendorItemId") is not None else None
        ),
        "reason": _clip_reason(reason),
        "status": std_status,
        "raw_status": raw_status,
        "requested_at": _parse_coupang_offset_datetime(item.get("createdAt")),
        # 이 API는 환불액을 제공하지 않는다(0으로 추정하지 않음 - None 유지).
        "refund_amount": None,
        "quantity": quantity,
        "shipping_fee": shipping_fee,
        "fault_type": item.get("faultByType"),
    }


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


def _parse_coupang_offset_datetime(value: Optional[str]) -> datetime:
    """쿠팡 반품/교환 응답의 createdAt(예: "2025-01-15T14:17:13.973885-08:00" - 밀리초·
    오프셋 포함)을 파싱한다. _parse_coupang_datetime과 달리 이 값은 항상 오프셋을
    포함하는 것으로 확인됐다(공식 문서 Response 예시, 2026-09 조회)."""
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(value)


def _parse_coupang_date_only(value: Optional[str]) -> Optional[date]:
    """쿠팡 정산 API의 날짜 전용 필드(예: "2026-07-31", YYYY-MM-dd)를 파싱한다."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _money_to_decimal(money: Optional[dict[str, Any]]) -> Optional[Decimal]:
    """쿠팡의 Money류 구조({currencyCode, units, nanos})를 Decimal로 변환한다.

    units는 정수부, nanos는 소수부(10^-9 단위) - 부호는 그대로 따른다(반품 배송비처럼
    고객에게 청구되는 금액은 음수로 내려올 수 있다, 임의 반전 금지)."""
    if not money:
        return None
    units = money.get("units")
    if units is None:
        return None
    nanos = money.get("nanos", 0) or 0
    return Decimal(units) + Decimal(nanos) / Decimal(10**9)


def _to_decimal(value: Any) -> Decimal:
    """단순 숫자 금액 필드를 Decimal로 변환한다(미제공 시 0 - 이 헬퍼를 쓰는 필드는
    모두 채널이 필수로 내려주는 것으로 확인된 필드에만 사용한다)."""
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _to_decimal_or_money(value: Any) -> Optional[Decimal]:
    """숫자 또는 Money류 구조({currencyCode, units, nanos}) 둘 다 방어적으로 처리한다
    (교환 응답의 exchangeAmount 필드 형식이 문서마다 다르게 보고돼 확정하지 못했다)."""
    if value is None:
        return None
    if isinstance(value, dict):
        return _money_to_decimal(value)
    return Decimal(str(value))


def _clip_reason(value: Any, length: int = 200) -> Optional[str]:
    """클레임 사유는 안전하게 잘라 저장한다(원문 전체 응답 저장 금지 - 모듈 docstring 참고)."""
    if value is None:
        return None
    text = str(value)
    return text[:length] if len(text) > length else text


def _iter_year_months(start_date: date, end_date: date) -> list[str]:
    """[start_date, end_date]가 걸치는 각 연-월을 "YYYY-MM" 문자열로 나열한다
    (쿠팡 정산 회차 요약 API가 월 단위로만 조회 가능하기 때문)."""
    months = []
    cursor = date(start_date.year, start_date.month, 1)
    while cursor <= end_date:
        months.append(f"{cursor.year:04d}-{cursor.month:02d}")
        cursor = date(cursor.year + 1, 1, 1) if cursor.month == 12 else date(cursor.year, cursor.month + 1, 1)
    return months
