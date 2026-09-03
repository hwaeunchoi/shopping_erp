"""
services/product_publish_service.py
------------------------------------------------
신규 상품 등록(PRODUCT_CREATE) - 상용 ERP 확장(3단계, 두 번째 묶음). 옵션 조합이
없는 단순 상품만 대상이다. 옵션 조합 등록/옵션 구조 변경/대량 등록/자동 가격결정/
자동 재고배분/다른 채널 추가는 이번 범위 밖이다.

1단계 outbox/lease/UNKNOWN 패턴을 그대로 재사용한다(services.
product_sync_dispatch_service와 동일 원칙) - 다만 target_type은 "PRODUCT_PUBLISH_DRAFT"
(target_id=ProductPublishDraft.id)로 별도 네임스페이스를 쓴다. 아직 등록되지 않은
초안은 재고/판매상태/정보수정이 쓰는 "PRODUCT_PLATFORM_MAP" 대상과 겹칠 대상 자체가
없으므로(외부 식별자가 아직 없다) 형제 매핑 확장 없이 draft.id 하나만으로 동시성
잠금/재시도 안전성을 확보한다(_resolve_contention_target_ids에 해당하는 로직이
필요 없다 - 항상 draft.id 단일 대상).

기본 차단: settings.product_publish_enabled가 False(기본값)이면 enqueue_create()가
즉시 ProductPublishDisabledError를 던진다 - PENDING 명령 자체를 만들지 않으므로
product_publish_dispatch_job이 실행할 대상도 생기지 않는다.

목표값 확정(재시도 안전성): enqueue 시점에 ProductPublishDraft의 전체 상태를 JSON
스냅샷으로 얼려 ProductPublishCommandDetail에 저장한다 - 이후 초안을 편집해도
이미 대기 중인 명령이 보내는 값은 바뀌지 않는다(재시도는 "정확히 같은 요청"의
반복이어야 한다).

중복 등록 방지: 이미 이 (product_option, platform)에 등록된 ProductPlatformMap이
있으면 enqueue_create()가 즉시 차단한다(재등록 시도 자체를 막음). 같은 초안의
스냅샷 해시로 idempotency_key를 만들어 같은 값으로 다시 접수 요청이 오면(버튼
연타 등) 기존 명령을 그대로 재사용한다. 아직 실행 전(PENDING/RETRY_WAIT)인 다른
스냅샷의 명령이 있으면 낡은 값이므로 취소한다(services.product_sync_dispatch_service
의 "오래된 명령" 취소와 동일 원칙).

등록 성공 후 매핑 생성(확인된 외부 ID만 저장): 커넥터가 옵션 단위 식별자
(channel_option_id)를 즉시 돌려주면(네이버) 바로 ProductPlatformMap을 만든다.
channel_option_id가 없으면(쿠팡 - 승인 후 별도 조회 필요, integrations.malls.
coupang_connector 모듈 docstring 참고) draft.pending_platform_product_id에 상품
단위 식별자만 잠정 저장하고 매핑은 만들지 않는다(확인되지 않은 옵션 단위 식별자를
추측해 저장하지 않기 위함) - check_registration_status()/confirm_mapping()으로
운영자가 승인 확인 후 직접 확정한다(자동 확정하지 않음 - 같은 상품에 옵션이
여러 개면 어느 vendorItemId가 이 SKU에 대응하는지 시스템이 추측할 수 없다).

⚠️ 이 서비스는 채널 등록까지 포함한 "정확히 한 번" 등록을 보장하지 않는다 - 등록
응답 유실(네트워크 타임아웃 등)이나 "외부 등록은 성공했지만 로컬 저장에 실패"하는
경우는 UNKNOWN으로 보류하고 자동으로 새 등록을 반복하지 않는다(운영자가 채널을
직접 확인해 resolve_unknown_command()로 해소해야 한다 - 채널에 이미 등록된 상품이
있는데 또 등록을 재시도하면 같은 상품이 중복 등록될 위험이 있다).

⚠️ 이 서비스의 직렬화는 이 ERP가 만든 ExternalCommand끼리만 적용된다 - 채널
판매자센터 관리자 화면에서의 수동 등록이나 다른 프로그램의 직접 API 호출까지
막지는 못한다(services.product_sync_dispatch_service 모듈 docstring과 동일한
한계).
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
)
from models.integration_sync import ExternalCommand, ProductPublishCommandDetail
from models.product import ProductPlatformMap, ProductPublishDraft
from repositories.integration_sync_repository import ExternalCommandRepository, ProductPublishCommandDetailRepository
from repositories.platform_repository import PlatformRepository
from repositories.product_repository import (
    ProductOptionRepository,
    ProductPlatformMapRepository,
    ProductPublishDraftRepository,
)

logger = logging.getLogger(__name__)

ConnectorFactory = Callable[[str, Any, Optional[int]], BaseMallConnector]

TARGET_TYPE = "PRODUCT_PUBLISH_DRAFT"
PRODUCT_CREATE = "PRODUCT_CREATE"

MAX_ATTEMPTS = 5
RETRY_BACKOFF_MINUTES = [2, 5, 15, 30, 60]
STALE_RUNNING_TIMEOUT_MINUTES = 15
_SAFE_RETRY_REASON_CODES = frozenset({"RATE_LIMITED", "CONNECT_FAILED"})
_CONFIRMED_FAILED_REASON_CODES = frozenset({"AUTH_FAILED"})

# 등록 페이로드에 들어갈 이미지는 채널이 허용하는 URL만 - 로컬 파일 경로/file://는
# 절대 허용하지 않는다(모듈 docstring 및 이번 라운드 지침 참고).
ALLOWED_IMAGE_URL_SCHEMES = ("http://", "https://")


class ProductPublishDisabledError(Exception):
    """실계정 검증 승인 전이라 상품 등록 기능이 기본 비활성화(OFF) 상태다."""


class ProductPublishDraftNotFoundError(Exception):
    pass


class ProductPublishAlreadyRegisteredError(Exception):
    """이 초안이 가리키는 (product_option, platform)에 이미 등록된 매핑이 있다 -
    재등록 대신 기존 매핑의 정보수정(services.product_sync_dispatch_service.
    ProductSyncDispatchService.enqueue_info_update)을 써야 한다."""


class ProductPublishAlreadyRunningError(Exception):
    """같은 명령이 이미 RUNNING(다른 worker가 처리 중)이다."""


class ProductPublishCommandTypeMismatchError(Exception):
    pass


class ProductPublishRejectedError(MarketplaceError):
    """채널이 명시적으로 거부(ERROR)를 응답했다 - 원본 응답 전문은 담지 않는다."""

    def __init__(self, marketplace_code: str, result_code: Optional[str]) -> None:
        self.marketplace_code = marketplace_code
        self.reason_code = result_code or "REJECTED"
        super().__init__(f"{marketplace_code}: 상품 등록이 거부되었습니다(code={self.reason_code}).")


def _classify_write_outcome(exc: Exception) -> str:
    """services.product_sync_dispatch_service._classify_write_outcome과 동일 원칙."""
    if isinstance(exc, ProductPublishRejectedError):
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


def _retry_backoff(attempt_count: int) -> timedelta:
    idx = min(max(attempt_count, 1), len(RETRY_BACKOFF_MINUTES)) - 1
    return timedelta(minutes=RETRY_BACKOFF_MINUTES[idx])


def _validate_image_urls(image_urls: list[str]) -> None:
    for url in image_urls:
        if not isinstance(url, str) or not url.lower().startswith(ALLOWED_IMAGE_URL_SCHEMES):
            raise ValueError(f"허용되지 않는 이미지 경로입니다(http/https URL만 허용): {url}")


@dataclass
class ProductPublishOutcome:
    command: ExternalCommand
    already_processed: bool  # True면 idempotency로 기존 SUCCESS 결과를 재사용(신규 API 호출 없음)


class ProductPublishService:
    def __init__(self, session: Session, connector_factory: ConnectorFactory = get_mall_connector) -> None:
        self.session = session
        self.connector_factory = connector_factory
        self.draft_repo = ProductPublishDraftRepository(session)
        self.option_repo = ProductOptionRepository(session)
        self.mapping_repo = ProductPlatformMapRepository(session)
        self.platform_repo = PlatformRepository(session)
        self.command_repo = ExternalCommandRepository(session)
        self.detail_repo = ProductPublishCommandDetailRepository(session)

    # --- 초안(draft) 저장 ---

    def save_draft(
        self,
        product_option_id: int,
        platform_id: int,
        *,
        name: Optional[str] = None,
        sale_price: Optional[float] = None,
        description_html: Optional[str] = None,
        category_code: Optional[str] = None,
        image_urls: Optional[list[str]] = None,
        stock_quantity: Optional[int] = None,
        channel_fields: Optional[dict[str, Any]] = None,
    ) -> ProductPublishDraft:
        """초안을 저장한다 - 미입력 항목이 있어도 그대로 저장할 수 있다(전송 가능
        여부 검증은 enqueue_create() 시점에 커넥터가 공식 계약 기준으로 한다).
        이미지 URL만은 저장 시점에도 http(s) 검사를 한다(로컬 경로/file://가
        페이로드는 물론 초안에도 유입되지 않도록 원천 차단)."""
        if image_urls is not None:
            _validate_image_urls(image_urls)
        option = self.option_repo.get_by_id(product_option_id)
        if option is None:
            raise ProductPublishDraftNotFoundError(f"옵션을 찾을 수 없습니다: id={product_option_id}")
        platform = self.platform_repo.get_by_id(platform_id)
        if platform is None:
            raise ProductPublishDraftNotFoundError(f"플랫폼을 찾을 수 없습니다: id={platform_id}")

        draft = self.draft_repo.get_by_option_and_platform(product_option_id, platform_id)
        if draft is None:
            draft = ProductPublishDraft(product_option_id=product_option_id, platform_id=platform_id)
            self.session.add(draft)
        if name is not None:
            draft.name = name
        if sale_price is not None:
            draft.sale_price = sale_price
        if description_html is not None:
            draft.description_html = description_html
        if category_code is not None:
            draft.category_code = category_code
        if image_urls is not None:
            draft.image_urls_json = json.dumps(image_urls, ensure_ascii=False)
        if stock_quantity is not None:
            draft.stock_quantity = stock_quantity
        if channel_fields is not None:
            draft.channel_fields_json = json.dumps(channel_fields, ensure_ascii=False)
        self.session.flush()
        return draft

    @staticmethod
    def _draft_snapshot(draft: ProductPublishDraft) -> dict[str, Any]:
        return {
            "name": draft.name,
            "sale_price": float(draft.sale_price) if draft.sale_price is not None else None,
            "description_html": draft.description_html,
            "category_code": draft.category_code,
            "image_urls": json.loads(draft.image_urls_json) if draft.image_urls_json else [],
            "stock_quantity": draft.stock_quantity,
            "channel_fields": json.loads(draft.channel_fields_json) if draft.channel_fields_json else {},
        }

    # --- 접수(enqueue) ---

    def enqueue_create(self, draft_id: int) -> ProductPublishOutcome:
        if not settings.product_publish_enabled:
            raise ProductPublishDisabledError(
                "상품 등록/정보수정 기능이 비활성화(OFF) 상태입니다 - 실계정 검증 승인 후 활성화해야 합니다."
            )
        draft = self.draft_repo.get_by_id(draft_id)
        if draft is None:
            raise ProductPublishDraftNotFoundError(f"초안을 찾을 수 없습니다: id={draft_id}")
        existing_mappings = self.mapping_repo.list_by_option(draft.product_option_id)
        if any(m.platform_id == draft.platform_id for m in existing_mappings):
            raise ProductPublishAlreadyRegisteredError(
                "이미 이 채널에 등록된 매핑이 있습니다 - 재등록 대신 정보수정 기능을 사용하세요."
            )
        platform = self.platform_repo.get_by_id(draft.platform_id)
        if platform is None:
            raise ProductPublishDraftNotFoundError(f"플랫폼을 찾을 수 없습니다: id={draft.platform_id}")

        snapshot = self._draft_snapshot(draft)
        snapshot_json = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        snapshot_hash = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()[:16]
        idempotency_key = f"{PRODUCT_CREATE}:{draft.id}:{snapshot_hash}"
        existing = self.command_repo.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return ProductPublishOutcome(command=existing, already_processed=existing.status == "SUCCESS")

        # 같은 초안에 아직 실행 전(PENDING/RETRY_WAIT)인 다른 스냅샷의 등록 명령이
        # 있으면 낡은 값이므로 취소한다(RUNNING은 강제 취소하지 않음 -
        # execute_command()의 실행 직전 재확인이 최종 방어선).
        for stale in self.command_repo.list_active_for_target(PRODUCT_CREATE, TARGET_TYPE, draft.id):
            if stale.status != "RUNNING":
                stale.status = "CANCELLED"
                stale.error_code = "SUPERSEDED_BY_NEWER_REQUEST"

        command = self.command_repo.add(
            ExternalCommand(
                idempotency_key=idempotency_key,
                command_type=PRODUCT_CREATE,
                platform_id=platform.id,
                platform_code=platform.code,
                target_type=TARGET_TYPE,
                target_id=draft.id,
                status="PENDING",
                trace_id=uuid.uuid4().hex,
            )
        )
        self.detail_repo.add(
            ProductPublishCommandDetail(command_id=command.id, draft_id=draft.id, snapshot_json=snapshot_json)
        )
        return ProductPublishOutcome(command=command, already_processed=False)

    # --- 실행(execute) ---

    def execute_command(self, command_id: int) -> ProductPublishOutcome:
        """outbox worker(scheduler.jobs.product_publish_dispatch_job)가 호출한다."""
        command = self.command_repo.get_by_id(command_id)
        if command is None:
            raise ValueError(f"명령을 찾을 수 없습니다: command_id={command_id}")
        if command.command_type != PRODUCT_CREATE:
            raise ProductPublishCommandTypeMismatchError(
                f"이 서비스가 다루지 않는 명령종류입니다: command_id={command_id}, command_type={command.command_type}"
            )
        if command.status == "SUCCESS":
            return ProductPublishOutcome(command=command, already_processed=True)

        detail = self.detail_repo.get_by_command_id(command.id)
        if detail is None:
            raise ValueError(f"명령의 등록 스냅샷 정보가 없습니다: command_id={command_id}")
        draft = self.draft_repo.get_by_id(detail.draft_id)
        if draft is None:
            raise ValueError(f"초안을 찾을 수 없습니다: id={detail.draft_id}")

        # 대상 단위 배타 실행(같은 초안을 두 worker가 동시에 등록 시도하지 않도록) -
        # services.product_sync_dispatch_service.execute_command과 동일 원칙(PostgreSQL
        # advisory lock, SQLite에서는 no-op).
        self.command_repo.acquire_target_lock(command.target_type, [command.target_id])

        if self.command_repo.exists_unresolved_unknown_predecessor(
            PRODUCT_CREATE, command.target_type, command.target_id, command.id
        ):
            logger.info("선행 UNKNOWN 명령이 해소되지 않아 이번 회차는 건너뜁니다: command_id=%s", command_id)
            return ProductPublishOutcome(command=command, already_processed=False)

        lease_token = uuid.uuid4().hex
        if not self.command_repo.claim(command_id, lease_token):
            self.session.refresh(command)
            if command.status == "SUCCESS":
                return ProductPublishOutcome(command=command, already_processed=True)
            if command.status == "RUNNING":
                raise ProductPublishAlreadyRunningError(f"이미 처리 중인 요청입니다: command_id={command_id}")
            raise ValueError(f"실행 대상이 아닌 상태입니다(status={command.status}): command_id={command_id}")
        self.session.refresh(command)

        if self.command_repo.exists_newer_command_for_target(
            PRODUCT_CREATE, command.target_type, command.target_id, command.id
        ):
            transitioned = self.command_repo.try_transition(
                command.id, lease_token, status="CANCELLED", error_code="SUPERSEDED_BY_NEWER_REQUEST"
            )
            self.session.refresh(command)
            if not transitioned:
                logger.warning("lease 소유권을 잃어 CANCELLED 기록을 건너뜁니다: command_id=%s", command.id)
            return ProductPublishOutcome(command=command, already_processed=False)

        try:
            platform = self.platform_repo.get_by_id(command.platform_id)
            if platform is None:
                raise ValueError(f"플랫폼 정보를 찾을 수 없습니다: platform_id={command.platform_id}")
            connector = self.connector_factory(platform.connector_class, self.session, platform.id)
            if not getattr(connector, "supports_product_create", False):
                raise MarketplaceCapabilityUnsupportedError(platform.code, "product_create")
            snapshot = json.loads(detail.snapshot_json)
            _validate_image_urls(snapshot.get("image_urls") or [])
            result = connector.create_product(snapshot)
            if not result.accepted:
                raise ProductPublishRejectedError(platform.code, result.platform_result_code)
        except Exception as e:  # noqa: BLE001 - 분류 후 안전하게 기록하고 다시 던진다.
            self._mark_after_failure(command, lease_token, e)
            raise

        # 확인된 외부 ID만 매핑에 저장한다 - 옵션 단위 식별자가 없으면(쿠팡 승인 대기)
        # 매핑을 만들지 않고 상품 단위 식별자만 초안에 잠정 저장한다(모듈 docstring 참고).
        if result.channel_option_id:
            self.mapping_repo.add(
                ProductPlatformMap(
                    product_option_id=draft.product_option_id,
                    platform_id=draft.platform_id,
                    platform_option_id=result.channel_option_id,
                    platform_product_id=result.channel_product_id,
                    platform_origin_product_id=(
                        result.channel_product_id if platform.code == "naver_smartstore" else None
                    ),
                )
            )
            draft.registered_at = datetime.now(timezone.utc)
        elif result.channel_product_id:
            draft.pending_platform_product_id = result.channel_product_id
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
            return ProductPublishOutcome(command=command, already_processed=(command.status == "SUCCESS"))
        return ProductPublishOutcome(command=command, already_processed=False)

    def _mark_after_failure(self, command: ExternalCommand, lease_token: str, exc: Exception) -> None:
        kind = _classify_write_outcome(exc)
        error_code = getattr(exc, "capability", None) or getattr(exc, "reason_code", None) or type(exc).__name__
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
        """outbox worker가 매 실행 시작 시 먼저 호출한다 - RUNNING으로 너무 오래
        머문 명령을 UNKNOWN으로 회수한다(services.product_sync_dispatch_service.
        recover_stale_running과 동일 원칙)."""
        threshold = datetime.now(timezone.utc) - timedelta(minutes=STALE_RUNNING_TIMEOUT_MINUTES)
        stale = self.command_repo.list_stale_running(PRODUCT_CREATE, threshold)
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
        """UNKNOWN 명령을 운영자가 채널을 직접 확인한 뒤 해소한다(services.
        product_sync_dispatch_service.resolve_unknown_command과 동일한 세 가지
        해소 방식)."""
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

    # --- 심사상태 조회 / 매핑 확정(쿠팡처럼 등록 응답에 옵션단위 식별자가 없는 채널) ---

    def check_registration_status(self, draft_id: int) -> dict[str, Any]:
        """draft.pending_platform_product_id(등록 접수는 됐지만 아직 옵션단위
        식별자가 확정되지 않은 상품)의 채널 심사/승인 상태를 조회한다."""
        draft = self.draft_repo.get_by_id(draft_id)
        if draft is None:
            raise ProductPublishDraftNotFoundError(f"초안을 찾을 수 없습니다: id={draft_id}")
        if not draft.pending_platform_product_id:
            raise ValueError("등록 접수된 상품 단위 식별자가 없습니다(아직 등록 전이거나 이미 매핑 확정됨).")
        platform = self.platform_repo.get_by_id(draft.platform_id)
        if platform is None:
            raise ProductPublishDraftNotFoundError(f"플랫폼을 찾을 수 없습니다: id={draft.platform_id}")
        connector = self.connector_factory(platform.connector_class, self.session, platform.id)
        return connector.fetch_registration_status(draft.pending_platform_product_id)

    def confirm_mapping(self, draft_id: int, channel_option_id: str) -> ProductPlatformMap:
        """운영자가 check_registration_status() 결과로 직접 확인한 옵션 단위
        식별자로 매핑을 확정한다 - 이 서비스가 채널 응답에서 자동으로 하나를 골라
        추측하지 않는다(모듈 docstring 참고 - 승인 후에도 어느 vendorItemId가 이
        SKU에 대응하는지는 운영자 확인이 필요하다)."""
        draft = self.draft_repo.get_by_id(draft_id)
        if draft is None:
            raise ProductPublishDraftNotFoundError(f"초안을 찾을 수 없습니다: id={draft_id}")
        if not draft.pending_platform_product_id:
            raise ValueError("확정할 상품 단위 식별자가 없습니다.")
        mapping = self.mapping_repo.add(
            ProductPlatformMap(
                product_option_id=draft.product_option_id,
                platform_id=draft.platform_id,
                platform_option_id=channel_option_id,
                platform_product_id=draft.pending_platform_product_id,
            )
        )
        draft.registered_at = datetime.now(timezone.utc)
        draft.pending_platform_product_id = None
        self.session.flush()
        return mapping
