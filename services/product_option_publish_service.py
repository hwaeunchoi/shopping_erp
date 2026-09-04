"""
services/product_option_publish_service.py
------------------------------------------------
옵션 조합 상품 신규 등록(PRODUCT_OPTION_CREATE) - 상용 ERP 확장(3단계, 세 번째
묶음). 하나의 로컬 상품(Product)에 속한 여러 SKU(ProductOption)를 하나의 채널
상품 + 옵션 목록으로 묶어 등록한다. services.product_publish_service(옵션 조합
없는 단일 SKU 등록)와 완전히 독립된 서비스다 - 대상 초안 테이블(target_type=
"PRODUCT_OPTION_PUBLISH_DRAFT", target_id=ProductPublishOptionGroupDraft.id)도
별개다. 기존 옵션 추가/삭제/구조 변경, 대량 등록, 자동 가격결정/재고배분, 다른
채널 추가는 이번 범위 밖이다.

1단계 outbox/lease/UNKNOWN 패턴과 3단계 두 번째 묶음(product_publish_service)의
설계를 그대로 재사용한다(idempotency_key, 스냅샷 확정 저장, 오래된 명령 취소,
recover_stale_running, resolve_unknown_command 등 - 각 메서드 docstring에서
"동일 원칙"으로 표기한 부분은 문자 그대로 동일한 로직이다. 코드 중복은
의도적이다 - services.product_sync_dispatch_service/product_publish_service도
서로 이 방식으로 독립돼 있다).

기본 차단: settings.product_option_publish_enabled가 False(기본값)이면
enqueue_create()가 즉시 ProductOptionPublishDisabledError를 던진다 - 위
product_publish_enabled(단일 SKU 등록)와 완전히 독립된 플래그다.

중복 등록 방지(옵션 조합 vs 단일 SKU 교차 차단): enqueue_create()는 이 초안이
가리키는 모든 SKU 각각에 대해, 이미 이 채널에 등록된 ProductPlatformMap이
있으면(단일 SKU 등록으로 이미 등록됐거나, 과거에 이 옵션조합 등록으로 매핑이
이미 확정된 경우 모두 포함) 즉시 차단한다 - 같은 상품이 두 경로로 중복
등록되지 않도록 하는 유일한 안전장치다(다른 방식의 "진행 중인 초안" 자체는
검사하지 않는다 - services.product_publish_service.enqueue_create와 동일한
보호 수준).

매핑 확정(옵션 단위 식별자는 항상 후속 조회로만 확정): 두 채널 모두 옵션조합
등록 응답에는 SKU별 식별자가 없다(integrations.malls의 각 커넥터
create_product_with_options 모듈 주석 참고) - 그래서 이 서비스는 등록 성공
시점에 ProductPlatformMap을 만들지 않는다(단일 SKU 등록과 다른 점). 대신
check_registration_status()가 채널에 재조회해, 우리가 등록 시 보낸
seller_product_code와 정확히 일치하는 품목만(배열 순서·이름 유사도 추정 금지)
자동으로 매핑을 확정한다 - 일부만 확정돼도 상품 전체를 다시 등록하지 않고,
전부 확정되기 전에는 "매핑 완료"로 표시하지 않는다(get 결과의 overall_status
참고). 모호하거나(같은 코드가 서로 다른 채널 식별자로 여러 번 나타남) 응답에
없는 품목은 매핑 미확정으로 남긴다 - confirm_item_mapping()으로 운영자가
명시적으로 지정해도, 그 값이 실제로 이 상품의 이 SKU 코드에 대응하는지 매번
서버에 다시 확인한 뒤에만 매핑을 만든다(다른 계정/상품의 옵션 ID를 추측으로
확정하지 못하도록 하는 방어).

⚠️ 이 서비스도 product_publish_service와 동일하게 "정확히 한 번" 등록을
보장하지 않는다 - 등록 응답 유실/외부 성공 후 로컬 저장 실패는 UNKNOWN으로
보류하고 자동 재등록하지 않는다(운영자가 채널을 직접 확인해
resolve_unknown_command()로 해소해야 한다).
"""

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from config.settings import settings
from integrations.malls import get_mall_connector
from integrations.malls.base_mall_connector import BaseMallConnector
from integrations.malls.errors import (
    MarketplaceCapabilityUnsupportedError,
    MarketplaceCredentialMissingError,
    MarketplaceError,
    MarketplaceExternalAPIError,
    MarketplaceValidationError,
)
from models.integration_sync import ExternalCommand, ProductOptionPublishCommandDetail
from models.product import ProductPlatformMap, ProductPublishOptionGroupDraft, ProductPublishOptionGroupItemDraft
from repositories.integration_sync_repository import (
    ExternalCommandRepository,
    ProductOptionPublishCommandDetailRepository,
)
from repositories.platform_repository import PlatformRepository
from repositories.product_repository import (
    ProductOptionRepository,
    ProductPlatformMapRepository,
    ProductPublishOptionGroupDraftRepository,
    ProductPublishOptionGroupItemDraftRepository,
    ProductRepository,
)

