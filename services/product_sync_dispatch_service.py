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
확정하지 않는다. 운영자가 UNKNOWN을 해소하면 다음 회차에 정상 실행된다). stale
RUNNING 회수(recover_stale_running)도 마찬가지로 UNKNOWN으로 보낼 뿐 "채널 요청이
끝났다"고 간주하지 않는다 - 회수 후에도 그 대상에 대한 후속 명령은 위 UNKNOWN
충돌 검사에 그대로 걸려 운영자가 해소하기 전까지 실행되지 않는다.

같은 외부 대상을 공유하는 서로 다른 내부 매핑: platform_option_id는 (platform_id,
platform_option_id) 유니크 제약으로 ProductPlatformMap 1건과 항상 1:1이지만,
platform_origin_product_id(네이버 원상품번호)에는 유니크 제약이 없다 - 원상품
하나가 스마트스토어/윈도우 등 복수 채널상품(=복수 매핑 행)을 가질 수 있도록
의도적으로 허용했기 때문이다. 그래서 위의 모든 동시성 검사(재실행 취소/UNKNOWN
충돌/아래 교차종류 실행중 확인)는 ProductPlatformMap.id 하나가 아니라
_resolve_contention_target_ids()가 모은 "같은 외부 대상을 공유하는 모든 매핑 id"
집합을 대상으로 한다 - 그렇지 않으면 서로 다른 매핑 두 개가 실제로는 같은
원상품에 대해 동시에 서로 다른 값을 보낼 수 있다.

재고 변경과 판매상태 변경의 상호 배제(같은 외부 대상에 한함): 두 명령종류는
서로 다른 의도이므로 대기(PENDING/RETRY_WAIT) 상태의 상대방을 잘못 취소하지
않는다(위 "오래된 명령" 취소는 command_type이 같을 때만 적용). 하지만 네이버처럼
"현재 상태 조회 -> 변경 요청"을 하나의 채널 API로 묶어 처리하는 채널에서는, 같은
외부 대상에 대해 재고 명령과 판매상태 명령이 동시에 채널로 나가면 한쪽이 읽은
현재값을 다른 쪽이 그 사이 바꿔버려 서로 덮어쓸 위험이 있다(예: 재고 명령이
"현재 상태=SALE"을 읽은 직후 판매상태 명령이 SUSPENSION으로 바꿨는데, 재고
명령이 그 SALE 값을 그대로 실어 보내 SUSPENSION을 되돌리는 경우). 그래서
execute_command()는 claim 이후, 명령종류를 가리지 않고 같은 외부 대상에 지금
RUNNING인 다른 명령이 있으면 채널을 호출하지 않고 PENDING으로 되돌려(lease
반납) 다음 회차에 다시 시도한다(exists_other_running_for_targets) - 채널 실패가
아니므로 FAILED/RETRY_WAIT 백오프는 적용하지 않는다.

외부 대상 단위 배타 실행의 DB 수준 보장(advisory lock): 위의 UNKNOWN/최신명령/
교차종류 실행중 확인은 전부 평범한 SELECT이고, execute_command() 전체가
claim()부터 채널 호출·최종 상태 기록까지 커밋 하나 없이 한 트랜잭션으로
진행된다(commit은 scheduler.jobs.product_sync_dispatch_job이 처리를 마친 뒤에야
한다). READ COMMITTED 하에서 그런 SELECT는 다른 트랜잭션이 "이미 커밋한" 변경만
보므로, 같은 외부 대상을 가리키는 서로 다른 명령 행 두 개를 서로 다른 worker가
각자 claim()한 뒤 거의 동시에 그 SELECT를 실행하면 둘 다 "다른 RUNNING 없음"으로
잘못 판단하고 둘 다 채널을 동시 호출할 수 있었다(claim()의 행 단위 UPDATE는
"같은 명령 행"의 중복 처리만 막을 뿐, "다른 행이 같은 외부 대상을 가리키는" 경우는
막지 못함 - 실제 동시 worker 통합 테스트로 확인된 공백). 그래서 contention_ids를
구한 직후, 위 모든 검사보다 먼저 acquire_target_lock()으로 PostgreSQL
pg_advisory_xact_lock을 건다 - 같은 외부 대상을 다투는 다른 worker는 이 호출에서
대기하다가 앞선 worker의 트랜잭션이 commit/rollback되어야(그 안에서 이뤄진 모든
상태 변화가 이미 커밋된 뒤에야) 통과하므로, 이후의 SELECT들이 항상 최신 상태를
올바르게 본다. SQLite(단위테스트)에는 advisory lock이 없어 이 함수는 아무 것도
하지 않는다 - 실제 동시성 보장은 격리 PostgreSQL 통합 테스트로 검증한다.

