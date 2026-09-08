"""
services/shipment_dispatch_service.py
------------------------------------------
Shipment(내부 배송 레코드)를 실제 채널(네이버/쿠팡)에 전송한다.

기본 차단(실계정 검증 전): settings.shipment_channel_submit_enabled가 False(기본값)이면
enqueue()가 즉시 ShipmentChannelSubmitDisabledError를 던진다 - PENDING 명령 자체를
만들지 않으므로 outbox_dispatch_job이 실행할 대상도 생기지 않는다. 즉 이 플래그가
꺼져 있으면 전송 버튼(API)·enqueue()·worker 실행 전부 실제 채널 호출 없이 안전하게
차단된다. 실계정 검증 승인 후 운영자가 명시적으로 켜야 한다(기존 주문수집/네이버
상품동기화는 이 플래그와 무관하게 항상 동작한다).

outbox 실행 모델(비동기):
- API(POST /api/shipments/{id}/submit)는 enqueue()만 호출한다 - 채널 HTTP 호출을
  API 요청 스레드에서 동기 실행하지 않는다. enqueue()는 PENDING ExternalCommand를
  멱등하게 만들거나(이미 있으면 그대로 재사용) 즉시 반환한다 - 버튼 연타로 새 명령이
  중복 생성되지 않는다.
- 실제 채널 호출은 scheduler.jobs.outbox_dispatch_job이 주기적으로 due한 명령마다
  execute_command()를 호출해 수행한다. 화면은 command_id로 상태를 조회(GET
  /api/shipments/commands/{id})해 폴링하고, SUCCESS를 확인한 뒤에만 성공으로 표시해야 한다.
- submit()은 enqueue()+execute_command()를 그 자리에서 동기 실행하는 편의 메서드로
  남겨둔다(테스트/관리자 강제 즉시실행용) - 실 API 경로는 더 이상 이 메서드를 쓰지 않는다.

결과 불명(UNKNOWN) 처리 - "채널이 처리했는지 알 수 없는 실패를 자동 재전송하지
않는다"는 원칙(1단계 완결 검토): _classify_write_outcome()이 예외를 세 가지로 분류한다.
- SAFE_RETRY: 채널이 요청을 아예 받지 못했음(연결 자체가 성립되지 않음)이 확실하거나,
  채널이 명시적으로 "아직 처리 안 했다"고 응답한 경우(429 rate limit) - 자동 재시도
  안전(RETRY_WAIT, 백오프, MAX_ATTEMPTS 소진 시 FAILED로 확정).
- CONFIRMED_FAILED: 채널로 요청을 보내지도 않았거나(자격증명 없음/미지원/사전검증
  실패), 채널이 명시적으로 거부 응답을 준 경우 - 확실히 미처리이므로 FAILED로 확정한다
  (사람의 조치 필요, 자동 재시도 없음).
- UNKNOWN: timeout/응답 파싱 실패/그 외 전송 중 오류/5xx처럼 요청이 채널에 도달해
  실제로 처리됐을 가능성을 배제할 수 없는 경우 - 자동으로 재시도도, FAILED 확정도
  하지 않는다. resolve_unknown_command()로 운영자가 채널을 직접 확인한 뒤에만 벗어날
  수 있다. 이 분류는 기존 수집(읽기) 경로가 쓰는 MarketplaceExternalAPIError.retryable
  플래그를 그대로 신뢰하지 않는다 - 그 플래그는 "다시 읽어도 안전한가"만 판단하며
  "이미 채널에 쓰기가 반영됐을 수 있는가"는 구분하지 않기 때문이다. 공식 문서로 확인된
  멱등성 보장이나 채널 조회로 미처리를 확정할 방법이 없는 한(Naver/Coupang 모두 아직
  없음), 결과가 불명확하면 항상 UNKNOWN을 기본값으로 한다.
- RUNNING으로 STALE_RUNNING_TIMEOUT_MINUTES 이상 머물러 있으면(worker 프로세스가 실행
  도중 죽었다고 추정) recover_stale_running()이 PENDING이 아니라 UNKNOWN으로 회수한다 -
  같은 이유(채널 도달 여부 불명)로 자동 재시도 대상이 아니다. 회수 시 lease_token도
  비워 이전 worker가 뒤늦게 끝나도 그 결과로 이 상태를 덮어쓰지 못하게 한다.

동시 실행 방지(claim/lease): execute_command()는 select-then-update가 아니라
ExternalCommandRepository.claim()(원자적 조건부 UPDATE)으로 PENDING/RETRY_WAIT ->
RUNNING 전이와 lease_token 발급을 한 번에 수행한다 - 두 worker가 같은 명령을 동시에
claim 시도해도 정확히 하나만 성공한다. 최종 상태 반영(try_transition)도 lease_token이
아직 자신의 것일 때만 허용된다 - recover_stale_running()이 그 사이 회수했다면(lease_token
변경) 뒤늦게 끝난 worker의 결과는 조용히 무시된다(로그만 남김). 로컬 idempotency_key는
"같은 명령을 두 번 만들지 않는다"는 보장일 뿐 채널 쪽 exactly-once를 보장하지 않는다.

부분성공: 여러 라인을 전송하다 일부만 성공하면(_dispatch_all_lines) 성공한 라인은
ExternalCommandLineResult로 즉시 기록되고, 다음 실행(재시도/수동 재개)에서는 그
라인을 건너뛴다 - 이미 성공한 라인을 중복 전송하지 않는다.

부분출고와 주문 전체 상태: 송장 전송 성공 후에도 주문 전체를 SHIPPING으로 반영하는
것은 그 주문의 모든(채널 라인 식별자가 있는) OrderItem이 "발송 수량 합계 >= 주문
수량"으로 완전히 이행된 경우뿐이다(_is_order_fully_dispatched) - 부분출고 상태에서는
Order.status를 건드리지 않는다(Shipment/ShipmentItem 자체가 이미 부분출고를 표현한다).

carrier 표기 규약: Shipment.carrier는 자유 텍스트가 아니라
integrations.malls.carrier_codes에 등록된 내부 표준 코드(예: "CJ_LOGISTICS")를
저장해야 한다 - 등록되지 않은 코드는 UnknownCarrierError로 안전하게 거부된다.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from config.settings import settings
from integrations.malls import get_mall_connector
from integrations.malls.base_mall_connector import BaseMallConnector
from integrations.malls.carrier_codes import UnknownCarrierError, normalize_carrier_code
from integrations.malls.errors import (
    MarketplaceCapabilityUnsupportedError,
    MarketplaceCredentialMissingError,
    MarketplaceError,
    MarketplaceExternalAPIError,
)
from models.extra import AuditLog
from models.integration_sync import ExternalCommand
from models.order import Order, OrderItem, Shipment, ShipmentItem
from models.platform import Platform
from repositories.integration_sync_repository import ExternalCommandLineResultRepository, ExternalCommandRepository
from repositories.order_repository import OrderRepository, ShipmentRepository
from repositories.platform_repository import PlatformRepository
from services.order_channel_sync_service import OrderChannelSyncService

logger = logging.getLogger(__name__)

ConnectorFactory = Callable[[str, Any, Optional[int]], BaseMallConnector]

# 재시도 가능한(SAFE_RETRY) 실패의 최대 시도 횟수(이후에는 FAILED로 확정, 자동 재시도 중단).
MAX_ATTEMPTS = 5
# 시도 횟수(1-based)별 재시도 대기 시간(분) - 마지막 값을 넘는 시도는 마지막 값을 그대로 쓴다.
RETRY_BACKOFF_MINUTES = [2, 5, 15, 30, 60]
# RUNNING으로 이보다 오래 머물러 있으면 worker 프로세스가 죽었다고 보고 UNKNOWN으로 회수한다.
STALE_RUNNING_TIMEOUT_MINUTES = 15

# 채널이 요청 자체를 받지 못했음이 확실하거나(연결 미성립) 명시적으로 "아직 처리 안
# 했다"고 응답한(429) reason_code - 자동 재시도가 안전하다.
_SAFE_RETRY_REASON_CODES = frozenset({"RATE_LIMITED", "CONNECT_FAILED"})
# 채널이 비즈니스 로직 실행 전 단계(인증)에서 확실히 거부한 reason_code.
_CONFIRMED_FAILED_REASON_CODES = frozenset({"AUTH_FAILED"})


class ShipmentNotReadyError(Exception):
    """배송이 READY 상태가 아니거나 필수 정보(운송사/송장번호)가 없어 전송할 수 없다."""


class ShipmentPlatformMismatchError(Exception):
    """같은 배송(합포장)에 서로 다른 플랫폼의 주문이 섞여 있어 단일 채널로 전송할 수 없다."""


class ShipmentAlreadyRunningError(Exception):
    """같은 명령이 이미 RUNNING(다른 worker가 처리 중)이다."""


class ShipmentChannelSubmitDisabledError(Exception):
    """실계정 검증 승인 전이라 채널 전송 기능이 기본 비활성화(OFF) 상태다.

    settings.shipment_channel_submit_enabled를 명시적으로 켜야 enqueue()/전송 API/
    outbox worker가 동작한다(모듈 docstring 참고)."""


class ShipmentBoxQuantityAmbiguousError(Exception):
    """쿠팡처럼 배송묶음(box) ID 단위로만 표현되는 채널에서, 주문라인의 일부 수량만
    발송하는 것은 이 데이터 모델로 안전하게 표현할 수 없다.

    쿠팡 송장 API는 vendorItemId+shipmentBoxId 단위로만 "발송 처리"를 표시하며 수량을
    받지 않는다 - 같은 라인의 남은 수량을 나중에 다른 배송묶음으로 다시 보내는 경우
    "이 라인은 이미 처리됐다"는 신호와 실제 부분 수량이 어긋날 수 있어, 추측 대신
    명시적으로 차단한다(전송 자체를 막음). 네이버 등 배송묶음 개념이 없는 채널은
    해당하지 않는다(라인 단위 발송 API가 수량 없이도 완결된다)."""

    def __init__(self, order_item_id: int) -> None:
        self.order_item_id = order_item_id
        super().__init__(
            f"배송묶음(box) 단위로만 추적되는 라인의 부분 수량 발송은 지원하지 않습니다: "
            f"order_item_id={order_item_id}"
        )


@dataclass
class ShipmentDispatchOutcome:
    command: ExternalCommand
    already_processed: bool  # True면 idempotency로 기존 SUCCESS 결과를 재사용(신규 API 호출 없음)


def _retry_backoff(attempt_count: int) -> timedelta:
    idx = min(max(attempt_count, 1), len(RETRY_BACKOFF_MINUTES)) - 1
    return timedelta(minutes=RETRY_BACKOFF_MINUTES[idx])


def _classify_write_outcome(exc: Exception) -> str:
    """ "SAFE_RETRY" / "CONFIRMED_FAILED" / "UNKNOWN" 중 하나를 반환한다(모듈 docstring 참고).

    송장 전송처럼 부작용이 있는 쓰기 요청 전용 분류다 - 기존 수집(읽기) 경로가 쓰는
    MarketplaceExternalAPIError.retryable을 그대로 신뢰하지 않는다."""
    if isinstance(exc, (ShipmentSubmitRejectedError, ShipmentBoxQuantityAmbiguousError)):
        return "CONFIRMED_FAILED"  # 채널의 명시적 거부, 또는 애초에 전송하지 않고 차단한 경우.
    if isinstance(
        exc,
        (
            MarketplaceCredentialMissingError,
            MarketplaceCapabilityUnsupportedError,
            UnknownCarrierError,
            ShipmentNotReadyError,
            ShipmentPlatformMismatchError,
            ValueError,
        ),
    ):
        return "CONFIRMED_FAILED"  # 채널 커넥터 호출 이전 단계에서 막힘 - 전송 자체가 없었다.
    if isinstance(exc, MarketplaceExternalAPIError):
        if exc.reason_code in _SAFE_RETRY_REASON_CODES:
            return "SAFE_RETRY"
        if exc.reason_code in _CONFIRMED_FAILED_REASON_CODES:
            return "CONFIRMED_FAILED"
        return "UNKNOWN"  # TIMEOUT/TRANSPORT_ERROR/SERVER_ERROR/PARSE_FAILED/BAD_RESPONSE 등.
    return "UNKNOWN"  # 예상 밖 예외 - 안전한 기본값.


class ShipmentDispatchService:
    def __init__(self, session: Session, connector_factory: ConnectorFactory = get_mall_connector) -> None:
        self.session = session
        self.connector_factory = connector_factory
        self.shipment_repo = ShipmentRepository(session)
        self.order_repo = OrderRepository(session)
        self.platform_repo = PlatformRepository(session)
        self.command_repo = ExternalCommandRepository(session)
        self.line_result_repo = ExternalCommandLineResultRepository(session)
        self.channel_sync_service = OrderChannelSyncService(session)

    def submit_many(self, shipment_ids: list[int], dispatch_date: Optional[date] = None) -> list[dict[str, Any]]:
        """대량(일괄) 전송 - 한 건 실패가 나머지 건을 막지 않는다.

        각 건을 SAVEPOINT(session.begin_nested())로 감싸 독립 처리한다. 아직 이 메서드를
        호출하는 API 엔드포인트는 없다(6단계에서 API/outbox worker 경로로 옮긴다)."""
        results: list[dict[str, Any]] = []
        for shipment_id in shipment_ids:
            savepoint = self.session.begin_nested()
            try:
                outcome = self.submit(shipment_id, dispatch_date)
                savepoint.commit()
                results.append(
                    {
                        "shipment_id": shipment_id,
                        "success": True,
                        "command_id": outcome.command.id,
                        "already_processed": outcome.already_processed,
                    }
                )
            except Exception as e:  # noqa: BLE001 - 한 건의 예상 밖 실패도 격리해야 한다.
                savepoint.rollback()
                results.append({"shipment_id": shipment_id, "success": False, "error": type(e).__name__})
        return results

    def enqueue(self, shipment_id: int) -> ShipmentDispatchOutcome:
        """API가 호출하는 진입점 - READY/플랫폼단일성 검증 후 PENDING 명령을 멱등하게
        생성/재사용한다. 채널 HTTP 호출은 하지 않는다(outbox_dispatch_job이 나중에
        execute_command()로 수행). settings.shipment_channel_submit_enabled가 False면
        아무 것도 만들지 않고 즉시 차단한다(모듈 docstring 참고)."""
        if not settings.shipment_channel_submit_enabled:
            raise ShipmentChannelSubmitDisabledError(
                "채널 전송 기능이 비활성화(OFF) 상태입니다 - 실계정 검증 승인 후 활성화해야 합니다."
            )
        shipment, _orders, platform = self._load_shipment_orders_platform(shipment_id)
        idempotency_key = self._idempotency_key(shipment)
        existing = self.command_repo.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return ShipmentDispatchOutcome(command=existing, already_processed=existing.status == "SUCCESS")

        command = self.command_repo.add(
            ExternalCommand(
                idempotency_key=idempotency_key,
                command_type="SHIPMENT_SUBMIT",
                platform_id=platform.id,
                platform_code=platform.code,
                target_type="SHIPMENT",
                target_id=shipment.id,
                status="PENDING",
                trace_id=uuid.uuid4().hex,
            )
        )
        return ShipmentDispatchOutcome(command=command, already_processed=False)

    def execute_command(self, command_id: int, dispatch_date: Optional[date] = None) -> ShipmentDispatchOutcome:
        """outbox worker(scheduler.jobs.outbox_dispatch_job)가 호출 - PENDING/RETRY_WAIT
        명령을 실제로 채널에 전송한다. claim()으로 원자적으로 선점하므로 두 worker가
        동시에 같은 명령을 실행할 수 없다(모듈 docstring "동시 실행 방지" 참고)."""
        command = self.command_repo.get_by_id(command_id)
        if command is None:
            raise ValueError(f"명령을 찾을 수 없습니다: command_id={command_id}")
        if command.status == "SUCCESS":
            return ShipmentDispatchOutcome(command=command, already_processed=True)

        lease_token = uuid.uuid4().hex
        if not self.command_repo.claim(command_id, lease_token):
            self.session.refresh(command)
            if command.status == "SUCCESS":
                return ShipmentDispatchOutcome(command=command, already_processed=True)
            if command.status == "RUNNING":
                raise ShipmentAlreadyRunningError(f"이미 처리 중인 전송 요청입니다: command_id={command_id}")
            raise ValueError(f"실행 대상이 아닌 상태입니다(status={command.status}): command_id={command_id}")
        self.session.refresh(command)

        try:
            # 배송/주문 상태 재검증도 이 try 안에서 한다 - enqueue() 이후 시간이 지나
            # 배송이 취소되는 등 상태가 바뀌었을 수 있다.
            shipment, orders, platform = self._load_shipment_orders_platform(command.target_id)
            connector = self.connector_factory(platform.connector_class, self.session, platform.id)
            self._dispatch_all_lines(connector, platform.code, shipment, command, dispatch_date or date.today())
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
            return ShipmentDispatchOutcome(command=command, already_processed=(command.status == "SUCCESS"))
        self._sync_channel_status_after_success(orders)
        return ShipmentDispatchOutcome(command=command, already_processed=False)

    def submit(self, shipment_id: int, dispatch_date: Optional[date] = None) -> ShipmentDispatchOutcome:
        """enqueue() + execute_command()를 그 자리에서 동기로 실행하는 편의 메서드
        (테스트/관리자 강제 즉시실행용). 실 API 라우터는 더 이상 이 메서드를 쓰지
        않는다 - POST /api/shipments/{id}/submit은 enqueue()만 호출한다."""
        outcome = self.enqueue(shipment_id)
        if outcome.already_processed:
            return outcome
        if outcome.command.status == "RUNNING":
            raise ShipmentAlreadyRunningError(f"이미 처리 중인 전송 요청입니다: shipment_id={shipment_id}")
        return self.execute_command(outcome.command.id, dispatch_date=dispatch_date)

    def recover_stale_running(self, command_type: str = "SHIPMENT_SUBMIT") -> int:
        """outbox_dispatch_job이 매 실행 시작 시 먼저 호출한다 - STALE_RUNNING_TIMEOUT_MINUTES
        이상 RUNNING에 머물러 있는 명령(worker 프로세스가 실행 도중 죽었다고 추정)을
        UNKNOWN으로 회수한다(PENDING이 아니다 - 채널에 실제로 도달했는지 알 수 없는 채로
        자동 재전송하면 안 된다). lease_token도 비워 이전 worker가 뒤늦게 끝나도 그
        결과로 이 상태를 덮어쓰지 못하게 한다. 회수한 건수를 반환한다."""
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
        """UNKNOWN(결과 확인 필요) 명령을 운영자가 채널을 직접 확인한 뒤 해소한다.

        resolution:
        - CONFIRMED_NOT_SENT: 채널에 실제로 반영되지 않았음을 확인 - PENDING으로 되돌려
          다음 outbox 주기에 재시도되게 한다.
        - CONFIRMED_SUCCESS: 채널에 실제로 반영됐음을 확인 - SUCCESS로 확정하고(중복
          전송 없이) 성공 후 상태 동기화 훅을 실행한다.
        - CONFIRMED_FAILED: 채널에 반영되지 않았고 재시도도 불필요함을 확인 - FAILED로
          확정한다.
        """
        if resolution not in ("CONFIRMED_NOT_SENT", "CONFIRMED_SUCCESS", "CONFIRMED_FAILED"):
            raise ValueError(f"알 수 없는 해소 방식입니다: {resolution}")
        command = self.command_repo.get_by_id(command_id)
        if command is None:
            raise ValueError(f"명령을 찾을 수 없습니다: command_id={command_id}")
        if command.status != "UNKNOWN":
            raise ValueError(f"결과 확인이 필요한(UNKNOWN) 명령만 해소할 수 있습니다(현재 상태: {command.status}).")

        before_status = command.status
        orders_to_sync: list[Order] = []
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
            _, orders_to_sync, _ = self._load_shipment_orders_platform(command.target_id)

        self.session.add(
            AuditLog(
                entity_type="EXTERNAL_COMMAND",
                entity_id=command.id,
                action="UPDATE",
                before_json=f'{{"status":"{before_status}"}}',
                after_json=f'{{"status":"{command.status}"}}',
                changed_by=resolved_by,
                changed_at=datetime.now(timezone.utc),
                command="shipment_command.resolve_unknown",
                reason=f"운영자가 채널을 직접 확인해 해소: {resolution}",
            )
        )
        self.session.flush()
        if orders_to_sync:
            self._sync_channel_status_after_success(orders_to_sync)
        return command

    def retry_failed_command(self, command_id: int) -> ExternalCommand:
        """실패(FAILED)로 확정된 송장 전송 명령을 운영자 요청으로 재처리 대상
        (PENDING)으로 되돌린다 - services.product_publish_service.
        ProductPublishService.retry_failed_command와 동일 원칙(정확히 같은
        요청의 반복). UNKNOWN(결과 확인 필요) 명령은 이 메서드로 재처리할 수
        없다 - 반드시 resolve_unknown_command()로 운영자가 채널을 직접 확인한
        뒤에만 해소해야 한다(결과를 모르는 채로 재전송하면 중복 등록 위험)."""
        command = self.command_repo.get_by_id(command_id)
        if command is None:
            raise ValueError(f"명령을 찾을 수 없습니다: command_id={command_id}")
        if command.target_type != "SHIPMENT":
            raise ValueError(f"이 서비스가 다루지 않는 명령입니다: command_id={command_id}")
        if command.status != "FAILED":
            raise ValueError(f"실패(FAILED) 상태인 명령만 재처리할 수 있습니다(현재 상태: {command.status}).")
        command.status = "PENDING"
        command.next_retry_at = None
        command.error_code = None
        command.retryable = False
        self.session.flush()
        return command

    @staticmethod
    def _idempotency_key(shipment: Shipment) -> str:
        return f"SHIPMENT_SUBMIT:{shipment.id}:{shipment.tracking_no}"

    def _load_shipment_orders_platform(self, shipment_id: int) -> tuple[Shipment, list[Order], Platform]:
        shipment = self.shipment_repo.get_by_id(shipment_id)
        if shipment is None:
            raise ValueError(f"배송 정보를 찾을 수 없습니다: shipment_id={shipment_id}")
        self._validate_ready(shipment)

        order_ids = self.shipment_repo.list_orders_of_shipment(shipment.id)
        orders = [o for o in (self.order_repo.get_by_id(oid) for oid in order_ids) if o is not None]
        if not orders:
            raise ShipmentNotReadyError("배송에 연결된 주문이 없습니다.")
        platform_ids = {o.platform_id for o in orders}
        if len(platform_ids) != 1:
            raise ShipmentPlatformMismatchError("같은 배송에 서로 다른 플랫폼의 주문이 섞여 있습니다(현재 미지원).")
        platform_id = platform_ids.pop()
        platform = self.platform_repo.get_by_id(platform_id)
        if platform is None:
            raise ValueError(f"플랫폼 정보를 찾을 수 없습니다: platform_id={platform_id}")
        return shipment, orders, platform

    @staticmethod
    def _validate_ready(shipment: Shipment) -> None:
        if shipment.status != "READY":
            raise ShipmentNotReadyError(f"READY 상태의 배송만 전송할 수 있습니다(현재: {shipment.status}).")
        if not shipment.carrier or not shipment.tracking_no:
            raise ShipmentNotReadyError("운송사/송장번호가 없습니다.")

    def _dispatch_all_lines(
        self,
        connector: BaseMallConnector,
        platform_code: str,
        shipment: Shipment,
        command: ExternalCommand,
        dispatch_date: date,
    ) -> None:
        assert shipment.carrier is not None and shipment.tracking_no is not None
        carrier_code = normalize_carrier_code(shipment.carrier, platform_code)
        already_success = self.line_result_repo.list_success_order_item_ids(command.id)

        order_cache: dict[int, Optional[Order]] = {}
        for shipment_item in shipment.items:
            order = order_cache.setdefault(shipment_item.order_id, self.order_repo.get_by_id(shipment_item.order_id))
            if order is None:
                continue
            # order_item_id가 있으면 부분출고(그 라인 하나만) - 없으면 이 주문의 라인 전체가
            # 이 배송(택배 1건)의 대상이다.
            if shipment_item.order_item_id is not None:
                line_items = [i for i in order.items if i.id == shipment_item.order_item_id]
            else:
                line_items = order.items
            for item in line_items:
                if item.platform_order_item_no is None:
                    # 상품주문번호가 없는(구채널/과거) 주문은 라인 단위 전송 대상이 아니다.
                    continue
                if item.id in already_success:
                    # 이전 시도(부분성공)에서 이미 성공 확인된 라인 - 재전송하지 않는다.
                    continue

                quantity = self._resolve_line_quantity(item, shipment_item)

                result = connector.submit_shipment(
                    platform_order_item_no=item.platform_order_item_no,
                    carrier_code=carrier_code,
                    tracking_no=shipment.tracking_no,
                    dispatch_date=dispatch_date,
                    platform_order_no=order.platform_order_no,
                    platform_shipment_box_id=item.platform_shipment_box_id,
                )
                if not result.accepted:
                    self.line_result_repo.record_result(
                        command_id=command.id,
                        order_item_id=item.id,
                        status="FAILED",
                        quantity=quantity,
                        result_code=result.platform_result_code,
                    )
                    # 실패를 성공으로 표시하지 않는다 - 안전한 코드만 예외 메시지에 담는다.
                    raise ShipmentSubmitRejectedError(platform_code, result.platform_result_code)
                self.line_result_repo.record_result(
                    command_id=command.id,
                    order_item_id=item.id,
                    status="SUCCESS",
                    quantity=quantity,
                    result_code=result.platform_result_code,
                )

    @staticmethod
    def _resolve_line_quantity(item: OrderItem, shipment_item: ShipmentItem) -> int:
        """이 (item, shipment_item) 조합이 커버하는 발송 수량을 정한다.

        order_item_id가 없는(주문 전체) shipment_item은 그 라인의 전체 수량을 커버한다.
        order_item_id가 있고 quantity도 그 라인의 전체 수량과 같으면(또는 quantity가
        비어 있으면, 즉 "이 라인 전체를 이 배송이 담당") 마찬가지로 전체 수량이다.
        그보다 적은 수량(진짜 부분출고)인데 이 라인이 배송묶음(box) 단위로만 추적되는
        채널(쿠팡)의 라인이면 명시적으로 차단한다 - box id만으로는 "몇 개가 이 박스에
        속하는지"를 표현할 수 없기 때문이다(모듈 docstring/ShipmentBoxQuantityAmbiguousError
        참고). 배송묶음 개념이 없는 채널(네이버 등)은 부분 수량이어도 안전하게 그
        수량만 이행으로 집계한다.
        """
        if shipment_item.order_item_id is None:
            return item.quantity
        if shipment_item.quantity is not None and shipment_item.quantity < item.quantity:
            if item.platform_shipment_box_id is not None:
                raise ShipmentBoxQuantityAmbiguousError(item.id)
            return shipment_item.quantity
        return item.quantity

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
            # 채널이 처리했을 가능성을 배제할 수 없다 - 자동 재시도 금지, 운영자 확인 필요.
            values = {"status": "UNKNOWN", "retryable": False, "error_code": error_code}
        else:
            # CONFIRMED_FAILED, 또는 SAFE_RETRY인데 재시도 소진 - 둘 다 채널 미반영이 확실하다.
            values = {"status": "FAILED", "retryable": False, "error_code": error_code}
        transitioned = self.command_repo.try_transition(command.id, lease_token, **values)
        self.session.refresh(command)
        if not transitioned:
            logger.warning("lease 소유권을 잃어 실패 기록을 건너뜁니다: command_id=%s", command.id)

    def _is_order_fully_dispatched(self, order: Order) -> bool:
        """주문의 모든(채널 라인 식별자가 있는) OrderItem이 발송 수량 합계 기준으로
        완전히 이행됐는지 확인한다 - 같은 OrderItem이 여러 Shipment로 나뉘어 부분
        발송되는 경우까지 정확히 집계한다(라인 "존재" 여부가 아니라 수량 합계 기준)."""
        for item in order.items:
            if item.platform_order_item_no is None:
                continue
            if self.line_result_repo.sum_success_quantity(item.id) < item.quantity:
                return False
        return True

    def _sync_channel_status_after_success(self, orders: list[Order]) -> None:
        """송장 전송이 채널에 성공적으로 접수된 직후, 주문이 완전히 이행됐을 때만 그
        사실을 SHIPPING으로 반영한다(모듈 docstring "부분출고와 주문 전체 상태" 참고) -
        일부만 발송된 주문은 여기서 상태를 건드리지 않는다. 허용되지 않는 전이면
        다른 경로와 동일하게 OrderStatusConflict로 남긴다."""
        for order in orders:
            if self._is_order_fully_dispatched(order):
                self.channel_sync_service.sync_channel_status(order, "SHIPPING")


class ShipmentSubmitRejectedError(MarketplaceError):
    """채널이 명시적으로 거부(실패)를 응답했다 - 원본 응답 전문은 담지 않는다."""

    def __init__(self, marketplace_code: str, result_code: Optional[str]) -> None:
        self.marketplace_code = marketplace_code
        self.reason_code = result_code or "REJECTED"
        super().__init__(f"{marketplace_code}: 송장 전송이 거부되었습니다(code={self.reason_code}).")