logger = logging.getLogger(__name__)

ConnectorFactory = Callable[[str, Any, Optional[int]], BaseMallConnector]

TARGET_TYPE = "PRODUCT_OPTION_PUBLISH_DRAFT"
PRODUCT_OPTION_CREATE = "PRODUCT_OPTION_CREATE"

MAX_ATTEMPTS = 5
RETRY_BACKOFF_MINUTES = [2, 5, 15, 30, 60]
STALE_RUNNING_TIMEOUT_MINUTES = 15
_SAFE_RETRY_REASON_CODES = frozenset({"RATE_LIMITED", "CONNECT_FAILED"})
_CONFIRMED_FAILED_REASON_CODES = frozenset({"AUTH_FAILED"})

ALLOWED_IMAGE_URL_SCHEMES = ("http://", "https://")

# 네이버 조합형 옵션 식별자(옵션조합 id)는 단일 SKU 등록의 channelProductNo와
# 같은 숫자 공간을 공유하지 않지만(서로 다른 서버 채번 체계), 우연한 값 충돌로
# ProductPlatformMap.uq_platform_option_id 제약을 잘못 침범하지 않도록 원상품
# 번호를 접두사로 붙여 항상 구분되는 문자열로 저장한다.
_NAVER_COMBO_OPTION_ID_PREFIX = "COMBO"


class ProductOptionPublishDisabledError(Exception):
    """실계정 검증 승인 전이라 옵션조합 상품 등록 기능이 기본 비활성화(OFF) 상태다."""


class ProductOptionPublishDraftNotFoundError(Exception):
    pass


class ProductOptionPublishItemNotFoundError(Exception):
    pass


class ProductOptionPublishAlreadyRegisteredError(Exception):
    """이 초안이 가리키는 SKU 중 하나 이상이 이미 이 채널에 등록(매핑 존재)돼
    있다 - 재등록 대신 정보수정 기능을 사용해야 한다."""


class ProductOptionPublishAlreadyRunningError(Exception):
    """같은 명령이 이미 RUNNING(다른 worker가 처리 중)이다."""


class ProductOptionPublishCommandTypeMismatchError(Exception):
    pass


class ProductOptionPublishRejectedError(MarketplaceError):
    """채널이 명시적으로 거부(ERROR)를 응답했다 - 원본 응답 전문은 담지 않는다."""

    def __init__(self, marketplace_code: str, result_code: Optional[str]) -> None:
        self.marketplace_code = marketplace_code
        self.reason_code = result_code or "REJECTED"
        super().__init__(f"{marketplace_code}: 옵션조합 상품 등록이 거부되었습니다(code={self.reason_code}).")


def _classify_write_outcome(exc: Exception) -> str:
    """services.product_publish_service._classify_write_outcome과 동일 원칙."""
    if isinstance(exc, ProductOptionPublishRejectedError):
        return "CONFIRMED_FAILED"
    if isinstance(exc, (MarketplaceCredentialMissingError, MarketplaceCapabilityUnsupportedError, ValueError)):
        return "CONFIRMED_FAILED"
    if isinstance(exc, MarketplaceExternalAPIError):
        if exc.reason_code in _SAFE_RETRY_REASON_CODES:
            return "SAFE_RETRY"
        if exc.reason_code in _CONFIRMED_FAILED_REASON_CODES:
            return "CONFIRMED_FAILED"
        return "UNKNOWN"
    return "UNKNOWN"


_ERROR_CODE_MAX_LENGTH = 500


def _describe_exception(exc: Exception) -> str:
    """services.product_publish_service._describe_exception과 동일 원칙(화이트리스트에
    있는 안전한 타입만 실제 메시지를 담고, 그 외는 클래스명만 남긴다)."""
    code = getattr(exc, "capability", None) or getattr(exc, "reason_code", None)
    if code:
        return str(code)[:_ERROR_CODE_MAX_LENGTH]
    if isinstance(exc, MarketplaceCredentialMissingError):
        return f"{exc.marketplace_code}: 쇼핑몰 연결정보가 없거나 사용할 수 없습니다."[:_ERROR_CODE_MAX_LENGTH]
    if isinstance(exc, MarketplaceValidationError):
        message = str(exc)
        return message[:_ERROR_CODE_MAX_LENGTH] if message else type(exc).__name__
    return f"INTERNAL_ERROR:{type(exc).__name__}"


def _retry_backoff(attempt_count: int) -> timedelta:
    idx = min(max(attempt_count, 1), len(RETRY_BACKOFF_MINUTES)) - 1
    return timedelta(minutes=RETRY_BACKOFF_MINUTES[idx])


def _validate_image_urls(image_urls: list[str]) -> None:
    for url in image_urls:
        if not isinstance(url, str) or not url.lower().startswith(ALLOWED_IMAGE_URL_SCHEMES):
            raise MarketplaceValidationError(f"허용되지 않는 이미지 경로입니다(http/https URL만 허용): {url}")