⚠️ 이 서비스는 채널까지 포함한 "정확히 한 번" 전송을 보장하지 않는다 - 로컬
idempotency_key/lease는 "같은 명령을 두 번 만들거나 두 worker가 동시에 실행하지
않는다"는 보장일 뿐이다. 재고/판매상태 API는 모두 "절대값을 설정"하는 방식이라
(증감이 아님) 채널 쪽에서 두 요청이 뒤바뀐 순서로 도착하면 결과가 갈릴 수 있다는
근본적인 한계가 있다 - 위의 "오래된 명령 실행 차단"은 이 위험을 줄이지만 완전히
없애지는 못한다(공식 문서에 버전/순서 검증 필드가 없다).

⚠️ 이 서비스의 직렬화는 이 ERP가 만든 ExternalCommand끼리만 적용된다 - 채널
판매자센터(쿠팡윈도우/네이버 스마트스토어센터) 관리자 화면에서의 수동 변경이나,
이 ERP가 아닌 다른 프로그램(별도 연동 툴 등)이 같은 채널 API를 직접 호출하는
경우는 이 직렬화로 전혀 막을 수 없다 - 그런 외부 변경과 이 서비스의 전송이
겹치면 여전히 순서가 뒤바뀔 수 있다(공식 문서에 채널 쪽 버전/순서 검증 필드가
없어 이 ERP 쪽에서 감지할 방법도 없다).

