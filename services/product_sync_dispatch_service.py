"""
services/product_sync_dispatch_service.py
------------------------------------------------
기존 채널 상품(옵션)의 재고 수량/판매상태를 실제 채널(네이버/쿠팡)에 전송한다.
상용 ERP 확장(3단계, 첫 묶음) - 신규 상품 등록/전체 상품정보 수정은 범위 밖이다.

1단계(services.shipment_dispatch_service)의 outbox/lease/UNKNOWN 패턴을 그대로
재사용한다 - ExternalCommand/ExternalCommandRepository는 이미 command_type으로
일반화돼 있어 모델/저장소 변경 없이 새 command_type(INVENTORY_UPDATE/
SALE_STATUS_UPDATE)만 추가하면 된다.

기본 차단: settings.product_channel_sync_enabled가 False(기본값)이면
enqueue_*()가 즉시 ProductChannelSyncDisabledError를 던진다 - PENDING 명령 자체를
만들지 않으므로 product_sync_dispatch_job이 실행할 대상도 생기지 않는다(기존
주문수집/네이버 상품수집/송장 전송/클레임 수집은 이 플래그와 무관하게 그대로
동작한다).

목표값 확정(재시도 안전성): enqueue 시점에 검증을 마친 target_quantity/
target_sale_status를 ProductSyncCommandDetail에 확정 저장한다. execute_command()는
이 저장된 값만 읽어 전송한다 - 화면에서 다시 읽은 최신 입력값으로 재시도 중
바뀌지 않는다.

중복/동시 요청 방지(idempotency): 같은 대상(product_platform_map_id)에 같은
목표값을 다시 요청하면(버튼 연타 등) 결정론적 idempotency_key로 기존 명령을
그대로 재사용한다(f"{command_type}:{mapping_id}:{target}"). 이미 종료(SUCCESS/
FAILED/UNKNOWN/CANCELLED)된 명령에 같은 값으로 다시 요청하면 그 기존 명령을
그대로 반환한다(자동 재시도는 하지 않음 - shipment_dispatch_service의 기존
idempotency_key 설계와 동일한 원칙: 새로 시도하려면 사람이 확인 후 조치해야 한다).

오래된 명령이 최신 목표값을 덮어쓰지 않도록(순서/버전 검증): 같은 대상에 다른
목표값이 새로 enqueue되면, 아직 실행 전(PENDING/RETRY_WAIT)인 기존 명령은 즉시
CANCELLED로 표시한다. 그래도 그 사이 다른 worker가 이미 claim(RUNNING)했을 수
있으므로, execute_command()는 실제 채널 호출 직전에 "이 명령보다 나중에 생성된
같은 대상 명령이 있는가"를 한 번 더 확인해(exists_newer_command_for_target) 있으면
전송하지 않고 CANCELLED로 남긴다 - 이미 낡은 값이 채널에 늦게 반영되는 것을 막는다.

UNKNOWN 대상과의 충돌: 같은 대상에 아직 해소되지 않은 UNKNOWN 명령(더 이전에
생성됨)이 있으면, 채널이 그 명령을 실제로 처리했는지 알 수 없는 상태이므로 새
명령을 이번 회차에는 실행하지 않고 건너뛴다(PENDING 그대로 유지 - FAILED로
확정하지 않는다. 운영자가 UNKNOWN을 해소하면 다음 회차에 정상 실행된다).

⚠️ 이 서비스는 채널까지 포함한 "정확히 한 번" 전송을 보장하지 않는다 - 로컬
idempotency_key/lease는 "같은 명령을 두 번 만들거나 두 worker가 동시에 실행하지
않는다"는 보장일 뿐이다. 재고/판매상태 API는 모두 "절대값을 설정"하는 방식이라
(증감이 아님) 채널 쪽에서 두 요청이 뒤바뀐 순서로 도착하면 결과가 갈릴 수 있다는
근본적인 한계가 있다 - 위의 "오래된 명령 실행 차단"은 이 위험을 줄이지만 완전히
없애지는 못한다(공식 문서에 버전/순서 검증 필드가 없다).

실제 채널에 영향을 주는 이 서비스는 로컬 실물재고(models.inventory.Inventory)를
자동으로 읽거나 바꾸지 않는다 - target_quantity는 호출부(API)가 운영자로부터
이미 확정해 받은 값을 그대로 전달한다(자동 재고배분/안전재고/예약재고 차감 정책은
이번 범위 밖).
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from config.settings import settings
from integrations.malls import get_mall_connector
from integrations.malls.base_mall_connector import SALE_STATUS_ON_SALE, SALE_STATUS_SUSPENDED, BaseMallConnector
from integrations.malls.errors import (
    MarketplaceCapabilityUnsupportedError,
    MarketplaceCredentialMissingError,
    MarketplaceError,
    MarketplaceExternalAPIError,
)
from models.extra import AuditLog
from models.integration_sync import ExternalCommand, ProductSyncCommandDetail
from repositories.integration_sync_repository import ExternalCommandRepository, ProductSyncCommandDetailRepository
from repositories.platform_repository import PlatformRepository
from repositories.product_repository import ProductPlatformMapRepository

logger = logging.getLogger(__name__)

ConnectorFactory = Callable[[str, Any, Optional[int]], BaseMallConnector]

TARGET_TYPE = "PRODUCT_PLATFORM_MAP"
INVENTORY_UPDATE = "INVENTORY_UPDATE"
SALE_STATUS_UPDATE = "SALE_STATUS_UPDATE"

# 네이버 원상품 API가 문서로 확인한 재고수량 상한(공식 스펙: stockQuantity maximum
# 99999999) - 쿠팡은 상한이 문서화돼 있지 않으나 ERP 쪽 안전장치로 동일하게 적용한다
# (쿠팡에 이 값 자체를 채널 계약값으로 전달하는 것이 아니라 ERP 입력 검증 목적).
MAX_QUANTITY = 99_999_999

_SALE_STATUS_VALUES = frozenset({SALE_STATUS_ON_SALE, SALE_STATUS_SUSPENDED})

# 재시도 가능한(SAFE_RETRY) 실패의 최대 시도 횟수 - services.shipment_dispatch_service와 동일 정책.
MAX_ATTEMPTS = 5
RETRY_BACKOFF_MINUTES = [2, 5, 15, 30, 60]
STALE_RUNNING_TIMEOUT_MINUTES = 15

_SAFE_RETRY_REASON_CODES = frozenset({"RATE_LIMITED", "CONNECT_FAILED"})
_CONFIRMED_FAILED_REASON_CODES = frozenset({"AUTH_FAILED"})


class ProductChannelSyncDisabledError(Exception):
    """실계정 검증 승인 전이라 채널 재고/판매상태 전송 기능이 기본 비활성화(OFF)
    상태다. settings.product_channel_sync_enabled를 명시적으로 켜야 한다."""


class ProductSyncMappingNotFoundError(Exception):
    """product_platform_map_id에 해당하는 매핑을 찾을 수 없다."""


class ProductSyncAlreadyRunningError(Exception):
    """같은 명령이 이미 RUNNING(다른 worker가 처리 중)이다."""


class ProductSyncCommandTypeMismatchError(Exception):
    """execute_command()에 전달된 command_id가 이 서비스가 다루는 명령종류가 아니다
    (송장 worker가 재고 명령을, 또는 그 반대를 잘못 집어가는 것을 막는 방어)."""


class ProductSyncRejectedError(MarketplaceError):
    """채널이 명시적으로 거부(ERROR)를 응답했다 - 원본 응답 전문은 담지 않는다."""

    def __init__(self, marketplace_code: str, result_code: Optional[str]) -> None:
        self.marketplace_code = marketplace_code
        self.reason_code = result_code or "REJECTED"
        super().__init__(f"{marketplace_code}: 재고/판매상태 전송이 거부되었습니다(code={self.reason_code}).")


def _classify_write_outcome(exc: Exception) -> str:
    """ "SAFE_RETRY"/"CONFIRMED_FAILED"/"UNKNOWN" 중 하나를 반환한다
    (services.shipment_dispatch_service._classify_write_outcome과 동일 원칙 -
    재고/판매상태 전송도 부작용이 있는 쓰기 요청이라 기존 수집(읽기) 경로의
    MarketplaceExternalAPIError.retryable을 그대로 신뢰하지 않는다)."""
    if isinstance(exc, ProductSyncRejectedError):
        return "CONFIRMED_FAILED"
    if isinstance(exc, (MarketplaceCredentialMissingError, MarketplaceCapabilityUnsupportedError, ValueError)):
        return "CONFIRMED_FAILED"  # 채널 호출 이전 단계에서 막힘 - 전송 자체가 없었다.
    if isinstance(exc, MarketplaceExternalAPIError):
        if exc.reason_code in _SAFE_RETRY_REASON_CODES:
            return "SAFE_RETRY"
        if exc.reason_code in _CONFIRMED_FAILED_REASON_CODES:
            return "CONFIRMED_FAILED"
        return "UNKNOWN"  # TIMEOUT/TRANSPORT_ERROR/SERVER_ERROR/PARSE_FAILED/BAD_RESPONSE 등.
    return "UNKNOWN"  # 예상 밖 예외 - 안전한 기본값.


def _retry_backoff(attempt_count: int) -> timedelta:
    idx = min(max(attempt_count, 1), len(RETRY_BACKOFF_MINUTES)) - 1
    return timedelta(minutes=RETRY_BACKOFF_MINUTES[idx])


def _validate_quantity(quantity: int) -> None:
    if not isinstance(quantity, int) or isinstance(quantity, bool):
        raise ValueError("재고 수량은 정수여야 합니다.")
    if quantity < 0:
        raise ValueError("재고 수량은 0 이상이어야 합니다.")
    if quantity > MAX_QUANTITY:
        raise ValueError(f"재고 수량은 {MAX_QUANTITY} 이하여야 합니다.")


def _validate_sale_status(target_status: str) -> None:
    if target_status not in _SALE_STATUS_VALUES:
        raise ValueError(f"알 수 없는 target_status입니다: {target_status}")


@dataclass
class ProductSyncOutcome:
    command: ExternalCommand
    already_processed: bool  # True면 idempotency로 기존 SUCCESS 결과를 재사용(신규 API 호출 없음)


class ProductSyncDispatchService:
    def __init__(self, session: Session, connector_factory: ConnectorFactory = get_mall_connector) -> None:
        self.session = session
        self.connector_factory = connector_factory
        self.mapping_repo = ProductPlatformMapRepository(session)
        self.platform_repo = PlatformRepository(session)
        self.command_repo = ExternalCommandRepository(session)
        self.detail_repo = ProductSyncCommandDetailRepository(session)

    # --- 접수(enqueue) ---

    def enqueue_inventory_update(self, product_platform_map_id: int, target_quantity: int) -> ProductSyncOutcome:
        _validate_quantity(target_quantity)
        return self._enqueue(product_platform_map_id, INVENTORY_UPDATE, target_quantity, None)

    def enqueue_sale_status_update(self, product_platform_map_id: int, target_status: str) -> ProductSyncOutcome:
        _validate_sale_status(target_status)
        return self._enqueue(product_platform_map_id, SALE_STATUS_UPDATE, None, target_status)

    def _enqueue(
        self,
        product_platform_map_id: int,
        command_type: str,
        target_quantity: Optional[int],
        target_sale_status: Optional[str],
    ) -> ProductSyncOutcome:
        if not settings.product_channel_sync_enabled:
            raise ProductChannelSyncDisabledError(
                "재고/판매상태 전송 기능이 비활성화(OFF) 상태입니다 - 실계정 검증 승인 후 활성화해야 합니다."
            )
        mapping = self.mapping_repo.get_by_id(product_platform_map_id)
        if mapping is None:
            raise ProductSyncMappingNotFoundError(f"플랫폼 매핑을 찾을 수 없습니다: id={product_platform_map_id}")
        platform = self.platform_repo.get_by_id(mapping.platform_id)
        if platform is None:
            raise ProductSyncMappingNotFoundError(f"플랫폼 정보를 찾을 수 없습니다: platform_id={mapping.platform_id}")

        target_repr = target_quantity if command_type == INVENTORY_UPDATE else target_sale_status
        idempotency_key = f"{command_type}:{mapping.id}:{target_repr}"
        existing = self.command_repo.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return ProductSyncOutcome(command=existing, already_processed=existing.status == "SUCCESS")

        # 같은 대상에 다른 목표값으로 아직 실행 전(PENDING/RETRY_WAIT)인 명령이 있으면
        # 낡은 값이 되므로 취소한다 - RUNNING(이미 claim됨)은 여기서 강제 취소하지
        # 않는다(다른 worker가 다루는 중일 수 있어 execute_command()의 실행 직전
        # 재확인이 최종 방어선이다).
        for stale in self.command_repo.list_active_for_target(command_type, TARGET_TYPE, mapping.id):
            if stale.status != "RUNNING":
                stale.status = "CANCELLED"
                stale.error_code = "SUPERSEDED_BY_NEWER_REQUEST"

        command = self.command_repo.add(
            ExternalCommand(
                idempotency_key=idempotency_key,
                command_type=command_type,
                platform_id=platform.id,
                platform_code=platform.code,
                target_type=TARGET_TYPE,
                target_id=mapping.id,
                status="PENDING",
                trace_id=uuid.uuid4().hex,
            )
        )
        self.detail_repo.add(
            ProductSyncCommandDetail(
                command_id=command.id,
                product_platform_map_id=mapping.id,
                target_quantity=target_quantity,
                target_sale_status=target_sale_status,
            )
        )
        return ProductSyncOutcome(command=command, already_processed=False)

    # --- 실행(execute) ---

    def execute_command(self, command_id: int) -> ProductSyncOutcome:
        """outbox worker(scheduler.jobs.product_sync_dispatch_job)가 호출한다."""
        command = self.command_repo.get_by_id(command_id)
        if command is None:
            raise ValueError(f"명령을 찾을 수 없습니다: command_id={command_id}")
        if command.command_type not in (INVENTORY_UPDATE, SALE_STATUS_UPDATE):
            raise ProductSyncCommandTypeMismatchError(
                f"이 서비스가 다루지 않는 명령종류입니다: command_id={command_id}, "
                f"command_type={command.command_type}"
            )
        if command.status == "SUCCESS":
            return ProductSyncOutcome(command=command, already_processed=True)

        # UNKNOWN 대상과의 충돌 방지: 같은 대상(target_type+target_id)에 더 먼저
        # 생성된(id가 더 작은) 미해소 UNKNOWN 명령이 있으면, 채널이 그 명령을 실제로
        # 처리했는지 알 수 없는 채로 이번 명령을 실행하지 않는다(PENDING 유지 - FAILED
        # 확정 아님, 운영자가 UNKNOWN을 해소하면 다음 회차에 정상 실행된다).
        if self.command_repo.exists_unresolved_unknown_predecessor(
            command.command_type, command.target_type, command.target_id, command.id
        ):
            logger.info("선행 UNKNOWN 명령이 해소되지 않아 이번 회차는 건너뜁니다: command_id=%s", command_id)
            return ProductSyncOutcome(command=command, already_processed=False)

        lease_token = uuid.uuid4().hex
        if not self.command_repo.claim(command_id, lease_token):
            self.session.refresh(command)
            if command.status == "SUCCESS":
                return ProductSyncOutcome(command=command, already_processed=True)
            if command.status == "RUNNING":
                raise ProductSyncAlreadyRunningError(f"이미 처리 중인 요청입니다: command_id={command_id}")
            raise ValueError(f"실행 대상이 아닌 상태입니다(status={command.status}): command_id={command_id}")
        self.session.refresh(command)

        # claim 직후, 실제 채널 호출 직전에 다시 한번 "이 명령보다 나중에 생성된 같은
        # 대상 명령이 있는가"를 확인한다 - 있으면 이미 낡은 값이므로 전송하지 않고
        # CANCELLED로 남긴다(오래된 명령이 최신 목표값을 늦게 덮어쓰는 것을 방지).
        if self.command_repo.exists_newer_command_for_target(
            command.command_type, command.target_type, command.target_id, command.id
        ):
            transitioned = self.command_repo.try_transition(
                command.id, lease_token, status="CANCELLED", error_code="SUPERSEDED_BY_NEWER_REQUEST"
            )
            self.session.refresh(command)
            if not transitioned:
                logger.warning("lease 소유권을 잃어 CANCELLED 기록을 건너뜁니다: command_id=%s", command.id)
            return ProductSyncOutcome(command=command, already_processed=False)

        try:
            detail = self.detail_repo.get_by_command_id(command.id)
            if detail is None:
                raise ValueError(f"명령의 목표값 정보가 없습니다: command_id={command_id}")
            mapping = self.mapping_repo.get_by_id(detail.product_platform_map_id)
            if mapping is None:
                raise ValueError(f"플랫폼 매핑을 찾을 수 없습니다: id={detail.product_platform_map_id}")
            platform = self.platform_repo.get_by_id(mapping.platform_id)
            if platform is None:
                raise ValueError(f"플랫폼 정보를 찾을 수 없습니다: platform_id={mapping.platform_id}")
            connector = self.connector_factory(platform.connector_class, self.session, platform.id)
            self._dispatch(connector, platform.code, command, mapping, detail)
        except Exception as e:  # noqa: BLE001 - 분류 후 안전하게 기록하고 다시 던진다.
            self._mark_after_failure(command, lease_token, e)
            raise

        transitioned = self.command_repo.try_transition(
            command.id,
            lease_token,
            status="SUCCESS",
            response_summary="채널 확인 완료",
            completed_at=datetime.now(timezone.utc),
        )
        self.session.refresh(command)
        if not transitioned:
            logger.warning("lease 소유권을 잃어 SUCCESS 기록을 건너뜁니다: command_id=%s", command.id)
            return ProductSyncOutcome(command=command, already_processed=(command.status == "SUCCESS"))
        return ProductSyncOutcome(command=command, already_processed=False)

    def _dispatch(
        self,
        connector: BaseMallConnector,
        platform_code: str,
        command: ExternalCommand,
        mapping: Any,
        detail: ProductSyncCommandDetail,
    ) -> None:
        if command.command_type == INVENTORY_UPDATE:
            if not getattr(connector, "supports_inventory_update", False):
                raise MarketplaceCapabilityUnsupportedError(platform_code, "inventory_update")
            assert detail.target_quantity is not None
            result = connector.update_inventory(
                platform_option_id=mapping.platform_option_id,
                quantity=detail.target_quantity,
                platform_origin_product_id=mapping.platform_origin_product_id,
            )
        else:
            if not getattr(connector, "supports_sale_status_update", False):
                raise MarketplaceCapabilityUnsupportedError(platform_code, "sale_status_update")
            assert detail.target_sale_status is not None
            result = connector.update_sale_status(
                platform_option_id=mapping.platform_option_id,
                target_status=detail.target_sale_status,
                platform_origin_product_id=mapping.platform_origin_product_id,
            )
        if not result.accepted:
            raise ProductSyncRejectedError(platform_code, result.platform_result_code)

    def _mark_after_failure(self, command: ExternalCommand, lease_token: str, exc: Exception) -> None:
        kind = _classify_write_outcome(exc)
        error_code = getattr(exc, "reason_code", None) or type(exc).__name__
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

    def recover_stale_running(self, command_type: str) -> int:
        """outbox worker가 매 실행 시작 시 먼저 호출한다(services.shipment_dispatch_service.
        recover_stale_running과 동일 원칙) - RUNNING으로 너무 오래 머문 명령을 PENDING이
        아니라 UNKNOWN으로 회수한다."""
        threshold = datetime.now(timezone.utc) - timedelta(minutes=STALE_RUNNING_TIMEOUT_MINUTES)
        stale = self.command_repo.list_stale_running(command_type, threshold)
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
        """UNKNOWN(결과 확인 필요) 명령을 운영자가 채널을 직접 확인한 뒤 해소한다
        (services.shipment_dispatch_service.resolve_unknown_command과 동일한 세 가지
        해소 방식)."""
        if resolution not in ("CONFIRMED_NOT_SENT", "CONFIRMED_SUCCESS", "CONFIRMED_FAILED"):
            raise ValueError(f"알 수 없는 해소 방식입니다: {resolution}")
        command = self.command_repo.get_by_id(command_id)
        if command is None:
            raise ValueError(f"명령을 찾을 수 없습니다: command_id={command_id}")
        if command.status != "UNKNOWN":
            raise ValueError(f"결과 확인이 필요한(UNKNOWN) 명령만 해소할 수 있습니다(현재 상태: {command.status}).")

        before_status = command.status
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

        self.session.add(
            AuditLog(
                entity_type="EXTERNAL_COMMAND",
                entity_id=command.id,
                action="UPDATE",
                before_json=f'{{"status":"{before_status}"}}',
                after_json=f'{{"status":"{command.status}"}}',
                changed_by=resolved_by,
                changed_at=datetime.now(timezone.utc),
                command="product_sync_command.resolve_unknown",
                reason=f"운영자가 채널을 직접 확인해 해소: {resolution}",
            )
        )
        self.session.flush()
        return command
