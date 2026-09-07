"""상용 ERP 확장(3단계, 네 번째 묶음) - 상품/옵션조합 등록과 재고·판매상태·정보수정을
채널별로 대량 접수하고, 항목별 결과를 독립적으로 확인·재처리하는 오케스트레이션
계층이다.

이 서비스는 기존 단건 서비스(services.product_publish_service.ProductPublishService,
services.product_option_publish_service.ProductOptionPublishService,
services.product_sync_dispatch_service.ProductSyncDispatchService)의 enqueue_*/
retry_failed_command()를 그대로 재사용한다 - outbox 적재·멱등키·supersede·대상
잠금·UNKNOWN 처리 등 안전장치를 이 서비스가 새로 구현하거나 우회하지 않는다.
이 서비스가 새로 하는 일은 딱 두 가지다: (1) 여러 항목을 순회하며 항목별로 독립된
결과를 모으는 것, (2) 각 단건 서비스가 아직 갖고 있지 않은 사전검증(미지원 채널,
미해소 UNKNOWN 선점, 이번 배치 내 중복)을 접수 전에 미리 알려주는 것.

트랜잭션 정책(로드맵 문서화 결함 수정):
services.shipment_dispatch_service.ShipmentDispatchService.submit_many()은 항목별로
session.begin_nested()(SAVEPOINT)를 쓰기 때문에, 실패 항목의 rollback이 그 항목의
ExternalCommand INSERT까지 지워 outbox 이력이 아예 남지 않는다(docs/
COMMERCIAL_ERP_ROADMAP.md에 문서화된 한계). 이 서비스는 그 대신 항목별로 독립된
"진짜" 커밋(self.session.commit())과 롤백(self.session.rollback())을 쓴다 -
SAVEPOINT를 전혀 쓰지 않으므로, 뒤 항목의 rollback은 이미 커밋되어 확정된 앞
항목의 outbox 이력에 물리적으로 닿지 않는다.

두 가지 예외 부류를 구분해서 처리한다.
* 검증 실패(비활성화 플래그, 대상 없음, 이미 등록됨, 정적 메시지의 ValueError) -
  이 항목만 VALIDATION_FAILED로 기록하고 배치는 계속 진행한다.
* 그 외 모든 예외(SQLAlchemy 오류 포함) - 세션이 어떤 상태인지 신뢰할 수 없으므로
  "부분 성공으로 위장"하지 않고 배치를 즉시 중단한다. 이미 커밋된 앞 항목들의
  결과는 그대로 유효하다(위 트랜잭션 정책 참고). 아직 시도하지 않은 나머지
  항목은 전부 FAILED_TO_ENQUEUE(ABORTED_DUE_TO_PRIOR_ERROR)로 명시적으로
  표시한다 - 조용히 빠뜨리지 않는다.

기능 플래그가 OFF면 해당 종류의 대량 접수는 그 어떤 리포지토리 조회도 하지 않고
즉시 전부 VALIDATION_FAILED(FEATURE_DISABLED)로 반환한다(외부 HTTP 요청은 물론
불필요한 DB 세션도 열지 않는 기존 정책 - 각 단건 서비스의 enqueue_* 모듈 docstring
참고 - 을 대량 경로에서도 유지하기 위함).

재처리(retry) 정책: 이 서비스는 FAILED 상태 명령만 재처리(단건 서비스의
retry_failed_command() 호출)한다. UNKNOWN 상태 명령은 운영자가 기존 단건 확인
화면(/sync-commands/{command_id}/resolve 등)에서 직접 세 가지 해소값 중 하나를
선택해 확인하기 전까지는 이 서비스가 절대 건드리지 않는다(대량으로 자동
재전송하면 실제로 전송됐는지 모르는 채로 다시 보내 중복 등록 위험이 생긴다 -
이번 라운드 지침의 명시적 요구사항)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config.settings import settings
from integrations.malls import get_mall_connector
from integrations.malls.base_mall_connector import BaseMallConnector
from integrations.malls.errors import MarketplaceCapabilityUnsupportedError
from models.integration_sync import ExternalCommand
from repositories.integration_sync_repository import ExternalCommandRepository
from repositories.platform_repository import PlatformRepository

# isort: off
# (아래 세 블록은 isort와 black이 서로 다른 형태를 원해 재실행할 때마다 서로
# 되돌리는 낡은 상호작용이 있어 - isort는 단일 이름 import도 괄호로 감싸려
# 하고, black은 120자 이내면 다시 한 줄로 접는다 - isort 자동정렬을 끄고
# black이 이미 안정 형태로 인정한 정렬을 그대로 고정한다.)
from services.product_option_publish_service import PRODUCT_OPTION_CREATE
from services.product_option_publish_service import TARGET_TYPE as OPTION_PUBLISH_TARGET_TYPE
from services.product_option_publish_service import (
    ProductOptionPublishAlreadyRegisteredError,
    ProductOptionPublishDisabledError,
    ProductOptionPublishDraftNotFoundError,
    ProductOptionPublishService,
)
from services.product_publish_service import PRODUCT_CREATE
from services.product_publish_service import TARGET_TYPE as PUBLISH_TARGET_TYPE
from services.product_publish_service import (
    ProductPublishAlreadyRegisteredError,
    ProductPublishDisabledError,
    ProductPublishDraftNotFoundError,
    ProductPublishService,
)
from services.product_sync_dispatch_service import INVENTORY_UPDATE, PRODUCT_INFO_UPDATE, SALE_STATUS_UPDATE
from services.product_sync_dispatch_service import TARGET_TYPE as SYNC_TARGET_TYPE
from services.product_sync_dispatch_service import (
    ProductChannelSyncDisabledError,
    ProductSyncDispatchService,
    ProductSyncMappingNotFoundError,
)

# isort: on

logger = logging.getLogger(__name__)

ConnectorFactory = Callable[[str, Any, Optional[int]], BaseMallConnector]

# --- 항목별 접수 결과 분류(이번 라운드 지침이 요구하는 최소 구분) ---
ACCEPTED = "ACCEPTED"
VALIDATION_FAILED = "VALIDATION_FAILED"
UNSUPPORTED = "UNSUPPORTED"
BLOCKED_BY_UNKNOWN = "BLOCKED_BY_UNKNOWN"
DUPLICATE_OR_SUPERSEDED = "DUPLICATE_OR_SUPERSEDED"
FAILED_TO_ENQUEUE = "FAILED_TO_ENQUEUE"

# --- 재처리(retry) 결과 분류(접수와는 다른 어휘 - 새 명령을 만드는 게 아니라
# 기존 명령의 상태를 되돌리는 동작이므로 ACCEPTED/DUPLICATE 등은 해당 없다) ---
RETRIED = "RETRIED"
RETRY_NOT_FOUND = "NOT_FOUND"
RETRY_NOT_RETRYABLE = "NOT_RETRYABLE"
RETRY_UNKNOWN_REQUIRES_RESOLUTION = "UNKNOWN_REQUIRES_RESOLUTION"
RETRY_FAILED_TO_ENQUEUE = "FAILED_TO_ENQUEUE"

_ERROR_CODE_MAX_LENGTH = 500

# 검증 실패로 취급해 배치를 계속 진행할 예외들 - 전부 이 코드베이스의 단건
# 서비스가 이미 정적·안전 문구로만 던지는 타입이다(services.product_publish_service.
# _describe_exception, api/routers/products.py의 기존 ValueError 처리와 동일 원칙).
_DISABLED_EXCEPTIONS = (ProductPublishDisabledError, ProductOptionPublishDisabledError, ProductChannelSyncDisabledError)
_NOT_FOUND_EXCEPTIONS = (
    ProductPublishDraftNotFoundError,
    ProductOptionPublishDraftNotFoundError,
    ProductSyncMappingNotFoundError,
)
_ALREADY_REGISTERED_EXCEPTIONS = (ProductPublishAlreadyRegisteredError, ProductOptionPublishAlreadyRegisteredError)
_BUSINESS_EXCEPTIONS = _DISABLED_EXCEPTIONS + _NOT_FOUND_EXCEPTIONS + _ALREADY_REGISTERED_EXCEPTIONS + (ValueError,)


def _validation_error_code(exc: Exception) -> str:
    if isinstance(exc, _DISABLED_EXCEPTIONS):
        return "FEATURE_DISABLED"
    if isinstance(exc, _NOT_FOUND_EXCEPTIONS):
        return "NOT_FOUND"
    if isinstance(exc, _ALREADY_REGISTERED_EXCEPTIONS):
        return "ALREADY_REGISTERED"
    return str(exc)[:_ERROR_CODE_MAX_LENGTH]


@dataclass
class BulkItemResult:
    target_id: int
    outcome: str
    command_id: Optional[int] = None
    error_code: Optional[str] = None


@dataclass
class BulkSubmitResult:
    items: list[BulkItemResult]
    aborted: bool = False


@dataclass
class BulkInventoryItem:
    product_platform_map_id: int
    target_quantity: int


@dataclass
class BulkSaleStatusItem:
    product_platform_map_id: int
    target_status: str


@dataclass
class BulkInfoUpdateItem:
    product_platform_map_id: int
    name: Optional[str] = None
    sale_price: Optional[float] = None
    description: Optional[str] = None


@dataclass
class RetryItemResult:
    command_id: int
    outcome: str
    error_code: Optional[str] = None


@dataclass
class BulkRetryResult:
    items: list[RetryItemResult]
    aborted: bool = False


class ProductBulkService:
    def __init__(self, session: Session, connector_factory: ConnectorFactory = get_mall_connector) -> None:
        self.session = session
        self.connector_factory = connector_factory
        self.command_repo = ExternalCommandRepository(session)
        self.platform_repo = PlatformRepository(session)
        self.publish_service = ProductPublishService(session, connector_factory=connector_factory)
        self.option_publish_service = ProductOptionPublishService(session, connector_factory=connector_factory)
        self.sync_service = ProductSyncDispatchService(session, connector_factory=connector_factory)

    # --- 대량 접수(submit) ---

    def submit_publish_drafts(self, draft_ids: list[int]) -> BulkSubmitResult:
        return self._submit_bulk(
            draft_ids,
            command_type=PRODUCT_CREATE,
            target_type=PUBLISH_TARGET_TYPE,
            capability_attr="supports_product_create",
            flag_enabled=settings.product_publish_enabled,
            get_target_id=lambda draft_id: draft_id,
            load_target=lambda draft_id: self.publish_service.draft_repo.get_by_id(draft_id),
            call_enqueue=lambda draft_id: self.publish_service.enqueue_create(draft_id),
        )

    def submit_option_publish_drafts(self, group_draft_ids: list[int]) -> BulkSubmitResult:
        return self._submit_bulk(
            group_draft_ids,
            command_type=PRODUCT_OPTION_CREATE,
            target_type=OPTION_PUBLISH_TARGET_TYPE,
            capability_attr="supports_product_option_create",
            flag_enabled=settings.product_option_publish_enabled,
            get_target_id=lambda group_draft_id: group_draft_id,
            load_target=lambda group_draft_id: self.option_publish_service.group_repo.get_by_id(group_draft_id),
            call_enqueue=lambda group_draft_id: self.option_publish_service.enqueue_create(group_draft_id),
        )

    def submit_inventory_updates(self, items: list[BulkInventoryItem]) -> BulkSubmitResult:
        return self._submit_bulk(
            items,
            command_type=INVENTORY_UPDATE,
            target_type=SYNC_TARGET_TYPE,
            capability_attr="supports_inventory_update",
            flag_enabled=settings.product_channel_sync_enabled,
            get_target_id=lambda it: it.product_platform_map_id,
            load_target=lambda mapping_id: self.sync_service.mapping_repo.get_by_id(mapping_id),
            call_enqueue=lambda it: self.sync_service.enqueue_inventory_update(
                it.product_platform_map_id, it.target_quantity
            ),
        )

    def submit_sale_status_updates(self, items: list[BulkSaleStatusItem]) -> BulkSubmitResult:
        return self._submit_bulk(
            items,
            command_type=SALE_STATUS_UPDATE,
            target_type=SYNC_TARGET_TYPE,
            capability_attr="supports_sale_status_update",
            flag_enabled=settings.product_channel_sync_enabled,
            get_target_id=lambda it: it.product_platform_map_id,
            load_target=lambda mapping_id: self.sync_service.mapping_repo.get_by_id(mapping_id),
            call_enqueue=lambda it: self.sync_service.enqueue_sale_status_update(
                it.product_platform_map_id, it.target_status
            ),
        )

    def submit_info_updates(self, items: list[BulkInfoUpdateItem]) -> BulkSubmitResult:
        return self._submit_bulk(
            items,
            command_type=PRODUCT_INFO_UPDATE,
            target_type=SYNC_TARGET_TYPE,
            capability_attr="supports_product_info_update",
            flag_enabled=settings.product_info_update_enabled,
            get_target_id=lambda it: it.product_platform_map_id,
            load_target=lambda mapping_id: self.sync_service.mapping_repo.get_by_id(mapping_id),
            call_enqueue=lambda it: self.sync_service.enqueue_info_update(
                it.product_platform_map_id, it.name, it.sale_price, it.description
            ),
        )

    def _submit_bulk(
        self,
        items: list[Any],
        *,
        command_type: str,
        target_type: str,
        capability_attr: str,
        flag_enabled: bool,
        get_target_id: Callable[[Any], int],
        load_target: Callable[[int], Optional[Any]],
        call_enqueue: Callable[[Any], Any],
    ) -> BulkSubmitResult:
        """다섯 종류(단일 SKU 등록/옵션조합 등록/재고/판매상태/정보수정) 대량
        접수가 공유하는 하나의 처리 흐름 - 클래스 docstring의 트랜잭션 정책·예외
        분류를 그대로 구현한다. get_target_id/load_target/call_enqueue만 종류별로
        다르고 나머지 안전장치(스냅샷 기반 중복 판정, 미지원/미해소 UNKNOWN
        사전검증, 커밋/롤백 경계)는 완전히 동일하다."""
        if not flag_enabled:
            return BulkSubmitResult(
                items=[
                    BulkItemResult(
                        target_id=get_target_id(it), outcome=VALIDATION_FAILED, error_code="FEATURE_DISABLED"
                    )
                    for it in items
                ]
            )

        target_ids = [get_target_id(it) for it in items]
        # 배치 시작 전 스냅샷 - 이후 call_enqueue()가 돌려준 command.id가 이미 이
        # 집합에 있었다면(=멱등키로 기존 명령을 그대로 돌려받음) 새로 생성된 게
        # 아니라는 뜻이다. 새로 생성될 때마다 아래에서 이 집합에 추가하므로,
        # 같은 배치 안에서 동일 대상이 두 번 들어와도 두 번째는 올바르게
        # DUPLICATE_OR_SUPERSEDED로 판정된다(모듈 docstring 참고).
        seen_command_ids = {c.id for c in self.command_repo.list_for_targets(command_type, target_type, target_ids)}

        results: list[BulkItemResult] = []
        aborted = False
        for it in items:
            target_id = get_target_id(it)
            if aborted:
                results.append(
                    BulkItemResult(
                        target_id=target_id, outcome=FAILED_TO_ENQUEUE, error_code="ABORTED_DUE_TO_PRIOR_ERROR"
                    )
                )
                continue
            try:
                target = load_target(target_id)
                if target is None:
                    results.append(
                        BulkItemResult(target_id=target_id, outcome=VALIDATION_FAILED, error_code="NOT_FOUND")
                    )
                    continue
                platform = self.platform_repo.get_by_id(target.platform_id)
                if platform is None:
                    results.append(
                        BulkItemResult(target_id=target_id, outcome=VALIDATION_FAILED, error_code="NOT_FOUND")
                    )
                    continue
                try:
                    connector = self.connector_factory(platform.connector_class, self.session, platform.id)
                except MarketplaceCapabilityUnsupportedError:
                    results.append(
                        BulkItemResult(target_id=target_id, outcome=UNSUPPORTED, error_code=platform.connector_class)
                    )
                    continue
                if not getattr(connector, capability_attr, False):
                    results.append(
                        BulkItemResult(target_id=target_id, outcome=UNSUPPORTED, error_code=platform.connector_class)
                    )
                    continue
                # 사용자 경험 개선용 사전검증일 뿐이다 - 최종 방어선은 여전히
                # execute_command()의 exists_unresolved_unknown_predecessor()다
                # (repositories.integration_sync_repository.
                # ExternalCommandRepository.exists_unknown_for_target 모듈
                # docstring 참고).
                if self.command_repo.exists_unknown_for_target(command_type, target_type, target_id):
                    results.append(
                        BulkItemResult(target_id=target_id, outcome=BLOCKED_BY_UNKNOWN, error_code="UNRESOLVED_UNKNOWN")
                    )
                    continue

                try:
                    outcome = call_enqueue(it)
                except IntegrityError:
                    # 다른 worker가 같은 순간 같은 멱등키로 먼저 커밋한 경우(진짜
                    # 동시성 경합) - idempotency_key는 유니크 인덱스라 두 번째
                    # INSERT가 여기서 실패한다. 배치를 중단하지 않고 딱 한 번만
                    # 다시 호출한다 - 롤백 이후에는 그 worker의 커밋이 이미
                    # 보이므로, call_enqueue() 내부의 get_by_idempotency_key()가
                    # 이번에는 기존 행을 그대로 찾아 반환하고 다시 INSERT를
                    # 시도하지 않는다(=재시도가 새 경합을 만들지 않는다).
                    self.session.rollback()
                    outcome = call_enqueue(it)
                command = outcome.command
                is_duplicate = command.id in seen_command_ids
                # 결과를 기록에 남기기 전에 먼저 커밋한다 - commit()이 실패하면
                # (아래 except Exception) 이 항목은 ACCEPTED/DUPLICATE_OR_SUPERSEDED로
                # 낙관적으로 기록되지 않고 오직 FAILED_TO_ENQUEUE 하나로만 남아야
                # 한다("부분 성공으로 위장하지 말 것").
                self.session.commit()
                if is_duplicate:
                    results.append(
                        BulkItemResult(target_id=target_id, outcome=DUPLICATE_OR_SUPERSEDED, command_id=command.id)
                    )
                else:
                    seen_command_ids.add(command.id)
                    results.append(BulkItemResult(target_id=target_id, outcome=ACCEPTED, command_id=command.id))
            except _BUSINESS_EXCEPTIONS as exc:
                self.session.rollback()
                results.append(
                    BulkItemResult(
                        target_id=target_id, outcome=VALIDATION_FAILED, error_code=_validation_error_code(exc)
                    )
                )
            except Exception:
                # 세션이 어떤 상태인지 신뢰할 수 없는 예외(SQLAlchemy 오류 포함) -
                # 클래스 docstring의 트랜잭션 정책대로 배치를 중단한다. 이미
                # 커밋된 앞 항목의 결과는 SAVEPOINT를 쓰지 않았으므로 그대로
                # 유효하다.
                self.session.rollback()
                logger.exception(
                    "대량 접수 중 예상하지 못한 오류로 배치를 중단합니다: command_type=%s, target_id=%s",
                    command_type,
                    target_id,
                )
                results.append(
                    BulkItemResult(target_id=target_id, outcome=FAILED_TO_ENQUEUE, error_code="DB_OR_INTERNAL_ERROR")
                )
                aborted = True

        return BulkSubmitResult(items=results, aborted=aborted)

    # --- 진행상태 조회 ---

    def get_commands_status(self, command_ids: list[int]) -> list[ExternalCommand]:
        return self.command_repo.list_by_ids(command_ids)

    # --- 실패 항목 선택 재처리 ---

    def retry_commands(self, command_ids: list[int], resolved_by: Optional[int] = None) -> BulkRetryResult:
        """FAILED 상태 명령만 재처리한다. UNKNOWN은 절대 이 경로로 재처리하지
        않는다(클래스 docstring 참고) - 운영자가 기존 단건 확인 화면에서 직접
        해소해야 한다."""
        results: list[RetryItemResult] = []
        aborted = False
        for command_id in command_ids:
            if aborted:
                results.append(
                    RetryItemResult(
                        command_id=command_id, outcome=RETRY_FAILED_TO_ENQUEUE, error_code="ABORTED_DUE_TO_PRIOR_ERROR"
                    )
                )
                continue
            try:
                command = self.command_repo.get_by_id(command_id)
                if command is None:
                    results.append(RetryItemResult(command_id=command_id, outcome=RETRY_NOT_FOUND))
                    continue
                if command.status == "UNKNOWN":
                    results.append(RetryItemResult(command_id=command_id, outcome=RETRY_UNKNOWN_REQUIRES_RESOLUTION))
                    continue
                if command.status != "FAILED":
                    results.append(
                        RetryItemResult(
                            command_id=command_id,
                            outcome=RETRY_NOT_RETRYABLE,
                            error_code=f"CURRENT_STATUS_{command.status}",
                        )
                    )
                    continue

                if command.command_type == PRODUCT_CREATE:
                    self.publish_service.retry_failed_command(command_id)
                elif command.command_type == PRODUCT_OPTION_CREATE:
                    self.option_publish_service.retry_failed_command(command_id)
                elif command.command_type in (INVENTORY_UPDATE, SALE_STATUS_UPDATE, PRODUCT_INFO_UPDATE):
                    self.sync_service.retry_failed_command(command_id, resolved_by=resolved_by)
                else:
                    results.append(
                        RetryItemResult(
                            command_id=command_id, outcome=RETRY_NOT_RETRYABLE, error_code="UNSUPPORTED_COMMAND_TYPE"
                        )
                    )
                    continue

                self.session.commit()
                results.append(RetryItemResult(command_id=command_id, outcome=RETRIED))
            except Exception:
                self.session.rollback()
                logger.exception("대량 재처리 중 예상하지 못한 오류로 배치를 중단합니다: command_id=%s", command_id)
                results.append(
                    RetryItemResult(
                        command_id=command_id, outcome=RETRY_FAILED_TO_ENQUEUE, error_code="DB_OR_INTERNAL_ERROR"
                    )
                )
                aborted = True

        return BulkRetryResult(items=results, aborted=aborted)