실제 채널에 영향을 주는 이 서비스는 로컬 실물재고(models.inventory.Inventory)를
자동으로 읽거나 바꾸지 않는다 - target_quantity는 호출부(API)가 운영자로부터
이미 확정해 받은 값을 그대로 전달한다(자동 재고배분/안전재고/예약재고 차감 정책은
이번 범위 밖).
"""

import hashlib
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
# 상용 ERP 확장(3단계, 두 번째 묶음) - 기존 상품의 상품명/판매가/상세설명 중 일부만
# 수정한다. TARGET_TYPE/target_id(=product_platform_map.id)를 재고/판매상태와
# 그대로 공유하므로, _resolve_contention_target_ids()의 형제 매핑 대상 확장과
# exists_other_running_for_targets()의 명령종류 무관 실행중 검사가 별도 수정 없이
# 이 명령종류에도 그대로 적용된다(같은 외부 대상을 두고 재고/판매상태/정보수정이
# 서로 겹쳐 실행되지 않는다).
PRODUCT_INFO_UPDATE = "PRODUCT_INFO_UPDATE"

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


def _validate_info_update_price(sale_price: float) -> None:
    if isinstance(sale_price, bool) or not isinstance(sale_price, (int, float)):
        raise ValueError("판매가는 숫자여야 합니다.")
    if sale_price < 0:
        raise ValueError("판매가는 0 이상이어야 합니다.")


def _info_update_target_repr(name: Optional[str], sale_price: Optional[float], description: Optional[str]) -> str:
    """PRODUCT_INFO_UPDATE의 idempotency_key에 쓰는 목표값 표현 - description은
    길어질 수 있어(HTML) 해시로 줄인다(같은 값이면 항상 같은 해시)."""
    description_hash = hashlib.sha256(description.encode("utf-8")).hexdigest()[:16] if description is not None else None
    return f"{name}|{sale_price}|{description_hash}"


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
        return self._enqueue(
            product_platform_map_id,
            INVENTORY_UPDATE,
            str(target_quantity),
            lambda command, mapping: ProductSyncCommandDetail(
                command_id=command.id, product_platform_map_id=mapping.id, target_quantity=target_quantity
            ),
        )

    def enqueue_sale_status_update(self, product_platform_map_id: int, target_status: str) -> ProductSyncOutcome:
        _validate_sale_status(target_status)
        return self._enqueue(
            product_platform_map_id,
            SALE_STATUS_UPDATE,
            target_status,
            lambda command, mapping: ProductSyncCommandDetail(
                command_id=command.id, product_platform_map_id=mapping.id, target_sale_status=target_status
            ),
        )

    def enqueue_info_update(
        self,
        product_platform_map_id: int,
        name: Optional[str] = None,
        sale_price: Optional[float] = None,
        description: Optional[str] = None,
    ) -> ProductSyncOutcome:
        """상품명/판매가/상세설명 중 실제로 바뀐 값만 넘긴다 - None인 항목은 커넥터가
        채널의 현재값을 그대로 보존한다(models.integration_sync.ProductSyncCommandDetail
        모듈 docstring 참고)."""
        if name is None and sale_price is None and description is None:
            raise ValueError("수정할 항목(상품명/판매가/상세설명)이 하나도 없습니다.")
        if sale_price is not None:
            _validate_info_update_price(sale_price)
        target_repr = _info_update_target_repr(name, sale_price, description)
        return self._enqueue(
            product_platform_map_id,
            PRODUCT_INFO_UPDATE,
            target_repr,
            lambda command, mapping: ProductSyncCommandDetail(
                command_id=command.id,
                product_platform_map_id=mapping.id,
                target_name=name,
                target_sale_price=sale_price,
                target_description=description,
            ),
        )

    def _enqueue(
        self,
        product_platform_map_id: int,
        command_type: str,
        target_repr: str,
        build_detail: Callable[[ExternalCommand, Any], ProductSyncCommandDetail],
    ) -> ProductSyncOutcome:
        # PRODUCT_INFO_UPDATE(상용 ERP 확장 3단계 두 번째 묶음)는 재고/판매상태와
        # 별개의 독립 플래그(product_publish_enabled)로 통제한다 - 하나를 켜도
        # 다른 하나는 여전히 OFF로 남아야 한다(요구사항: "신규 등록·정보 수정은
        # 별도 기본 OFF 플래그로 제어하고 기존 플래그도 OFF 유지").
        flag_enabled = (
            settings.product_publish_enabled
            if command_type == PRODUCT_INFO_UPDATE
            else settings.product_channel_sync_enabled
        )
        if not flag_enabled:
            raise ProductChannelSyncDisabledError(
                "재고/판매상태/정보수정 전송 기능이 비활성화(OFF) 상태입니다 - 실계정 검증 승인 후 활성화해야 합니다."
            )
        mapping = self.mapping_repo.get_by_id(product_platform_map_id)
        if mapping is None:
            raise ProductSyncMappingNotFoundError(f"플랫폼 매핑을 찾을 수 없습니다: id={product_platform_map_id}")
        platform = self.platform_repo.get_by_id(mapping.platform_id)
        if platform is None:
            raise ProductSyncMappingNotFoundError(f"플랫폼 정보를 찾을 수 없습니다: platform_id={mapping.platform_id}")

        idempotency_key = f"{command_type}:{mapping.id}:{target_repr}"
        existing = self.command_repo.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return ProductSyncOutcome(command=existing, already_processed=existing.status == "SUCCESS")

        # 같은 대상(들)에 다른 목표값으로 아직 실행 전(PENDING/RETRY_WAIT)인 같은
        # 명령종류의 명령이 있으면 낡은 값이 되므로 취소한다 - 서로 다른 매핑이 같은
        # 외부 대상(예: 네이버 원상품번호)을 공유하는 경우도 함께 취소 대상에
        # 포함한다(_resolve_contention_target_ids 참고). RUNNING(이미 claim됨)은
        # 여기서 강제 취소하지 않는다(다른 worker가 다루는 중일 수 있어
        # execute_command()의 실행 직전 재확인이 최종 방어선이다). 다른
        # 명령종류(재고 vs 판매상태 vs 정보수정)는 서로 독립된 의도라 취소 대상에서
        # 제외한다(list_active_for_target이 command_type으로 이미 필터링).
        contention_ids = self._resolve_contention_target_ids(mapping)
        for stale in self.command_repo.list_active_for_target(command_type, TARGET_TYPE, contention_ids):
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
        self.detail_repo.add(build_detail(command, mapping))
        return ProductSyncOutcome(command=command, already_processed=False)

    # --- 동시성 제어 공통 ---

    def _resolve_contention_target_ids(self, mapping: Any) -> list[int]:
        """이 매핑과 "같은 외부 대상"을 공유하는 모든 ProductPlatformMap.id를 반환한다
        (자기 자신 포함). platform_option_id는 (platform_id, platform_option_id) 유니크
        제약으로 이미 매핑 1건과 1:1이라 별도 처리가 필요 없지만, 네이버의
        platform_origin_product_id는 유니크 제약이 없다 - 원상품 하나가 스마트스토어/
        윈도우 등 복수 채널상품(=복수 ProductPlatformMap 행)을 가질 수 있도록 의도적으로
        허용했기 때문이다(models.product.ProductPlatformMap 참고). 그래서 같은
        platform_origin_product_id를 공유하는 다른 매핑이 있으면 전부 "같은 외부
        대상"으로 취급해 동시성 검사(재실행/UNKNOWN충돌/교차종류 RUNNING 확인)에
        함께 포함시킨다 - 그렇지 않으면 서로 다른 매핑 두 개가 실제로는 같은 원상품에
        대해 동시에 서로 다른 값을 전송할 수 있다."""
        ids = {mapping.id}
        if mapping.platform_origin_product_id:
            siblings = self.mapping_repo.list_by_platform_and_origin_product_id(
                mapping.platform_id, mapping.platform_origin_product_id
            )
            ids.update(m.id for m in siblings)
        return sorted(ids)

    # --- 실행(execute) ---

    def execute_command(self, command_id: int) -> ProductSyncOutcome:
        """outbox worker(scheduler.jobs.product_sync_dispatch_job)가 호출한다."""
        command = self.command_repo.get_by_id(command_id)
        if command is None:
            raise ValueError(f"명령을 찾을 수 없습니다: command_id={command_id}")
        if command.command_type not in (INVENTORY_UPDATE, SALE_STATUS_UPDATE, PRODUCT_INFO_UPDATE):
            raise ProductSyncCommandTypeMismatchError(
                f"이 서비스가 다루지 않는 명령종류입니다: command_id={command_id}, "
                f"command_type={command.command_type}"
            )
        if command.status == "SUCCESS":
            return ProductSyncOutcome(command=command, already_processed=True)

        detail = self.detail_repo.get_by_command_id(command.id)
        if detail is None:
            raise ValueError(f"명령의 목표값 정보가 없습니다: command_id={command_id}")
        mapping = self.mapping_repo.get_by_id(detail.product_platform_map_id)
        if mapping is None:
            raise ValueError(f"플랫폼 매핑을 찾을 수 없습니다: id={detail.product_platform_map_id}")
        contention_ids = self._resolve_contention_target_ids(mapping)

        # 이 외부 대상(들)에 대한 실행을 트랜잭션이 끝날 때(commit/rollback)까지
        # DB 수준에서 배타적으로 만든다 - 아래의 UNKNOWN/최신명령 확인은 전부 평범한
        # SELECT라 이 함수가 커밋 하나 없이 채널 호출까지 한 트랜잭션으로 진행하는
        # 동안에는 다른 worker의 아직 커밋되지 않은 변경을 볼 수 없다(TOCTOU). 이
        # 잠금이 없으면 같은 외부 대상을 가리키는 서로 다른 명령 행 두 개가 동시에
        # 채널을 호출할 수 있다(repositories.integration_sync_repository.
        # ExternalCommandRepository.acquire_target_lock 참고 - PostgreSQL 전용,
        # SQLite 단위테스트에서는 아무 것도 하지 않는다).
        self.command_repo.acquire_target_lock(command.target_type, contention_ids)

        # UNKNOWN 대상과의 충돌 방지: 같은 외부 대상(들)에 더 먼저 생성된(id가 더
        # 작은) 미해소 UNKNOWN 명령이 있으면, 채널이 그 명령을 실제로 처리했는지 알
        # 수 없는 채로 이번 명령을 실행하지 않는다(PENDING 유지 - FAILED 확정 아님,
        # 운영자가 UNKNOWN을 해소하면 다음 회차에 정상 실행된다).
        if self.command_repo.exists_unresolved_unknown_predecessor(
            command.command_type, command.target_type, contention_ids, command.id
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
        # 외부 대상 명령이 있는가"를 확인한다 - 있으면 이미 낡은 값이므로 전송하지
        # 않고 CANCELLED로 남긴다(오래된 명령이 최신 목표값을 늦게 덮어쓰는 것을 방지).
        if self.command_repo.exists_newer_command_for_target(
            command.command_type, command.target_type, contention_ids, command.id
        ):
            transitioned = self.command_repo.try_transition(
                command.id, lease_token, status="CANCELLED", error_code="SUPERSEDED_BY_NEWER_REQUEST"
            )
            self.session.refresh(command)
            if not transitioned:
                logger.warning("lease 소유권을 잃어 CANCELLED 기록을 건너뜁니다: command_id=%s", command.id)
            return ProductSyncOutcome(command=command, already_processed=False)

        # 명령종류(재고 vs 판매상태)가 달라도, 같은 외부 대상에 대해 지금 실제로 채널
        # 호출 중인 다른 명령이 있으면 이번 실행은 미룬다 - 네이버처럼 "현재 상태
        # 조회 -> 변경 요청"을 한 채널 API로 묶어 처리하는 채널에서, 재고 명령과
        # 판매상태 명령이 동시에 나가면 서로의 조회 결과를 되돌릴 위험이 있다(모듈
        # docstring 참고). claim으로 얻은 lease는 그대로 반납(PENDING)해 다음 회차에
        # 바로 재시도되게 한다 - 채널 실패가 아니므로 FAILED/RETRY_WAIT 백오프를
        # 적용하지 않는다.
        if self.command_repo.exists_other_running_for_targets(command.target_type, contention_ids, command.id):
            transitioned = self.command_repo.try_transition(command.id, lease_token, status="PENDING", lease_token=None)
            self.session.refresh(command)
            if not transitioned:
                logger.warning("lease 소유권을 잃어 재대기 반영을 건너뜁니다: command_id=%s", command.id)
            else:
                logger.info(
                    "같은 외부 대상에 다른 종류의 명령이 실행 중이라 이번 회차는 재대기합니다: command_id=%s",
                    command_id,
                )
            return ProductSyncOutcome(command=command, already_processed=False)

        try:
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
        elif command.command_type == SALE_STATUS_UPDATE:
            if not getattr(connector, "supports_sale_status_update", False):
                raise MarketplaceCapabilityUnsupportedError(platform_code, "sale_status_update")
            assert detail.target_sale_status is not None
            result = connector.update_sale_status(
                platform_option_id=mapping.platform_option_id,
                target_status=detail.target_sale_status,
                platform_origin_product_id=mapping.platform_origin_product_id,
            )
        else:
            if not getattr(connector, "supports_product_info_update", False):
                raise MarketplaceCapabilityUnsupportedError(platform_code, "product_info_update")
            result = connector.update_product_info(
                platform_option_id=mapping.platform_option_id,
                platform_origin_product_id=mapping.platform_origin_product_id,
                name=detail.target_name,
                sale_price=detail.target_sale_price,
                description=detail.target_description,
            )
        if not result.accepted:
            raise ProductSyncRejectedError(platform_code, result.platform_result_code)

    def _mark_after_failure(self, command: ExternalCommand, lease_token: str, exc: Exception) -> None:
        kind = _classify_write_outcome(exc)
        # capability(MarketplaceCapabilityUnsupportedError)가 reason_code보다 먼저 -
        # "옵션 구조 미지원"/"판매상태 전환 불가" 등 구체적 차단 사유를 화면에 그대로
        # 노출하기 위함이다(모두 MarketplaceCapabilityUnsupportedError라는 클래스명
        # 하나로 뭉뚱그려지면 사용자가 원인을 구분할 수 없다).
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
