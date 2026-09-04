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
import logging
import time as time_module
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

import bcrypt
import httpx

from integrations.malls.base_mall_connector import (
    SALE_STATUS_ON_SALE,
    SALE_STATUS_SUSPENDED,
    BaseMallConnector,
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
# 발송처리(송장 전송) API - 공식 문서 근거는 커넥터 클래스 docstring 및
# scripts 없이도 참조 가능한 CHANGELOG 대신 아래 submit_shipment()의 주석에 남긴다.
# 요청: {"dispatchProductOrders": [{"productOrderId","deliveryMethod","deliveryCompanyCode",
# "trackingNumber","dispatchDate"}]}, 응답: {"data": {"successProductOrderIds": [...],
# "failProductOrderInfos": [{"productOrderId","code","message"}]}}. 실 계정으로
# 아직 검증하지 못했다 - deliveryMethod="DELIVERY"(택배) 값과 응답 스키마는
# 공식 저장소(commerce-api-naver/commerce-api) 문의글 기준이며, 실 연동 전
# 재확인이 필요하다(모듈 docstring "실제 운영 환경으로 검증하며 확인한 사항" 절 참고).
DISPATCH_PATH = "/external/v1/pay-order/seller/product-orders/dispatch"
# 네이버 커머스 API 공식 문서 기준으로 작성했으나, 주문 API와 달리 아직 실제 계정으로
# 응답 스키마를 검증하지 못했다 - 실 연동 시 필드명/페이지네이션 방식이 다르면
# _fetch_raw_products_live/_normalize_live_products만 수정하면 된다(서비스 계층은
# 이 커넥터가 반환하는 정규화된 dict 형식에만 의존한다).
PRODUCT_SEARCH_PATH = "/external/v1/products/search"
PRODUCT_PAGE_SIZE = 100

# --- 재고/판매상태 변경 (상용 ERP 확장 3단계, 첫 묶음) ---
# 공식 OpenAPI 스펙(commerce-api-naver/commerce-api 저장소 docs/2.0.0-RC.js에 포함된
# 스펙 파일에서 직접 확인, 2026-09 조회 - apicenter.commerce.naver.com 자체는 이
# 세션의 WebFetch로 접근 불가했으나, 저장소에 공개된 실제 스펙 파일을 원문 그대로
# 파싱해 아래 경로/필드를 확인했다):
#   PUT /v1/products/origin-products/{originProductNo}/change-status
#   요청 바디(ExternalApiProductSaleStatusUpdateRequestVo.product):
#     {statusType: "SALE"|"OUTOFSTOCK"|"SUSPENSION"(필수, 이 셋만 입력 가능 - WAIT/
#      UNADMISSION/REJECTION/CLOSE/PROHIBITION/DELETE는 시스템 산출 상태라 입력 불가),
#      stockQuantity?: int(선택, 생략하면 재고 그대로 유지 - 같은 서비스의 원상품
#      전체수정 API 필드 설명 원문: "재고 수량을 입력하지 않으면...변하지 않습니다")}
#   재고 0으로 저장하면(원상품 전체수정 API의 동일 필드 설명 원문) statusType으로
#   보낸 값은 무시되고 상품 상태가 OUTOFSTOCK으로 강제된다 - 이 change-status
#   엔드포인트 자체의 설명에는 재반복되어 있지 않지만, 같은 서비스의 동일 필드가
#   공유하는 규칙으로 보고 그대로 적용한다(완전히 별개로 재확인되지는 않음).
#   응답: CommonResponse {code, message, data} - code로 성공/실패만 판단하고 그 외
#   원본 응답은 저장하지 않는다.
# path 파라미터는 channelProductNo가 아니라 originProductNo다 - 상품 검색 API
# 원본 응답에 이미 있던 값을 상품동기화 때 함께 저장해 둔다
# (models.product.ProductPlatformMap.platform_origin_product_id 참고).
PRODUCT_STATUS_PATH_TMPL = "/v1/products/origin-products/{origin_product_no}/change-status"
_SALE_STATUS_TO_NAVER = {SALE_STATUS_ON_SALE: "SALE", SALE_STATUS_SUSPENDED: "SUSPENSION"}
# 재고만 바꿀 때 현재 판매상태를 그대로 유지하기 위해 먼저 조회한다(원상품 조회 API,
# 같은 OpenAPI 스펙에서 확인 - GET 응답도 originProduct.statusType을 그대로 포함한다).
ORIGIN_PRODUCT_GET_PATH_TMPL = "/v2/products/origin-products/{origin_product_no}"
# change-status가 입력으로 받는 것으로 확인된 값(모듈 상단 주석 참고) - 조회된 현재
# 상태가 이 밖이면(승인대기/판매종료 등 시스템 상태) 재고만 바꾸려는 시도도 안전하게
# 차단한다(추측으로 강제 전환하지 않음).
_NAVER_INPUTABLE_STATUS_TYPES = frozenset({"SALE", "OUTOFSTOCK", "SUSPENSION"})


def _has_option_managed_stock(origin_product: dict[str, Any]) -> bool:
    """이 원상품의 재고가 "원상품 전체" 단위가 아니라 "옵션(조합)별"로 관리되는지
    확인한다(공식 OpenAPI 스펙 ExternalApiOptionInfoVo.product, 2026-09 조회 -
    originProduct.detailAttribute.optionInfo 경로). 조합형(optionCombinations)/
    표준형(optionStandards, standardOptionGroups) 옵션이 있거나 "옵션 재고 수량
    관리"(useStockManagement)를 켠 상품은, 상품 전체 재고(stockQuantity)가 옵션별
    재고의 합으로 자동 계산되며 직접 입력이 거부되거나 무시된다(공식 GitHub
    discussion #1194/#562 확인, 2026-09 조회: "조합형 옵션 상품이거나 옵션 재고
    관리를 활성화한 상품의 경우, 상품 재고 수량은 각 옵션 조합별 재고 수량의 총
    합으로 자동 계산됩니다"). 단독형(optionSimple)/직접입력형(optionCustom) 옵션
    또는 옵션 없음은 상품 전체 단위로 재고가 관리되므로 여기 해당하지 않는다.

    이 ERP는 옵션 조합별 재고를 개별적으로 설정하는 기능이 없다 - True를 반환하는
    상품은 update_inventory()가 명시적으로 차단한다(추측으로 상품 전체
    stockQuantity를 보내지 않음)."""
    option_info = ((origin_product.get("detailAttribute") or {}).get("optionInfo")) or {}
    if bool(option_info.get("useStockManagement")):
        return True
    if option_info.get("optionCombinations"):
        return True
    return bool(option_info.get("optionStandards") or option_info.get("standardOptionGroups"))


# HTTP 429(Rate Limit) 재시도 정책: Retry-After 헤더가 있으면 그 값을, 없으면
# 1s -> 2s -> 4s -> 8s -> 16s 지수 백오프로 대기 후 재시도한다.
RATE_LIMIT_MAX_RETRIES = 5
RATE_LIMIT_BACKOFF_BASE_SECONDS = 1.0

# --- 신규 상품 등록 / 제한된 정보 수정 (상용 ERP 확장 3단계, 두 번째 묶음) ---
# 공식 OpenAPI 스펙(commerce-api-naver/commerce-api 저장소 docs/2.0.0-RC.js, 2026-09
# 조회 - 위 재고/판매상태 전송과 동일한 방법으로 확인)에서 확인한 등록 계약:
#   POST /v2/products (createProduct_2.product)
#   요청(ExternalApiCreateProductRequestVo.product, 최상위 필수: originProduct,
#   smartstoreChannelProduct):
#     originProduct(ExternalApiOriginProductVo.product, 필수: detailAttribute,
#     detailContent, images, name, salePrice, statusType - 등록 시에는 statusType은
#     SALE만 가능, leafCategoryId/stockQuantity도 등록 시 필수라고 필드 설명에 명시):
#       {statusType:"SALE", leafCategoryId, name, images:{representativeImage:{url},
#        optionalImages:[{url}]}, detailContent, salePrice, stockQuantity,
#        deliveryInfo(ExternalApiDeliveryInfoVo, 필수: claimDeliveryInfo,
#        deliveryAttributeType, deliveryFee, deliveryType),
#        detailAttribute(ExternalApiBaseProductDetailAttributeVo, 필수:
#        afterServiceInfo{afterServiceTelephoneNumber,afterServiceGuideContent},
#        minorPurchasable(bool), originAreaInfo{originAreaCode 필수})}
#     smartstoreChannelProduct(ExternalApiSmartstoreChannelProductVo, 필수:
#       channelProductDisplayStatusType, naverShoppingRegistration)
#   응답(ExternalApiCreateUpdateProductResponseVo.product):
#     {originProductNo, smartstoreChannelProductNo, windowChannelProductNo} - 등록과
#     동시에 옵션 단위 식별자(channelProductNo)까지 동기적으로 확정 반환된다(쿠팡과
#     달리 승인 후 별도 조회가 필요 없다).
#   상품정보제공고시(detailAttribute.productInfoProvidedNotice, ExternalApi
#   ProductInfoProvidedNoticeVo)는 카테고리마다 다른 30여 개 하위 스키마가 있다 -
#   이번 라운드는 그중 "기타 재화"(productInfoProvidedNoticeType="ETC",
#   ExternalApiEtcInfoProvidedNoticeVo, 필수 8개 확인: itemName, manufacturer,
#   modelName, qualityAssuranceStandard, compensationProcedure, troubleShootingContents,
#   noRefundReason, returnCostReason) 하나만 입력 스키마를 확인했다 - 그 외 유형
#   (화장품/식품/의류 등 카테고리 전용 고시)은 스키마를 확인하지 못해 명시적으로
#   차단한다(추측 금지).
#   ⚠️ 범위 한정: "ETC 스키마를 구현했다"는 사실과 "이 leafCategoryId에 ETC를
#   써도 되는지"는 별개다 - 카테고리별로 어떤 상품정보제공고시 유형이 필수인지
#   조회하는 공식 API를 이 세션에서 확인하지 못했다(쿠팡의 카테고리 메타정보
#   조회 같은 확인된 엔드포인트가 네이버에는 없다). 그래서 이 커넥터는 "ETC 입력
#   형식은 지원하되, 이 카테고리에 ETC가 실제로 맞는지는 시스템이 검증할 수
#   없다" - 운영자가 판매자센터에서 직접 확인했다는 명시적 확인(channel_fields.
#   productInfoProvidedNotice.categoryNoticeTypeConfirmedByOperator=true)이 없으면
#   등록 자체를 차단한다(초안 저장은 이 검사와 무관하게 항상 가능하다).
#
#   GET/PUT /v2/products/origin-products/{originProductNo}: GET 응답의 originProduct
#   서브 객체와 PUT 요청 바디의 originProduct는 동일 스키마(ExternalApiOriginProductVo)
#   다 - PUT은 전체교체형(update 시에도 등록과 동일한 필수 필드 목록을 요구)이라,
#   상품명/판매가/상세설명만 바꾸려 해도 GET으로 받은 전체 객체에서 그 세 필드만
#   바꾸고 나머지(이미지/배송/재고/상세속성 등)는 그대로 되돌려 보내야 안전하다
#   (update_product_info 참고 - 이 방식 없이 세 필드만 담아 보내면 나머지 필드가
#   비거나 기본값으로 리셋될 위험이 있다).
CREATE_PRODUCT_PATH = "/v2/products"
# ExternalApiEtcInfoProvidedNoticeVo.product 필수 필드(위 주석 참고) - draft.
# channel_fields의 "productInfoProvidedNotice.etc.<key>" 키로 그대로 받는다.
_NAVER_ETC_NOTICE_REQUIRED_FIELDS = (
    "itemName",
    "manufacturer",
    "modelName",
    "qualityAssuranceStandard",
    "compensationProcedure",
    "troubleShootingContents",
    "noRefundReason",
    "returnCostReason",
)
# ExternalApiDeliveryInfoVo.product 필수 필드.
_NAVER_DELIVERY_INFO_REQUIRED_FIELDS = ("deliveryType", "deliveryAttributeType", "deliveryFee", "claimDeliveryInfo")
# ExternalApiClaimDeliveryInfoVo.product 필수 필드(claimDeliveryInfo 하위).
_NAVER_CLAIM_DELIVERY_REQUIRED_FIELDS = ("returnDeliveryFee", "exchangeDeliveryFee")
# ExternalApiAfterServiceInfoVo.product 필수 필드.
_NAVER_AFTER_SERVICE_REQUIRED_FIELDS = ("afterServiceTelephoneNumber", "afterServiceGuideContent")


def _validate_naver_publish_draft(draft: dict[str, Any]) -> None:
    """신규 등록 스냅샷이 위 공식 계약의 확인된 필수 항목을 전부 채웠는지 검사한다.
    하나라도 비어 있으면(카테고리·제조자·원산지·인증정보·배송비·반품지·판매가격을
    추측하거나 임의 기본값으로 채우지 않고) 채널 호출 전에 ValueError로 차단한다."""
    missing: list[str] = []
    if not draft.get("name"):
        missing.append("name(상품명)")
    if draft.get("sale_price") is None:
        missing.append("sale_price(판매가)")
    if not draft.get("description_html"):
        missing.append("description_html(상세설명)")
    if not draft.get("category_code"):
        missing.append("category_code(leafCategoryId)")
    image_urls = draft.get("image_urls") or []
    if not image_urls:
        missing.append("image_urls(대표이미지 최소 1장 필요)")
    if draft.get("stock_quantity") is None:
        missing.append("stock_quantity(등록 시 필수)")

    cf = draft.get("channel_fields") or {}
    if cf.get("minorPurchasable") is None:
        missing.append("channel_fields.minorPurchasable")
    if cf.get("naverShoppingRegistration") is None:
        missing.append("channel_fields.naverShoppingRegistration")
    if not cf.get("channelProductDisplayStatusType"):
        missing.append("channel_fields.channelProductDisplayStatusType")

    origin_area = cf.get("originAreaInfo") or {}
    if not origin_area.get("originAreaCode"):
        missing.append("channel_fields.originAreaInfo.originAreaCode")

    after_service = cf.get("afterServiceInfo") or {}
    for field in _NAVER_AFTER_SERVICE_REQUIRED_FIELDS:
        if not after_service.get(field):
            missing.append(f"channel_fields.afterServiceInfo.{field}")

    delivery = cf.get("deliveryInfo") or {}
    for field in ("deliveryType", "deliveryAttributeType"):
        if not delivery.get(field):
            missing.append(f"channel_fields.deliveryInfo.{field}")
    delivery_fee = delivery.get("deliveryFee") or {}
    if not delivery_fee.get("deliveryFeeType"):
        missing.append("channel_fields.deliveryInfo.deliveryFee.deliveryFeeType")
    claim_delivery = delivery.get("claimDeliveryInfo") or {}
    for field in _NAVER_CLAIM_DELIVERY_REQUIRED_FIELDS:
        if claim_delivery.get(field) is None:
            missing.append(f"channel_fields.deliveryInfo.claimDeliveryInfo.{field}")

    notice = cf.get("productInfoProvidedNotice") or {}
    if notice.get("productInfoProvidedNoticeType") != "ETC":
        missing.append("channel_fields.productInfoProvidedNotice.productInfoProvidedNoticeType(현재 ETC만 지원)")
    else:
        etc = notice.get("etc") or {}
        for field in _NAVER_ETC_NOTICE_REQUIRED_FIELDS:
            if not etc.get(field):
                missing.append(f"channel_fields.productInfoProvidedNotice.etc.{field}")
        # ETC 스키마를 "구현"한 것과, 이 leafCategoryId에 ETC를 써도 되는지("적합성")는
        # 별개다 - 이 커넥터는 카테고리별 고시 유형 요구사항을 조회할 공식 API를
        # 확인하지 못했으므로(네이버는 쿠팡의 카테고리 메타정보 조회 같은 확인된
        # 엔드포인트가 없다), 시스템이 스스로 "이 카테고리는 ETC가 맞다"고 판단하지
        # 않는다. 대신 운영자가 네이버 판매자센터에서 이 leafCategoryId의 상품정보
        # 제공고시 유형을 직접 확인했다는 명시적 확인(true)을 요구한다 - 이 값이
        # 없으면(또는 False면) ETC 하위 필드가 다 채워졌어도 등록을 차단한다(초안
        # 저장은 이 검사 이전에 이미 자유롭게 가능 - services.product_publish_service.
        # save_draft는 어떤 검증도 하지 않는다. 여기서 막는 것은 "전송(등록)"만이다).
        if notice.get("categoryNoticeTypeConfirmedByOperator") is not True:
            missing.append(
                "channel_fields.productInfoProvidedNotice.categoryNoticeTypeConfirmedByOperator"
                "(true여야 함 - 이 카테고리에 ETC 고시가 맞는지 판매자센터에서 직접 확인 필요."
                " ETC 입력 형식은 지원하지만 카테고리 적합성은 시스템이 검증할 수 없다)"
            )

    if missing:
        raise MarketplaceValidationError(
            "네이버 상품 등록에 필요한 항목이 비어 있습니다(추측 금지): " + ", ".join(missing)
        )


# --- 옵션 조합 상품 등록 (상용 ERP 확장 3단계, 세 번째 묶음) ---
# 공식 OpenAPI 스펙(commerce-api-naver/commerce-api 저장소 docs/2.0.0-RC.js, 2026-09
# 조회 - 위 신규 상품 등록과 동일한 방법으로 원문 JSON 스키마를 직접 파싱해 확인)
# ExternalApiOptionInfoVo.product / ExternalApiOptionCombinationVo.product:
#   detailAttribute.optionInfo.optionCombinationGroupNames.optionGroupName1..3(축 이름,
#   최대 3개) + optionCombinations[](축 값 조합, 최대 3축):
#     {optionName1(필수)/optionName2/optionName3: 그 축의 값,
#      stockQuantity: int(미입력 시 0), price: int(옵션가 - salePrice에 대한
#      "추가금", 미입력 시 0원. 절대가가 아니다 - models.product.
#      ProductPublishOptionGroupDraft.base_sale_price 모듈 docstring 참고),
#      sellerManagerCode: str(판매자 관리 코드), usable: bool(미입력 시 true)}
#   optionInfo.useStockManagement=true로 설정해야 재고가 옵션 조합별로 관리된다
#   (미설정 시 원상품 전체 stockQuantity만 쓰인다 - 우리는 조합별 재고를 쓰므로
#   항상 true로 보낸다).
#   ⚠️ 등록 응답(ExternalApiCreateUpdateProductResponseVo.product)에는
#   originProductNo/smartstoreChannelProductNo/windowChannelProductNo만 있고 조합별
#   식별자(optionCombinations[].id)가 없다 - 단일 옵션 등록(create_product)과 달리
#   조합별 식별자는 등록 후 GET /v2/products/origin-products/{originProductNo}
#   재조회로만 확인할 수 있다(fetch_option_registration_status 참고). id는
#   ExternalApiOptionCombinationVo.product 필드 설명("옵션 ID 입력 시 기존 옵션
#   수정")으로 미루어 서버가 생성 후 부여하고 조회로 되돌려주는 값으로 추정한다
#   (조회 스키마가 등록 스키마와 동일하므로 이 자리에 채워져 돌아온다).
NAVER_MAX_OPTION_COMBINATION_AXES = 3


def _validate_naver_option_group_draft(draft: dict[str, Any]) -> None:
    """옵션 조합 상품 등록 스냅샷의 공통(상품 레벨) 필수 항목을 검사한다 -
    _validate_naver_publish_draft와 거의 동일하나, sale_price/stock_quantity는
    품목(item)마다 따로 있어 여기서는 검사하지 않고 base_sale_price(상품 기준
    판매가)만 검사한다(호출부가 items 자체의 완전성은 별도로 검사한다 -
    services.product_option_publish_service 참고)."""
    missing: list[str] = []
    if not draft.get("name"):
        missing.append("name(상품명)")
    if draft.get("base_sale_price") is None:
        missing.append("base_sale_price(salePrice, 옵션가의 기준이 되는 상품 판매가)")
    if not draft.get("description_html"):
        missing.append("description_html(상세설명)")
    if not draft.get("category_code"):
        missing.append("category_code(leafCategoryId)")
    image_urls = draft.get("image_urls") or []
    if not image_urls:
        missing.append("image_urls(대표이미지 최소 1장 필요)")

    cf = draft.get("channel_fields") or {}
    if cf.get("minorPurchasable") is None:
        missing.append("channel_fields.minorPurchasable")
    if cf.get("naverShoppingRegistration") is None:
        missing.append("channel_fields.naverShoppingRegistration")
    if not cf.get("channelProductDisplayStatusType"):
        missing.append("channel_fields.channelProductDisplayStatusType")

    origin_area = cf.get("originAreaInfo") or {}
    if not origin_area.get("originAreaCode"):
        missing.append("channel_fields.originAreaInfo.originAreaCode")

    after_service = cf.get("afterServiceInfo") or {}
    for f in _NAVER_AFTER_SERVICE_REQUIRED_FIELDS:
        if not after_service.get(f):
            missing.append(f"channel_fields.afterServiceInfo.{f}")

    delivery = cf.get("deliveryInfo") or {}
    for f in ("deliveryType", "deliveryAttributeType"):
        if not delivery.get(f):
            missing.append(f"channel_fields.deliveryInfo.{f}")
    delivery_fee = delivery.get("deliveryFee") or {}
    if not delivery_fee.get("deliveryFeeType"):
        missing.append("channel_fields.deliveryInfo.deliveryFee.deliveryFeeType")
    claim_delivery = delivery.get("claimDeliveryInfo") or {}
    for f in _NAVER_CLAIM_DELIVERY_REQUIRED_FIELDS:
        if claim_delivery.get(f) is None:
            missing.append(f"channel_fields.deliveryInfo.claimDeliveryInfo.{f}")

    notice = cf.get("productInfoProvidedNotice") or {}
    if notice.get("productInfoProvidedNoticeType") != "ETC":
        missing.append("channel_fields.productInfoProvidedNotice.productInfoProvidedNoticeType(현재 ETC만 지원)")
    else:
        etc = notice.get("etc") or {}
        for f in _NAVER_ETC_NOTICE_REQUIRED_FIELDS:
            if not etc.get(f):
                missing.append(f"channel_fields.productInfoProvidedNotice.etc.{f}")
        if notice.get("categoryNoticeTypeConfirmedByOperator") is not True:
            missing.append(
                "channel_fields.productInfoProvidedNotice.categoryNoticeTypeConfirmedByOperator"
                "(true여야 함 - 이 카테고리에 ETC 고시가 맞는지 판매자센터에서 직접 확인 필요)"
            )

    items = draft.get("items") or []
    if not items:
        missing.append("items(등록할 SKU가 1개 이상 필요)")

    if missing:
        raise MarketplaceValidationError(
            "네이버 옵션조합 상품 등록에 필요한 항목이 비어 있습니다(추측 금지): " + ", ".join(missing)
        )

    axis_names: Optional[list[str]] = None
    seen_codes: set[str] = set()
    seen_combos: set[tuple[str, ...]] = set()
    item_missing: list[str] = []
    for item in items:
        option_values = item.get("option_values") or []
        axes = [str(a) for a, _v in option_values]
        if axis_names is None:
            axis_names = axes
        elif axes != axis_names:
            raise MarketplaceValidationError(
                "모든 SKU는 같은 옵션축 순서를 가져야 합니다(추측 금지): "
                f"기준={axis_names}, 다른 값={axes}(product_option_id={item.get('product_option_id')})"
            )
        if len(axes) == 0 or len(axes) > NAVER_MAX_OPTION_COMBINATION_AXES:
            raise MarketplaceValidationError(
                f"네이버 조합형 옵션은 축이 1~{NAVER_MAX_OPTION_COMBINATION_AXES}개여야 합니다"
                f"(현재 {len(axes)}개, product_option_id={item.get('product_option_id')})."
            )
        combo_key = tuple(str(v) for _a, v in option_values)
        if combo_key in seen_combos:
            raise MarketplaceValidationError(f"중복된 옵션 조합입니다(추측 금지): {combo_key}")
        seen_combos.add(combo_key)

        code = item.get("seller_product_code")
        if not code:
            item_missing.append(f"items[product_option_id={item.get('product_option_id')}].seller_product_code")
        elif code in seen_codes:
            raise MarketplaceValidationError(f"같은 초안 안에 판매자 관리코드가 중복됩니다: {code}")
        else:
            seen_codes.add(code)
        if item.get("sale_price") is None:
            item_missing.append(f"items[product_option_id={item.get('product_option_id')}].sale_price")
        if item.get("stock_quantity") is None:
            item_missing.append(f"items[product_option_id={item.get('product_option_id')}].stock_quantity")
    if item_missing:
        raise MarketplaceValidationError(
            "네이버 옵션조합 상품 등록에 필요한 품목별 항목이 비어 있습니다(추측 금지): " + ", ".join(item_missing)
        )


class NaverSmartstoreConnector(BaseMallConnector):
    platform_code = "naver_smartstore"
    supports_shipment_submit = True
    supports_inventory_update = True
    supports_sale_status_update = True
    supports_product_create = True
    supports_product_info_update = True
    supports_product_option_create = True

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
        if credentials is None:
            raise MarketplaceCredentialMissingError("naver")
        client_id, client_secret, seller_id = credentials
        raw_contents = self._fetch_raw_orders_live(start_date, end_date, client_id, client_secret, seller_id)
        return self._normalize_live_orders(raw_contents)

    def fetch_order_detail(self, platform_order_no: str) -> dict[str, Any]:
        # 주문 상세 단건 조회는 아직 미구현 - 더미로 위장하지 않고 미지원 오류를 던진다.
        raise MarketplaceCapabilityUnsupportedError("naver", "order_detail")

    def update_shipment(self, platform_order_no: str, carrier: str, tracking_no: str) -> bool:
        # 레거시 인터페이스 - 신규 코드는 submit_shipment()를 사용한다(base 클래스 참고).
        raise MarketplaceCapabilityUnsupportedError("naver", "shipment_update")

    def submit_shipment(
        self,
        platform_order_item_no: str,
        carrier_code: str,
        tracking_no: str,
        dispatch_date: date,
        platform_order_no: Optional[str] = None,
        platform_shipment_box_id: Optional[str] = None,
    ) -> ShipmentSubmitResult:
        # 네이버는 productOrderId(platform_order_item_no) 하나로 충분하다 -
        # platform_order_no/platform_shipment_box_id는 쓰지 않는다(쿠팡 전용).
        credentials = self._get_credentials()
        if credentials is None:
            raise MarketplaceCredentialMissingError("naver")
        client_id, client_secret, seller_id = credentials
        access_token = self._fetch_access_token(client_id, client_secret, seller_id)
        response = self._request_with_retry(
            "POST",
            DISPATCH_PATH,
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "dispatchProductOrders": [
                    {
                        "productOrderId": platform_order_item_no,
                        "deliveryMethod": "DELIVERY",
                        "deliveryCompanyCode": carrier_code,
                        "trackingNumber": tracking_no,
                        "dispatchDate": self._to_naver_datetime(dispatch_date),
                    }
                ]
            },
        )
        # 비200은 응답 본문(개인정보 가능) 노출 없이 안전한 외부 API 오류로 변환한다.
        raise_for_status("naver", response.status_code)
        with external_call("naver"):
            data = response.json().get("data", {})
            success_ids = data.get("successProductOrderIds", [])
            fail_infos = data.get("failProductOrderInfos", [])
        if platform_order_item_no in success_ids:
            return ShipmentSubmitResult(accepted=True, platform_result_code="OK")
        # 실패 사유 코드만 담고(개인정보 아님), message 원문은 담지 않는다 - 원본 응답에
        # 상품/고객 관련 문구가 섞여 나올 수 있어 안전한 code만 사용한다.
        fail_code = next(
            (str(info.get("code")) for info in fail_infos if info.get("productOrderId") == platform_order_item_no),
            "UNKNOWN",
        )
        return ShipmentSubmitResult(accepted=False, platform_result_code=fail_code)

    def fetch_settlements(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        # 정산 연동 미구현 - 더미로 위장하지 않고 미지원 오류를 던진다. 상용 ERP 확장
        # (2단계) 조사에서도 apicenter.commerce.naver.com/GitHub commerce-api-naver
        # discussions를 확인했으나 판매자용 정산/지급 조회 API 스펙을 찾지 못했다
        # (2026-09 조회) - 확인되지 않은 계약을 지어내지 않고 미지원으로 유지한다.
        raise MarketplaceCapabilityUnsupportedError("naver", "settlement")

    # 취소/반품/교환(fetch_cancellations/fetch_returns/fetch_exchanges)도 상용 ERP 확장
    # (2단계)에서 조사했으나 base 기본 구현(미지원 오류)을 그대로 상속한다: 클레임
    # 정보(claimId/claimStatus 등)는 "상품주문 상세 조회"(단건, productOrderId 필요)
    # 응답에서만 확인되고, 쿠팡의 returnRequests/exchangeRequests처럼 기간으로 대량
    # 조회하는 별도 목록 API가 공식 문서/GitHub discussions에서 확인되지 않았다
    # (2026-09 조회) - 단건 조회를 주문 수만큼 반복 호출하는 구조는 이번 범위에서
    # 구현하지 않는다(확인되지 않은 계약으로 짐작해 구현하지 않기 위함).
    # supports_cancellation_sync/supports_return_sync/supports_exchange_sync는
    # base 기본값(False)을 그대로 상속한다.

    def fetch_products(self) -> list[dict[str, Any]]:
        """상품(원본상품 + 채널상품 + 옵션조합) 목록을 정규화된 형식으로 조회한다.

        services.product_sync_service.ProductSyncService.sync_products_from_naver가
        이 결과를 사용해 상품/옵션/플랫폼매핑/이미지를 등록·갱신한다 - 상품이 생기는
        유일한 경로다(주문 API에서는 더 이상 상품이 생기지 않는다).
        """
        if self._product_cache is not None:
            return self._product_cache
        credentials = self._get_credentials()
        if credentials is None:
            raise MarketplaceCredentialMissingError("naver")
        client_id, client_secret, seller_id = credentials
        raw_pages = self._fetch_raw_products_live(client_id, client_secret, seller_id)
        result = self._normalize_live_products(raw_pages)
        self._product_cache = result
        return result

    def update_inventory(
        self, platform_option_id: str, quantity: int, platform_origin_product_id: Optional[str] = None
    ) -> ProductSyncActionResult:
        """원상품(origin product) 단위 재고 수량만 전송한다(모듈 상단
        PRODUCT_STATUS_PATH_TMPL 주석의 공식 스펙 근거 참고). change-status는
        statusType이 필수라, 재고만 바꾸고 판매상태는 건드리지 않기 위해 먼저 현재
        원상품 정보를 조회해 statusType은 그대로 함께 보낸다.

        ⚠️ 지원 범위(중요): 이 메서드가 보내는 stockQuantity는 "원상품 전체"의
        재고 수량이지 "개별 옵션(조합)"의 재고가 아니다. 옵션 없음/단독형/직접
        입력형 옵션 상품은 재고가 원상품 단위로 관리되어 이 방식이 정확하지만,
        조합형/표준형(간편) 옵션 상품이거나 "옵션 재고 수량 관리"를 사용 중인
        상품은 원상품 stockQuantity가 옵션(조합)별 재고의 합으로 자동 계산되며
        직접 입력이 거부되거나 무시된다(공식 GitHub discussion #1194/#562 확인,
        2026-09 조회) - 이 ERP는 옵션 조합별 재고를 개별적으로 설정하는 기능이
        없으므로, 그런 상품 구조로 확인되면 추측으로 원상품 stockQuantity를
        보내지 않고 명시적으로 차단한다(_has_option_managed_stock 참고)."""
        if not platform_origin_product_id:
            # vendorItemId(channelProductNo)만으로는 이 API를 호출할 수 없다 - 원상품번호를
            # 상품동기화 때 저장해 두지 못한(과거 데이터) 매핑은 안전하게 차단한다.
            raise MarketplaceCapabilityUnsupportedError("naver", "inventory_update_missing_origin_product_id")
        credentials = self._get_credentials()
        if credentials is None:
            raise MarketplaceCredentialMissingError("naver")
        client_id, client_secret, seller_id = credentials
        access_token = self._fetch_access_token(client_id, client_secret, seller_id)
        origin_product = self._fetch_origin_product(platform_origin_product_id, access_token)
        current_status = origin_product.get("statusType")
        if not current_status:
            raise MarketplaceExternalAPIError("naver", "PARSE_FAILED", False)
        if current_status not in _NAVER_INPUTABLE_STATUS_TYPES:
            # 승인대기/판매종료 등 시스템 상태 - 추측으로 SALE/SUSPENSION 중 하나로
            # 강제 전환하지 않고 명시적으로 차단한다.
            raise MarketplaceCapabilityUnsupportedError("naver", f"inventory_update_blocked_status_{current_status}")
        if _has_option_managed_stock(origin_product):
            # 조합형/표준형 옵션 또는 옵션 재고 관리 사용 상품 - 원상품 단위
            # stockQuantity를 직접 설정할 수 없는 구조다(모듈 docstring 참고).
            # 이 ERP는 옵션 조합별 재고를 다루지 않으므로 추측하지 않고 차단한다.
            raise MarketplaceCapabilityUnsupportedError("naver", "inventory_update_unsupported_option_structure")
        return self._put_change_status(
            platform_origin_product_id, access_token, status_type=str(current_status), stock_quantity=quantity
        )

    def update_sale_status(
        self, platform_option_id: str, target_status: str, platform_origin_product_id: Optional[str] = None
    ) -> ProductSyncActionResult:
        """판매상태만 전송한다(재고는 stockQuantity를 생략해 현재값을 유지한다 -
        모듈 상단 주석의 공식 필드 설명 근거 참고)."""
        if not platform_origin_product_id:
            raise MarketplaceCapabilityUnsupportedError("naver", "sale_status_update_missing_origin_product_id")
        naver_status = _SALE_STATUS_TO_NAVER.get(target_status)
        if naver_status is None:
            raise MarketplaceValidationError(f"알 수 없는 target_status입니다: {target_status}")
        credentials = self._get_credentials()
        if credentials is None:
            raise MarketplaceCredentialMissingError("naver")
        client_id, client_secret, seller_id = credentials
        access_token = self._fetch_access_token(client_id, client_secret, seller_id)
        return self._put_change_status(platform_origin_product_id, access_token, status_type=naver_status)

    def _fetch_origin_product(self, origin_product_no: str, access_token: str) -> dict[str, Any]:
        """원상품 조회(모듈 상단 ORIGIN_PRODUCT_GET_PATH_TMPL 주석 참고) - 응답의
        originProduct 서브 객체를 그대로 반환한다(statusType, detailAttribute.
        optionInfo 등을 포함 - update_inventory가 현재 판매상태 보존과 옵션 구조
        확인에 함께 사용한다)."""
        path = ORIGIN_PRODUCT_GET_PATH_TMPL.format(origin_product_no=origin_product_no)
        response = self._request_with_retry("GET", path, headers={"Authorization": f"Bearer {access_token}"})
        raise_for_status("naver", response.status_code)
        with external_call("naver"):
            payload = response.json()
            origin_product = payload.get("originProduct")
        if not origin_product:
            raise MarketplaceExternalAPIError("naver", "PARSE_FAILED", False, http_status=response.status_code)
        return dict(origin_product)

    def create_product(self, draft_snapshot: dict[str, Any]) -> ProductCreateResult:
        """옵션 조합 없는 단순 신규 상품을 등록한다(모듈 상단 CREATE_PRODUCT_PATH
        주석의 공식 스펙 근거 참고). 등록 응답이 원상품번호+채널상품번호를 모두
        동기적으로 돌려주므로, 성공 시 바로 ProductPlatformMap을 만들 수 있다
        (services.product_publish_service 참고 - 쿠팡과 다른 점)."""
        _validate_naver_publish_draft(draft_snapshot)
        credentials = self._get_credentials()
        if credentials is None:
            raise MarketplaceCredentialMissingError("naver")
        client_id, client_secret, seller_id = credentials
        access_token = self._fetch_access_token(client_id, client_secret, seller_id)

        cf = draft_snapshot["channel_fields"]
        image_urls: list[str] = draft_snapshot["image_urls"]
        images: dict[str, Any] = {"representativeImage": {"url": image_urls[0]}}
        if len(image_urls) > 1:
            images["optionalImages"] = [{"url": u} for u in image_urls[1:]]

        origin_product = {
            "statusType": "SALE",
            "leafCategoryId": draft_snapshot["category_code"],
            "name": draft_snapshot["name"],
            "images": images,
            "detailContent": draft_snapshot["description_html"],
            "salePrice": int(draft_snapshot["sale_price"]),
            "stockQuantity": int(draft_snapshot["stock_quantity"]),
            "deliveryInfo": cf["deliveryInfo"],
            "detailAttribute": {
                "afterServiceInfo": cf["afterServiceInfo"],
                "originAreaInfo": cf["originAreaInfo"],
                "minorPurchasable": bool(cf["minorPurchasable"]),
                "productInfoProvidedNotice": cf["productInfoProvidedNotice"],
            },
        }
        body = {
            "originProduct": origin_product,
            "smartstoreChannelProduct": {
                "channelProductDisplayStatusType": cf["channelProductDisplayStatusType"],
                "naverShoppingRegistration": bool(cf["naverShoppingRegistration"]),
            },
        }
        response = self._request_with_retry(
            "POST", CREATE_PRODUCT_PATH, headers={"Authorization": f"Bearer {access_token}"}, json=body
        )
        raise_for_status("naver", response.status_code)
        with external_call("naver"):
            payload = response.json()
            origin_product_no = payload.get("originProductNo")
            channel_product_no = payload.get("smartstoreChannelProductNo")
        if not origin_product_no or not channel_product_no:
            raise MarketplaceExternalAPIError("naver", "PARSE_FAILED", False, http_status=response.status_code)
        return ProductCreateResult(
            accepted=True,
            platform_result_code="SUCCESS",
            channel_product_id=str(origin_product_no),
            channel_option_id=str(channel_product_no),
        )

    def create_product_with_options(self, draft_snapshot: dict[str, Any]) -> ProductOptionsCreateResult:
        """하나의 로컬 상품에 속한 여러 SKU를 네이버 조합형 옵션(optionCombinations)
        하나의 원상품으로 묶어 등록한다(모듈 상단 NAVER_MAX_OPTION_COMBINATION_AXES
        주석의 공식 스펙 근거 참고). create_product()와 달리 등록 응답이 조합별
        식별자를 돌려주지 않으므로 items는 항상 seller_product_code만 채운 채
        channel_option_id=None으로 반환한다 - fetch_option_registration_status()가
        후속 조회로 확정한다."""
        _validate_naver_option_group_draft(draft_snapshot)
        credentials = self._get_credentials()
        if credentials is None:
            raise MarketplaceCredentialMissingError("naver")
        client_id, client_secret, seller_id = credentials
        access_token = self._fetch_access_token(client_id, client_secret, seller_id)

        cf = draft_snapshot["channel_fields"]
        image_urls: list[str] = draft_snapshot["image_urls"]
        images: dict[str, Any] = {"representativeImage": {"url": image_urls[0]}}
        if len(image_urls) > 1:
            images["optionalImages"] = [{"url": u} for u in image_urls[1:]]

        items: list[dict[str, Any]] = draft_snapshot["items"]
        base_price = Decimal(str(draft_snapshot["base_sale_price"]))
        axis_names = [str(a) for a, _v in (items[0].get("option_values") or [])]
        option_combination_group_names = {
            f"optionGroupName{i + 1}": axis_name for i, axis_name in enumerate(axis_names)
        }
        option_combinations = []
        total_stock = 0
        for item in items:
            option_values = item["option_values"]
            combo: dict[str, Any] = {}
            for i, (_axis, value) in enumerate(option_values):
                combo[f"optionName{i + 1}"] = str(value)
            stock_quantity = int(item["stock_quantity"])
            total_stock += stock_quantity
            item_price = Decimal(str(item["sale_price"]))
            combo["stockQuantity"] = stock_quantity
            combo["price"] = int(item_price - base_price)
            combo["sellerManagerCode"] = item["seller_product_code"]
            combo["usable"] = True
            option_combinations.append(combo)

        origin_product = {
            "statusType": "SALE",
            "leafCategoryId": draft_snapshot["category_code"],
            "name": draft_snapshot["name"],
            "images": images,
            "detailContent": draft_snapshot["description_html"],
            "salePrice": int(base_price),
            # 조합형 옵션 상품은 원상품 전체 재고가 옵션별 재고 합으로 자동 계산된다
            # (모듈 docstring _has_option_managed_stock 참고) - 그 규칙과 일치하도록
            # 미리 합산한 값을 그대로 보낸다(추측/임의값이 아니다).
            "stockQuantity": total_stock,
            "deliveryInfo": cf["deliveryInfo"],
            "detailAttribute": {
                "afterServiceInfo": cf["afterServiceInfo"],
                "originAreaInfo": cf["originAreaInfo"],
                "minorPurchasable": bool(cf["minorPurchasable"]),
                "productInfoProvidedNotice": cf["productInfoProvidedNotice"],
                "optionInfo": {
                    "useStockManagement": True,
                    "optionCombinationGroupNames": option_combination_group_names,
                    "optionCombinations": option_combinations,
                },
            },
        }
        body = {
            "originProduct": origin_product,
            "smartstoreChannelProduct": {
                "channelProductDisplayStatusType": cf["channelProductDisplayStatusType"],
                "naverShoppingRegistration": bool(cf["naverShoppingRegistration"]),
            },
        }
        response = self._request_with_retry(
            "POST", CREATE_PRODUCT_PATH, headers={"Authorization": f"Bearer {access_token}"}, json=body
        )
        raise_for_status("naver", response.status_code)
        with external_call("naver"):
            payload = response.json()
            origin_product_no = payload.get("originProductNo")
            channel_product_no = payload.get("smartstoreChannelProductNo")
        if not origin_product_no or not channel_product_no:
            raise MarketplaceExternalAPIError("naver", "PARSE_FAILED", False, http_status=response.status_code)
        return ProductOptionsCreateResult(
            accepted=True,
            platform_result_code="SUCCESS",
            channel_product_id=str(origin_product_no),
            channel_option_id=str(channel_product_no),
            items=[ProductOptionItemResult(seller_product_code=item["seller_product_code"]) for item in items],
        )

    def fetch_option_registration_status(
        self, channel_product_id: str, channel_option_id: Optional[str] = None
    ) -> ProductOptionRegistrationStatus:
        """등록된 원상품(channel_product_id=originProductNo)을 재조회해 조합별
        옵션 식별자(id)를 sellerManagerCode와 함께 돌려준다 - 등록 응답 자체에는
        조합별 식별자가 없어(create_product_with_options 모듈 주석 참고) 항상 이
        후속 조회가 필요하다."""
        credentials = self._get_credentials()
        if credentials is None:
            raise MarketplaceCredentialMissingError("naver")
        client_id, client_secret, seller_id = credentials
        access_token = self._fetch_access_token(client_id, client_secret, seller_id)
        origin_product = self._fetch_origin_product(channel_product_id, access_token)
        status_type = origin_product.get("statusType")
        option_info = ((origin_product.get("detailAttribute") or {}).get("optionInfo")) or {}
        combinations = option_info.get("optionCombinations") or []
        items = [
            ProductOptionItemResult(
                seller_product_code=combo["sellerManagerCode"],
                channel_option_id=str(combo["id"]) if combo.get("id") is not None else None,
            )
            for combo in combinations
            if combo.get("sellerManagerCode")
        ]
        return ProductOptionRegistrationStatus(status_name=str(status_type) if status_type else None, items=items)

    def update_product_info(
        self,
        platform_option_id: str,
        platform_origin_product_id: Optional[str],
        name: Optional[str] = None,
        sale_price: Optional[float] = None,
        description: Optional[str] = None,
    ) -> ProductSyncActionResult:
        """상품명/판매가/상세설명 중 주어진 값만 바꾼다. PUT이 전체교체형이라
        (모듈 상단 주석 참고) GET으로 현재 전체 값을 받아 그 세 필드만 치환하고
        나머지(이미지/배송/재고/상세속성 등)는 그대로 되돌려 보낸다 - 안전하게
        보존할 수 없는 필드는 없다(GET=PUT 동일 스키마이므로)."""
        if not platform_origin_product_id:
            raise MarketplaceCapabilityUnsupportedError("naver", "product_info_update_missing_origin_product_id")
        if name is None and sale_price is None and description is None:
            raise MarketplaceValidationError("수정할 항목(상품명/판매가/상세설명)이 하나도 없습니다.")
        credentials = self._get_credentials()
        if credentials is None:
            raise MarketplaceCredentialMissingError("naver")
        client_id, client_secret, seller_id = credentials
        access_token = self._fetch_access_token(client_id, client_secret, seller_id)

        current = self._fetch_full_product_for_update(platform_origin_product_id, access_token)
        origin_product = current["originProduct"]
        if name is not None:
            origin_product["name"] = name
        if sale_price is not None:
            origin_product["salePrice"] = int(sale_price)
        if description is not None:
            origin_product["detailContent"] = description
        body: dict[str, Any] = {
            "originProduct": origin_product,
            "smartstoreChannelProduct": current["smartstoreChannelProduct"],
        }
        if "windowChannelProduct" in current:
            body["windowChannelProduct"] = current["windowChannelProduct"]

        path = ORIGIN_PRODUCT_GET_PATH_TMPL.format(origin_product_no=platform_origin_product_id)
        response = self._request_with_retry("PUT", path, headers={"Authorization": f"Bearer {access_token}"}, json=body)
        raise_for_status("naver", response.status_code)
        with external_call("naver"):
            payload = response.json()
            origin_product_no = payload.get("originProductNo")
        accepted = bool(origin_product_no)
        return ProductSyncActionResult(
            accepted=accepted, platform_result_code="SUCCESS" if accepted else "PARSE_FAILED"
        )

    def fetch_registration_status(self, platform_product_id: str) -> dict[str, Any]:
        """네이버는 등록 응답이 원상품번호+채널상품번호를 동기적으로 모두 돌려주므로
        (create_product 참고), 이 조회는 옵션 단위 식별자 확정 목적이 아니라 화면에
        현재 판매상태(statusType - 승인대기/판매중 등)를 보여주기 위한 것이다."""
        credentials = self._get_credentials()
        if credentials is None:
            raise MarketplaceCredentialMissingError("naver")
        client_id, client_secret, seller_id = credentials
        access_token = self._fetch_access_token(client_id, client_secret, seller_id)
        current = self._fetch_full_product_for_update(platform_product_id, access_token)
        status_type = current["originProduct"].get("statusType")
        return {"status_name": str(status_type) if status_type else None, "channel_option_ids": []}

    def _fetch_full_product_for_update(self, origin_product_no: str, access_token: str) -> dict[str, Any]:
        """GET 원상품 응답 전체(originProduct + smartstoreChannelProduct +
        선택적 windowChannelProduct)를 그대로 반환한다 - PUT 전체교체 시 바꾸지
        않는 필드를 보존하기 위해 GET 응답을 그대로 재사용한다(update_product_info
        참고). _fetch_origin_product()는 originProduct 서브객체만 반환해(재고/
        판매상태 전송용) 이 목적에는 부족하다."""
        path = ORIGIN_PRODUCT_GET_PATH_TMPL.format(origin_product_no=origin_product_no)
        response = self._request_with_retry("GET", path, headers={"Authorization": f"Bearer {access_token}"})
        raise_for_status("naver", response.status_code)
        with external_call("naver"):
            payload = response.json()
        origin_product = payload.get("originProduct")
        smartstore_channel_product = payload.get("smartstoreChannelProduct")
        if not origin_product or not smartstore_channel_product:
            raise MarketplaceExternalAPIError("naver", "PARSE_FAILED", False, http_status=response.status_code)
        result: dict[str, Any] = {
            "originProduct": dict(origin_product),
            "smartstoreChannelProduct": dict(smartstore_channel_product),
        }
        window = payload.get("windowChannelProduct")
        if window is not None:
            result["windowChannelProduct"] = dict(window)
        return result

    def _put_change_status(
        self, origin_product_no: str, access_token: str, status_type: str, stock_quantity: Optional[int] = None
    ) -> ProductSyncActionResult:
        path = PRODUCT_STATUS_PATH_TMPL.format(origin_product_no=origin_product_no)
        body: dict[str, Any] = {"statusType": status_type}
        if stock_quantity is not None:
            body["stockQuantity"] = stock_quantity
        response = self._request_with_retry("PUT", path, headers={"Authorization": f"Bearer {access_token}"}, json=body)
        raise_for_status("naver", response.status_code)
        with external_call("naver"):
            payload = response.json()
            code = payload.get("code")
        return ProductSyncActionResult(accepted=(code == "SUCCESS"), platform_result_code=str(code))

    # --- 실제 API 연동 ---

    def _get_credentials(self) -> Optional[tuple[str, str, Optional[str]]]:
        """(client_id, client_secret, seller_id)를 반환한다. client_id/client_secret이
        모두 없으면 None(더미 폴백). seller_id(판매자 계정 ID)는 선택으로, 있으면 토큰
        발급 시 SELLER 타입 + account_id로 사용하고, 없으면 SELF(본인 계정)로 처리한다."""
        if self.session is None or self.platform_id is None:
            return None
        credential_service = ApiCredentialService(self.session)
        # 앞뒤 공백을 제거한다 - 화면에서 값을 붙여넣을 때 공백이 섞이면 서명/인증이 깨진다.
        client_id = (credential_service.get_decrypted("PLATFORM", self.platform_id, "client_id") or "").strip()
        client_secret = (credential_service.get_decrypted("PLATFORM", self.platform_id, "client_secret") or "").strip()
        if not client_id or not client_secret:
            return None
        seller_id = (credential_service.get_decrypted("PLATFORM", self.platform_id, "seller_id") or "").strip()
        return client_id, client_secret, seller_id or None

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
            with external_call("naver"):
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
        """네이버 커머스 API의 client_credentials 서명 규칙: bcrypt(client_id_timestamp, salt=client_secret).

        client_secret은 커머스API센터가 발급한 **전자서명 키**(bcrypt salt 형식, 보통
        "$2a$..."로 시작)여야 한다. 형식이 잘못되면 bcrypt가 "Invalid salt"를 던지는데,
        그대로 노출하면 원인을 알기 어려우므로 명확한 안내로 바꾼다.
        """
        password = f"{client_id}_{timestamp_ms}".encode("utf-8")
        try:
            hashed = bcrypt.hashpw(password, client_secret.encode("utf-8"))
        except (ValueError, TypeError) as e:
            # 전자서명 키 형식이 잘못돼 서명을 만들 수 없다 = 인증정보를 사용할 수 없음.
            # 키 값은 노출하지 않고 연결정보 누락 오류로 처리한다(사용자 조치 필요).
            raise MarketplaceCredentialMissingError("naver") from e
        return base64.urlsafe_b64encode(hashed).decode("utf-8")

    def _fetch_access_token(self, client_id: str, client_secret: str, seller_id: Optional[str] = None) -> str:
        timestamp_ms = str(int(time_module.time() * 1000))
        signature = self._sign(client_id, client_secret, timestamp_ms)
        data = {
            "client_id": client_id,
            "timestamp": timestamp_ms,
            "client_secret_sign": signature,
            "grant_type": "client_credentials",
            "type": "SELF",
        }
        # 판매자 계정 ID가 있으면 SELLER 타입으로 그 계정의 토큰을 발급받는다.
        if seller_id:
            data["type"] = "SELLER"
            data["account_id"] = seller_id
        response = self._request_with_retry("POST", TOKEN_PATH, data=data)
        # 응답 본문(원인 문자열)은 노출하지 않고 안전한 외부 API 오류로 변환한다.
        # (401/403은 AUTH_FAILED - seller_id를 SELLER로 요청했다 거부된 경우도 포함되며,
        #  세부 안내가 필요하면 설정 화면에서 seller_id 등록 여부를 확인한다.)
        raise_for_status("naver", response.status_code)
        with external_call("naver"):
            access_token = response.json().get("access_token")
        if not access_token:
            raise MarketplaceExternalAPIError("naver", "PARSE_FAILED", False, http_status=response.status_code)
        return access_token

    @staticmethod
    def _to_naver_datetime(d: date) -> str:
        """네이버 커머스 API가 요구하는 전체 ISO-8601(밀리초+KST 오프셋) 포맷으로 변환한다.

        예: date(2026, 7, 1) -> "2026-07-01T00:00:00.000+09:00"
        """
        dt = datetime.combine(d, datetime.min.time(), tzinfo=_KST)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000") + "+09:00"

    def _fetch_raw_orders_live(
        self, start_date: date, end_date: date, client_id: str, client_secret: str, seller_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """실운영 환경에서 확인된 제약: from/to는 최대 24시간 차이만 허용한다("최대 24시간
        차이로 설정해야 합니다" 오류로 확인됨). 여러 날짜에 걸친 조회는 하루(24시간) 단위로
        나눠 호출한 뒤 결과를 합친다."""
        access_token = self._fetch_access_token(client_id, client_secret, seller_id)
        raw_orders: list[dict[str, Any]] = []
        day_count = 0

        day = start_date
        while day < end_date:
            next_day = day + timedelta(days=1)
            response = self._request_with_retry(
                "GET",
                ORDER_LIST_PATH,
                headers={"Authorization": f"Bearer {access_token}"},
                params={"from": self._to_naver_datetime(day), "to": self._to_naver_datetime(next_day)},
            )
            # 비200은 응답 본문(개인정보 가능)을 노출하지 않고 안전한 외부 API 오류로 변환한다.
            raise_for_status("naver", response.status_code)
            with external_call("naver"):
                payload = response.json()
                raw_orders.extend(payload.get("data", {}).get("contents", []))
            day = next_day
            day_count += 1

        # 개인정보 보호: 응답 원문(주문자·수취인·주소·연락처 포함)을 로그나 파일로 남기지 않는다.
        # 개인정보가 없는 메타데이터(조회일수·수집 건수)만 기록한다.
        logger.info("네이버 주문 조회 완료: 조회일수=%d, 수집 상품주문(라인)=%d건", day_count, len(raw_orders))
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
            # 배송지(수취인) 정보 - 네이버는 productOrder.shippingAddress에 담긴다(주문 내 동일).
            shipping = product_orders[0].get("shippingAddress") or {}
            addr = " ".join(x for x in [shipping.get("baseAddress"), shipping.get("detailAddress")] if x) or None
            delivery_memo = product_orders[0].get("shippingMemo") or order.get("deliveryMemo")
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
                    "receiver_name": shipping.get("name"),
                    "receiver_phone": shipping.get("tel1") or shipping.get("tel2"),
                    "receiver_zipcode": shipping.get("zipCode"),
                    "receiver_address": addr,
                    "delivery_message": delivery_memo,
                    "items": [
                        {
                            # 상품주문번호(라인 단위 외부 식별자) - content.productOrder.productOrderId.
                            # 발주확인·송장·클레임의 핵심 식별자. 빈 문자열은 None으로 정규화.
                            "platform_order_item_no": (po.get("productOrderId") or None),
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

    def _fetch_raw_products_live(
        self, client_id: str, client_secret: str, seller_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """상품 검색 API를 페이지 단위로 끝까지 순회해 원본상품 목록을 모은다.

        페이지네이션 방식(page/size)과 응답 필드명은 네이버 커머스 API 공식 문서
        기준으로 작성했으나, 주문 API 때와 달리 아직 실제 계정으로 검증되지
        않았다 - 실 연동 후 오류가 나면 이 메서드와 _normalize_live_products만
        고치면 된다.
        """
        access_token = self._fetch_access_token(client_id, client_secret, seller_id)
        all_contents: list[dict[str, Any]] = []
        page = 1
        while True:
            response = self._request_with_retry(
                "POST",
                PRODUCT_SEARCH_PATH,
                headers={"Authorization": f"Bearer {access_token}"},
                json={"page": page, "size": PRODUCT_PAGE_SIZE},
            )
            # 비200은 응답 본문을 노출하지 않고 안전한 외부 API 오류로 변환한다.
            raise_for_status("naver", response.status_code)
            with external_call("naver"):
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
                origin_product_no = content.get("originProductNo")
                items.append(
                    {
                        # 옵션(채널상품) 단위 식별자 - product_platform_map.platform_option_id의
                        # 매핑 키(유니크)가 된다.
                        "platform_option_id": str(channel_product_no) if channel_product_no is not None else None,
                        "platform_product_id": str(group_no),
                        # 원상품 단위 식별자(channelProductNo와 다른 값) - 상용 ERP 확장(3단계)
                        # 재고/판매상태 변경 API가 경로 파라미터로 요구한다(모듈 상단
                        # PRODUCT_STATUS_PATH_TMPL 주석 참고). 실 응답에 이미 포함돼 있던
                        # 값을 그대로 저장한다(추측 아님).
                        "platform_origin_product_id": (
                            str(origin_product_no) if origin_product_no is not None else None
                        ),
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