def _notice_type_from_channel_fields_json(channel_fields_json: Optional[str]) -> Optional[str]:
    """services.product_publish_service._notice_type_from_channel_fields_json과 동일."""
    if not channel_fields_json:
        return None
    try:
        channel_fields = json.loads(channel_fields_json)
    except (ValueError, TypeError):
        return None
    notice = channel_fields.get("productInfoProvidedNotice") if isinstance(channel_fields, dict) else None
    return notice.get("productInfoProvidedNoticeType") if isinstance(notice, dict) else None


@dataclass
class ProductOptionPublishOutcome:
    command: ExternalCommand
    already_processed: bool  # True면 idempotency로 기존 SUCCESS 결과를 재사용(신규 API 호출 없음)


class ProductOptionPublishService:
    def __init__(self, session: Session, connector_factory: ConnectorFactory = get_mall_connector) -> None:
        self.session = session
        self.connector_factory = connector_factory
        self.product_repo = ProductRepository(session)
        self.option_repo = ProductOptionRepository(session)
        self.mapping_repo = ProductPlatformMapRepository(session)
        self.platform_repo = PlatformRepository(session)
        self.group_repo = ProductPublishOptionGroupDraftRepository(session)
        self.item_repo = ProductPublishOptionGroupItemDraftRepository(session)
        self.command_repo = ExternalCommandRepository(session)
        self.detail_repo = ProductOptionPublishCommandDetailRepository(session)

    # --- 초안(draft) 저장 ---

    def save_group_draft(
        self,
        product_id: int,
        platform_id: int,
        *,
        name: Optional[str] = None,
        description_html: Optional[str] = None,
        category_code: Optional[str] = None,
        image_urls: Optional[list[str]] = None,
        base_sale_price: Optional[float] = None,
        channel_fields: Optional[dict[str, Any]] = None,
    ) -> ProductPublishOptionGroupDraft:
        """상품 레벨(공통) 초안 값을 저장한다 - 미입력 항목이 있어도 자유롭게
        저장할 수 있다(전송 가능 여부 검증은 enqueue_create() 시점에 커넥터가
        공식 계약 기준으로 한다)."""
        if image_urls is not None:
            _validate_image_urls(image_urls)
        product = self.product_repo.get_by_id(product_id)
        if product is None:
            raise ProductOptionPublishDraftNotFoundError(f"상품을 찾을 수 없습니다: id={product_id}")
        platform = self.platform_repo.get_by_id(platform_id)
        if platform is None:
            raise ProductOptionPublishDraftNotFoundError(f"플랫폼을 찾을 수 없습니다: id={platform_id}")

        draft = self.group_repo.get_by_product_and_platform(product_id, platform_id)
        if draft is None:
            draft = ProductPublishOptionGroupDraft(product_id=product_id, platform_id=platform_id)
            self.session.add(draft)
        if name is not None:
            draft.name = name
        if description_html is not None:
            draft.description_html = description_html
        if category_code is not None:
            draft.category_code = category_code
        if image_urls is not None:
            draft.image_urls_json = json.dumps(image_urls, ensure_ascii=False)
        if base_sale_price is not None:
            draft.base_sale_price = base_sale_price
        if channel_fields is not None:
            draft.channel_fields_json = json.dumps(channel_fields, ensure_ascii=False)
        # 확인 대상 카테고리·고시유형이 바뀌면 네이버 ETC 확인을 무효화한다 -
        # services.product_publish_service.save_draft와 완전히 동일한 원칙.
        if draft.etc_notice_confirmed_at is not None:
            current_notice_type = _notice_type_from_channel_fields_json(draft.channel_fields_json)
            if (
                draft.etc_notice_confirmed_category_code != draft.category_code
                or draft.etc_notice_confirmed_notice_type != current_notice_type
            ):
                draft.etc_notice_confirmed_by = None
                draft.etc_notice_confirmed_at = None
                draft.etc_notice_confirmed_category_code = None
                draft.etc_notice_confirmed_notice_type = None
        self.session.flush()
        return draft

    def confirm_etc_notice(self, group_draft_id: int, confirmed_by: int) -> ProductPublishOptionGroupDraft:
        """services.product_publish_service.confirm_etc_notice와 완전히 동일한 원칙."""
        draft = self.group_repo.get_by_id(group_draft_id)
        if draft is None:
            raise ProductOptionPublishDraftNotFoundError(f"초안을 찾을 수 없습니다: id={group_draft_id}")
        if not draft.category_code:
            raise ValueError("카테고리 코드가 없습니다 - 먼저 카테고리 코드를 입력하고 저장하세요.")
        notice_type = _notice_type_from_channel_fields_json(draft.channel_fields_json)
        if notice_type != "ETC":
            raise ValueError(
                "현재 상품정보제공고시 유형이 ETC가 아니라서 이 확인이 적용되지 않습니다"
                f"(channel_fields.productInfoProvidedNotice.productInfoProvidedNoticeType={notice_type!r})."
            )
        draft.etc_notice_confirmed_by = confirmed_by
        draft.etc_notice_confirmed_at = datetime.now(timezone.utc)
        draft.etc_notice_confirmed_category_code = draft.category_code
        draft.etc_notice_confirmed_notice_type = notice_type
        self.session.flush()
        return draft

    def save_item(
        self,
        group_draft_id: int,
        product_option_id: int,
        *,
        option_values: Optional[list[list[str]]] = None,
        seller_product_code: Optional[str] = None,
        sale_price: Optional[float] = None,
        stock_quantity: Optional[int] = None,
    ) -> ProductPublishOptionGroupItemDraft:
        """초안에 SKU 1개(품목)를 추가/수정한다. option_values는 순서가 있는
        [[축이름, 값], ...] 목록이다(models.product.
        ProductPublishOptionGroupItemDraft 모듈 docstring 참고)."""
        draft = self.group_repo.get_by_id(group_draft_id)
        if draft is None:
            raise ProductOptionPublishDraftNotFoundError(f"초안을 찾을 수 없습니다: id={group_draft_id}")
        option = self.option_repo.get_by_id(product_option_id)
        if option is None:
            raise ProductOptionPublishDraftNotFoundError(f"옵션을 찾을 수 없습니다: id={product_option_id}")
        if option.product_id != draft.product_id:
            raise ValueError(
                f"이 SKU(product_option_id={product_option_id})는 이 초안의 상품"
                f"(product_id={draft.product_id})에 속하지 않습니다."
            )

        item = self.item_repo.get_by_group_and_option(group_draft_id, product_option_id)
        is_new = item is None
        if item is None:
            item = ProductPublishOptionGroupItemDraft(
                group_draft_id=group_draft_id, product_option_id=product_option_id
            )
            self.session.add(item)
        if option_values is not None:
            item.option_values_json = json.dumps(option_values, ensure_ascii=False)
        if seller_product_code is not None:
            item.seller_product_code = seller_product_code
        elif is_new:
            # 판매자 관리코드를 입력하지 않으면 이 SKU의 자체 채번 코드를 그대로
            # 쓴다(전역 유니크 - 채널 등록 후 되찾는 유일한 근거, 모듈 docstring 참고).
            item.seller_product_code = option.sku_code
        if sale_price is not None:
            item.sale_price = sale_price
        if stock_quantity is not None:
            item.stock_quantity = stock_quantity
        self.session.flush()
        return item

    def delete_item(self, group_draft_id: int, item_id: int) -> None:
        item = self.item_repo.get_by_id(item_id)
        if item is None or item.group_draft_id != group_draft_id:
            raise ProductOptionPublishItemNotFoundError(f"품목을 찾을 수 없습니다: id={item_id}")
        self.item_repo.delete(item)

    @staticmethod
    def _item_snapshot(item: ProductPublishOptionGroupItemDraft) -> dict[str, Any]:
        return {
            "product_option_id": item.product_option_id,
            "option_values": json.loads(item.option_values_json) if item.option_values_json else [],
            "seller_product_code": item.seller_product_code,
            "sale_price": float(item.sale_price) if item.sale_price is not None else None,
            "stock_quantity": item.stock_quantity,
        }

    @classmethod
    def _group_snapshot(
        cls, draft: ProductPublishOptionGroupDraft, items: list[ProductPublishOptionGroupItemDraft]
    ) -> dict[str, Any]:
        channel_fields = json.loads(draft.channel_fields_json) if draft.channel_fields_json else {}
        notice = channel_fields.get("productInfoProvidedNotice") if isinstance(channel_fields, dict) else None
        if isinstance(notice, dict) and notice.get("productInfoProvidedNoticeType") == "ETC":
            # services.product_publish_service._draft_snapshot과 동일 원칙 - 원시
            # JSON 플래그를 신뢰하지 않고 감사 가능한 확인 기록으로 다시 계산한다.
            notice["categoryNoticeTypeConfirmedByOperator"] = (
                draft.etc_notice_confirmed_at is not None
                and draft.etc_notice_confirmed_category_code == draft.category_code
                and draft.etc_notice_confirmed_notice_type == "ETC"
            )
        return {
            "name": draft.name,
            "description_html": draft.description_html,
            "category_code": draft.category_code,
            "image_urls": json.loads(draft.image_urls_json) if draft.image_urls_json else [],
            "base_sale_price": float(draft.base_sale_price) if draft.base_sale_price is not None else None,
            "channel_fields": channel_fields,
            "items": [cls._item_snapshot(i) for i in items],
        }

    # --- 접수(enqueue) ---

    def enqueue_create(self, group_draft_id: int) -> ProductOptionPublishOutcome:
        if not settings.product_option_publish_enabled:
            raise ProductOptionPublishDisabledError(
                "옵션조합 상품 등록 기능이 비활성화(OFF) 상태입니다 - 실계정 검증 승인 후 활성화해야 합니다."
            )
        draft = self.group_repo.get_by_id(group_draft_id)
        if draft is None:
            raise ProductOptionPublishDraftNotFoundError(f"초안을 찾을 수 없습니다: id={group_draft_id}")
        items = self.item_repo.list_by_group(group_draft_id)
        if not items:
            raise ValueError("등록할 SKU가 1개 이상 필요합니다 - 먼저 품목을 추가하세요.")

        # 단일 SKU 등록/과거 옵션조합 등록으로 이미 매핑된 SKU가 하나라도 있으면
        # 차단한다(모듈 docstring "중복 등록 방지" 참고 - 두 경로의 유일한
        # 교차 안전장치).
        for item in items:
            existing_mappings = self.mapping_repo.list_by_option(item.product_option_id)
            if any(m.platform_id == draft.platform_id for m in existing_mappings):
                raise ProductOptionPublishAlreadyRegisteredError(
                    f"SKU(product_option_id={item.product_option_id})는 이미 이 채널에 등록된 매핑이 있습니다 - "
                    "재등록 대신 정보수정 기능을 사용하세요."
                )

        platform = self.platform_repo.get_by_id(draft.platform_id)
        if platform is None:
            raise ProductOptionPublishDraftNotFoundError(f"플랫폼을 찾을 수 없습니다: id={draft.platform_id}")

        snapshot = self._group_snapshot(draft, items)
        snapshot_json = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        snapshot_hash = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()[:16]
        idempotency_key = f"{PRODUCT_OPTION_CREATE}:{draft.id}:{snapshot_hash}"
        existing = self.command_repo.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return ProductOptionPublishOutcome(command=existing, already_processed=existing.status == "SUCCESS")

        # 같은 초안에 아직 실행 전(PENDING/RETRY_WAIT)인 다른 스냅샷의 등록 명령이
        # 있으면 낡은 값이므로 취소한다(services.product_publish_service.enqueue_create와
        # 동일 원칙).
        for stale in self.command_repo.list_active_for_target(PRODUCT_OPTION_CREATE, TARGET_TYPE, draft.id):
            if stale.status != "RUNNING":
                stale.status = "CANCELLED"
                stale.error_code = "SUPERSEDED_BY_NEWER_REQUEST"

        command = self.command_repo.add(
            ExternalCommand(
                idempotency_key=idempotency_key,
                command_type=PRODUCT_OPTION_CREATE,
                platform_id=platform.id,
                platform_code=platform.code,
                target_type=TARGET_TYPE,
                target_id=draft.id,
                status="PENDING",
                trace_id=uuid.uuid4().hex,
            )
        )
        self.detail_repo.add(
            ProductOptionPublishCommandDetail(
                command_id=command.id, group_draft_id=draft.id, snapshot_json=snapshot_json
            )
        )
        return ProductOptionPublishOutcome(command=command, already_processed=False)

    # --- 실행(execute) ---

    def execute_command(self, command_id: int) -> ProductOptionPublishOutcome:
        """outbox worker(scheduler.jobs.product_option_publish_dispatch_job)가 호출한다."""
        command = self.command_repo.get_by_id(command_id)
        if command is None:
            raise ValueError(f"명령을 찾을 수 없습니다: command_id={command_id}")
        if command.command_type != PRODUCT_OPTION_CREATE:
            raise ProductOptionPublishCommandTypeMismatchError(
                f"이 서비스가 다루지 않는 명령종류입니다: command_id={command_id}, command_type={command.command_type}"
            )
        if command.status == "SUCCESS":
            return ProductOptionPublishOutcome(command=command, already_processed=True)

        detail = self.detail_repo.get_by_command_id(command.id)
        if detail is None:
            raise ValueError(f"명령의 등록 스냅샷 정보가 없습니다: command_id={command_id}")
        draft = self.group_repo.get_by_id(detail.group_draft_id)
        if draft is None:
            raise ValueError(f"초안을 찾을 수 없습니다: id={detail.group_draft_id}")

        self.command_repo.acquire_target_lock(command.target_type, [command.target_id])

        if self.command_repo.exists_unresolved_unknown_predecessor(
            PRODUCT_OPTION_CREATE, command.target_type, command.target_id, command.id
        ):
            logger.info("선행 UNKNOWN 명령이 해소되지 않아 이번 회차는 건너뜁니다: command_id=%s", command_id)
            return ProductOptionPublishOutcome(command=command, already_processed=False)

        lease_token = uuid.uuid4().hex
        if not self.command_repo.claim(command_id, lease_token):
            self.session.refresh(command)
            if command.status == "SUCCESS":
                return ProductOptionPublishOutcome(command=command, already_processed=True)
            if command.status == "RUNNING":
                raise ProductOptionPublishAlreadyRunningError(f"이미 처리 중인 요청입니다: command_id={command_id}")
            raise ValueError(f"실행 대상이 아닌 상태입니다(status={command.status}): command_id={command_id}")
        self.session.refresh(command)

        if self.command_repo.exists_newer_command_for_target(
            PRODUCT_OPTION_CREATE, command.target_type, command.target_id, command.id
        ):
            transitioned = self.command_repo.try_transition(
                command.id, lease_token, status="CANCELLED", error_code="SUPERSEDED_BY_NEWER_REQUEST"
            )
            self.session.refresh(command)
            if not transitioned:
                logger.warning("lease 소유권을 잃어 CANCELLED 기록을 건너뜁니다: command_id=%s", command.id)
            return ProductOptionPublishOutcome(command=command, already_processed=False)

        try:
            platform = self.platform_repo.get_by_id(command.platform_id)
            if platform is None:
                raise MarketplaceValidationError(f"플랫폼 정보를 찾을 수 없습니다: platform_id={command.platform_id}")
            connector = self.connector_factory(platform.connector_class, self.session, platform.id)
            if not getattr(connector, "supports_product_option_create", False):
                raise MarketplaceCapabilityUnsupportedError(platform.code, "product_option_create")
            snapshot = json.loads(detail.snapshot_json)
            _validate_image_urls(snapshot.get("image_urls") or [])
            result = connector.create_product_with_options(snapshot)
            if not result.accepted:
                raise ProductOptionPublishRejectedError(platform.code, result.platform_result_code)
        except Exception as e:  # noqa: BLE001 - 분류 후 안전하게 기록하고 다시 던진다.
            self._mark_after_failure(command, lease_token, e)
            raise

        # 상품 전체가 공유하는 식별자만 여기서 확정한다 - 옵션(SKU) 단위 매핑은
        # 두 채널 모두 등록 응답에 없어(모듈 docstring 참고) 만들지 않는다.
        # check_registration_status()가 후속 조회로 개별 확정한다.
        draft.channel_product_id = result.channel_product_id
        draft.channel_option_id = result.channel_option_id
        draft.registered_at = datetime.now(timezone.utc)
        # 커넥터가 예외적으로 등록 시점에 이미 품목별 식별자를 돌려준 경우(현재
        # 구현들은 항상 None이지만, 인터페이스 계약상 가능성을 열어둔다)를 위해
        # 방어적으로 처리한다 - 우리가 보낸 seller_product_code와 일치하는
        # 품목에 한해서만, 아직 매핑이 없는 경우에만 만든다.
        if result.items:
            platform_code = platform.code
            items_by_code = {i.seller_product_code: i for i in self.item_repo.list_by_group(draft.id)}
            for item_result in result.items:
                if not item_result.channel_option_id:
                    continue
                item = items_by_code.get(item_result.seller_product_code)
                if item is None:
                    continue
                if any(
                    m.platform_id == draft.platform_id for m in self.mapping_repo.list_by_option(item.product_option_id)
                ):
                    continue
                self._create_mapping(draft, item, platform_code, item_result.channel_option_id)
        self.session.flush()

        transitioned = self.command_repo.try_transition(
            command.id,
            lease_token,
            status="SUCCESS",
            response_summary="채널 등록 접수 완료",
            completed_at=datetime.now(timezone.utc),
        )
        self.session.refresh(command)
        if not transitioned:
            logger.warning("lease 소유권을 잃어 SUCCESS 기록을 건너뜁니다: command_id=%s", command.id)
            return ProductOptionPublishOutcome(command=command, already_processed=(command.status == "SUCCESS"))
        return ProductOptionPublishOutcome(command=command, already_processed=False)

    def _mark_after_failure(self, command: ExternalCommand, lease_token: str, exc: Exception) -> None:
        kind = _classify_write_outcome(exc)
        error_code = _describe_exception(exc)
        now = datetime.now(timezone.utc)
        if kind == "SAFE_RETRY" and command.attempt_count < MAX_ATTEMPTS:
            values: dict[str, Any] = {
                "status": "RETRY_WAIT",
                "retryable": True,
                "error_code": error_code,
                "next_retry_at": now + _retry_backoff(command.attempt_count),
            }
        elif kind == "UNKNOWN":
            values = {"status": "UNKNOWN", "retryable": False, "error_code": error_code}
        else:
            values = {"status": "FAILED", "retryable": False, "error_code": error_code}
        transitioned = self.command_repo.try_transition(command.id, lease_token, **values)
        self.session.refresh(command)
        if not transitioned:
            logger.warning("lease 소유권을 잃어 실패 기록을 건너뜁니다: command_id=%s", command.id)

    def recover_stale_running(self) -> int:
        """outbox worker가 매 실행 시작 시 먼저 호출한다 - services.
        product_publish_service.recover_stale_running과 동일 원칙."""
        threshold = datetime.now(timezone.utc) - timedelta(minutes=STALE_RUNNING_TIMEOUT_MINUTES)
        stale = self.command_repo.list_stale_running(PRODUCT_OPTION_CREATE, threshold)
        for command in stale:
            command.status = "UNKNOWN"
            command.error_code = "STALE_RUNNING_TIMEOUT"
            command.lease_token = None
        if stale:
            self.session.flush()
        return len(stale)

    def resolve_unknown_command(
        self, command_id: int, resolution: str, resolved_by: Optional[int] = None
    ) -> ExternalCommand:
        """services.product_publish_service.resolve_unknown_command과 동일한 세 가지
        해소 방식."""
        if resolution not in ("CONFIRMED_NOT_SENT", "CONFIRMED_SUCCESS", "CONFIRMED_FAILED"):
            raise ValueError(f"알 수 없는 해소 방식입니다: {resolution}")
        command = self.command_repo.get_by_id(command_id)
        if command is None:
            raise ValueError(f"명령을 찾을 수 없습니다: command_id={command_id}")
        if command.status != "UNKNOWN":
            raise ValueError(f"결과 확인이 필요한(UNKNOWN) 명령만 해소할 수 있습니다(현재 상태: {command.status}).")
        if resolution == "CONFIRMED_NOT_SENT":
            command.status = "PENDING"
            command.next_retry_at = None
            command.error_code = None
        elif resolution == "CONFIRMED_FAILED":
            command.status = "FAILED"
            command.retryable = False
        else:  # CONFIRMED_SUCCESS
            command.status = "SUCCESS"
            command.completed_at = datetime.now(timezone.utc)
            command.response_summary = "운영자가 채널에서 처리 완료를 확인함(수동 해소)"
        self.session.flush()
        return command

    # --- 품목별 옵션 단위 식별자 확정 ---

    def _create_mapping(
        self,
        draft: ProductPublishOptionGroupDraft,
        item: ProductPublishOptionGroupItemDraft,
        platform_code: str,
        channel_option_id: str,
    ) -> ProductPlatformMap:
        if platform_code == "naver_smartstore":
            # 조합형 옵션 id는 채널상품(channelProductNo) 하나에 여러 개가 속해
            # 그 자체로는 전역 유니크를 보장하지 않는다(다른 원상품의 옵션 id와
            # 우연히 같은 값이 나올 수 있다) - 원상품번호를 접두사로 붙여 항상
            # 구분되는 문자열로 저장한다(models.product.
            # ProductPublishOptionGroupDraft 모듈 docstring 참고).
            platform_option_id = f"{_NAVER_COMBO_OPTION_ID_PREFIX}-{draft.channel_product_id}-{channel_option_id}"
            platform_product_id = draft.channel_option_id  # channelProductNo(공유, 비유니크)
            platform_origin_product_id = draft.channel_product_id  # originProductNo
        else:
            platform_option_id = channel_option_id  # 쿠팡 vendorItemId(그 자체로 유니크)
            platform_product_id = draft.channel_product_id  # sellerProductId(공유, 비유니크)
            platform_origin_product_id = None
        return self.mapping_repo.add(
            ProductPlatformMap(
                product_option_id=item.product_option_id,
                platform_id=draft.platform_id,
                platform_option_id=platform_option_id,
                platform_product_id=platform_product_id,
                platform_origin_product_id=platform_origin_product_id,
                seller_product_code=item.seller_product_code,
            )
        )

    def check_registration_status(self, group_draft_id: int) -> dict[str, Any]:
        """채널에 재조회해 품목별 옵션 단위 식별자를 확인하고, 우리가 등록 시
        보낸 seller_product_code와 정확히 일치하는(그리고 모호하지 않은) 품목만
        자동으로 ProductPlatformMap을 확정한다(모듈 docstring 참고)."""
        draft = self.group_repo.get_by_id(group_draft_id)
        if draft is None:
            raise ProductOptionPublishDraftNotFoundError(f"초안을 찾을 수 없습니다: id={group_draft_id}")
        if not draft.channel_product_id:
            raise ValueError("등록 접수된 상품 단위 식별자가 없습니다(아직 등록 전입니다).")
        platform = self.platform_repo.get_by_id(draft.platform_id)
        if platform is None:
            raise ProductOptionPublishDraftNotFoundError(f"플랫폼을 찾을 수 없습니다: id={draft.platform_id}")
        connector = self.connector_factory(platform.connector_class, self.session, platform.id)
        status = connector.fetch_option_registration_status(draft.channel_product_id, draft.channel_option_id)

        items = self.item_repo.list_by_group(group_draft_id)
        # 이미 매핑된 SKU는 그 매핑에 실제로 저장된 식별자를 그대로 되돌려준다 -
        # 예전 호출(다른 화면 새로고침 등)에서 이미 확정된 SKU라도, 이번 호출의
        # 채널 응답에 그 코드가 다시 나타나지 않으면(승인 후 목록에서 빠지는 등)
        # channel_option_id를 비워 보여주면 안 되므로(실제 클릭 검증으로 발견된
        # 결함 - "매핑 완료"인데 식별자가 빈칸으로 보임), 매핑 존재 자체를 근거로
        # 삼는다.
        naver_prefix = f"{_NAVER_COMBO_OPTION_ID_PREFIX}-{draft.channel_product_id}-"
        existing_mapping_option_id: dict[int, str] = {}
        for item in items:
            existing = next(
                (
                    m
                    for m in self.mapping_repo.list_by_option(item.product_option_id)
                    if m.platform_id == draft.platform_id
                ),
                None,
            )
            if existing is not None:
                raw = existing.platform_option_id
                existing_mapping_option_id[item.product_option_id] = (
                    raw[len(naver_prefix) :] if raw.startswith(naver_prefix) else raw
                )

        # seller_product_code별로 채널이 돌려준 서로 다른 channel_option_id를
        # 모아 모호성을 판정한다 - 같은 코드가 두 개 이상의 서로 다른 값으로
        # 나타나면(채널 쪽 이상 데이터) 모호로 보류한다(배열 순서/유사도 추정 금지).
        candidates_by_code: dict[str, set[str]] = {}
        for candidate in status.items:
            if candidate.channel_option_id:
                candidates_by_code.setdefault(candidate.seller_product_code, set()).add(candidate.channel_option_id)

        result_items: list[dict[str, Any]] = []
        for item in items:
            code = item.seller_product_code
            mapped = item.product_option_id in existing_mapping_option_id
            resolved_id: Optional[str] = existing_mapping_option_id.get(item.product_option_id)
            ambiguous = False
            if not mapped and code:
                candidate_ids = candidates_by_code.get(code, set())
                if len(candidate_ids) == 1:
                    resolved_id = next(iter(candidate_ids))
                    self._create_mapping(draft, item, platform.code, resolved_id)
                    mapped = True
                elif len(candidate_ids) > 1:
                    ambiguous = True
            result_items.append(
                {
                    "product_option_id": item.product_option_id,
                    "seller_product_code": code,
                    "mapped": mapped,
                    "channel_option_id": resolved_id,
                    "ambiguous": ambiguous,
                }
            )
        mapped_count = sum(1 for r in result_items if r["mapped"])
        if mapped_count == 0:
            overall_status = "PENDING_REVIEW"
        elif mapped_count < len(result_items):
            overall_status = "PARTIALLY_MAPPED"
        else:
            overall_status = "FULLY_MAPPED"

        return {"channel_status_name": status.status_name, "overall_status": overall_status, "items": result_items}

    def confirm_item_mapping(
        self, group_draft_id: int, product_option_id: int, channel_option_id: str
    ) -> ProductPlatformMap:
        """운영자가 지정한 옵션 단위 식별자로 이 SKU 하나의 매핑을 확정한다 -
        운영자가 입력한 값을 그대로 믿지 않고, 이 자리에서 채널에 다시 조회해
        그 값이 실제로 이 SKU의 seller_product_code에 대응하는 후보인지
        서버에서 검증한다(다른 계정/상품의 옵션 ID로 확정하지 못하도록 하는
        방어 - services.product_publish_service.confirm_mapping과 동일 원칙,
        다만 이쪽은 품목이 여럿이라 seller_product_code까지 함께 검증한다)."""
        draft = self.group_repo.get_by_id(group_draft_id)
        if draft is None:
            raise ProductOptionPublishDraftNotFoundError(f"초안을 찾을 수 없습니다: id={group_draft_id}")
        if not draft.channel_product_id:
            raise ValueError("등록 접수된 상품 단위 식별자가 없습니다.")
        item = self.item_repo.get_by_group_and_option(group_draft_id, product_option_id)
        if item is None:
            raise ProductOptionPublishItemNotFoundError(
                f"품목을 찾을 수 없습니다: product_option_id={product_option_id}"
            )
        if any(m.platform_id == draft.platform_id for m in self.mapping_repo.list_by_option(product_option_id)):
            raise ValueError("이미 매핑이 존재하는 SKU입니다.")
        platform = self.platform_repo.get_by_id(draft.platform_id)
        if platform is None:
            raise ProductOptionPublishDraftNotFoundError(f"플랫폼을 찾을 수 없습니다: id={draft.platform_id}")
        connector = self.connector_factory(platform.connector_class, self.session, platform.id)
        status = connector.fetch_option_registration_status(draft.channel_product_id, draft.channel_option_id)
        matches = [c for c in status.items if c.seller_product_code == item.seller_product_code and c.channel_option_id]
        candidate_ids: set[str] = {c.channel_option_id for c in matches if c.channel_option_id}
        if channel_option_id not in candidate_ids:
            raise ValueError(
                "이 식별자는 채널이 이 SKU(판매자 관리코드="
                f"{item.seller_product_code})에 대해 실제로 확인해 준 후보 목록에 없습니다 - "
                "다시 조회한 확정 목록: " + (", ".join(sorted(candidate_ids)) if candidate_ids else "(없음)")
            )
        mapping = self._create_mapping(draft, item, platform.code, channel_option_id)
        self.session.flush()
        return mapping
