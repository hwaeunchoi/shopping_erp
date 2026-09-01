"""
services/shipment_dispatch_service.py
------------------------------------------
Shipment(내부 배송 레코드)를 실제 채널(네이버/쿠팡)에 전송한다.

outbox 실행 모델(비동기):
- API(POST /api/shipments/{id}/submit)는 enqueue()만 호출한다 - 채널 HTTP 호출을
  API 요청 스레드에서 동기 실행하지 않는다(외부 API 지연이 그대로 API 응답시간에
  전가되지 않도록). enqueue()는 PENDING ExternalCommand를 멱등하게 만들거나(이미
  있으면 그대로 재사용) 즉시 반환한다 - 버튼 연타로 새 명령이 중복 생성되지 않는다.
- 실제 채널 호출은 scheduler.jobs.outbox_dispatch_job이 주기적으로 due한 명령마다
  execute_command()를 호출해 수행한다. 화면은 command_id로 상태(PENDING/RUNNING/
  SUCCESS/FAILED/RETRY_WAIT)를 조회(GET /api/shipments/commands/{id})해 폴링하고,
  SUCCESS를 확인한 뒤에만 성공으로 표시해야 한다.
- submit()은 enqueue()+execute_command()를 그 자리에서 동기 실행하는 편의 메서드로
  남겨둔다(테스트/관리자 강제 즉시실행용) - 실 API 경로는 더 이상 이 메서드를 쓰지 않는다.

재시도 정책:
- MarketplaceExternalAPIError(retryable=True)이고 attempt_count가 MAX_ATTEMPTS
  미만이면 RETRY_WAIT(next_retry_at = now + 백오프)로 전환해 다음 주기의 worker가
  다시 시도하게 한다. 재시도가 소진되었거나 애초에 재시도 불가능한 실패(자격증명
  없음/미지원 기능/채널의 명시적 거부)는 FAILED로 확정한다 - 이 경우는 사람의 조치
  (설정 수정/송장정보 정정)가 필요하므로 자동 재시도 대상이 아니다.
- RUNNING으로 STALE_RUNNING_TIMEOUT_MINUTES 이상 머물러 있으면(worker 프로세스가
  실행 도중 죽었다고 추정) outbox_dispatch_job이 시작할 때마다 먼저 PENDING으로
  회수한다. 이 경우 채널에 실제로 요청이 도달했는지 확신할 수 없는 채로 재시도하게
  되지만, 동일 (shipment_id, tracking_no)에 대한 송장 전송은 채널 입장에서 같은
  송장번호를 다시 제출하는 것(발송정보 upsert)과 같아 중복 실행이 안전하다고
  간주한다 - 완전한 exactly-once 보장은 채널이 자체 요청ID를 지원해야 가능하며,
  이는 실계정 검증 후 재검토 대상이다(docs/COMMERCIAL_ERP_ROADMAP.md 참고).

송장 전송 성공 후 상태 동기화: execute_command()가 SUCCESS로 확정하면 이 배송에
연결된 모든 주문에 대해 OrderChannelSyncService.sync_channel_status(order,
"SHIPPING")을 호출한다 - 채널이 방금 우리의 발송 요청을 승인했다는 사실 자체가
"채널이 SHIPPING으로 인식했다"는 가장 강한 확인이므로, 별도 재조회 호출 없이 그
사실을 상태머신을 거쳐 반영한다(허용되지 않는 전이면 다른 경로와 동일하게
OrderStatusConflict로 남긴다 - 자동 덮어쓰기 없음).

분할배송/부분출고: Shipment는 shipment_items를 통해 Order/OrderItem과 N:M으로
연결된다. ShipmentItem.order_item_id가 있으면 그 라인 하나만(부분출고), 없으면
그 주문의 라인 전체를 이 배송의 대상으로 본다 - _dispatch_all_lines()는 반드시
shipment.items를 거쳐 대상 라인을 결정하며, order.items 전체를 무조건 순회하지
않는다(그렇게 하면 부분출고 배송이 아직 발송되지 않은 다른 라인까지 잘못
전송하게 된다).

carrier 표기 규약: Shipment.carrier는 자유 텍스트가 아니라
integrations.malls.carrier_codes에 등록된 내부 표준 코드(예: "CJ_LOGISTICS")를
저장해야 한다 - 화면에 보여줄 한글 이름("CJ대한통운")은 UI 레이어의 표시
전용 매핑이며, 저장값은 채널 무관 표준 코드로 고정한다(운영자가 임의
문자열을 입력해도 채널별 코드로 추측 변환하지 않기 위함). 등록되지 않은
코드는 UnknownCarrierError로 안전하게 거부된다.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from integrations.malls import get_mall_connector
from integrations.malls.base_mall_connector import BaseMallConnector
from integrations.malls.carrier_codes import normalize_carrier_code
from integrations.malls.errors import MarketplaceError
from models.integration_sync import ExternalCommand
from models.order import Order, Shipment
from models.platform import Platform
from repositories.integration_sync_repository import ExternalCommandRepository
from repositories.order_repository import OrderRepository, ShipmentRepository
from repositories.platform_repository import PlatformRepository
from services.order_channel_sync_service import OrderChannelSyncService

ConnectorFactory = Callable[[str, Any, Optional[int]], BaseMallConnector]

# 재시도 가능한 실패의 최대 시도 횟수(이후에는 FAILED로 확정, 자동 재시도 중단).
MAX_ATTEMPTS = 5
# 시도 횟수(1-based)별 재시도 대기 시간(분) - 마지막 값을 넘는 시도는 마지막 값을 그대로 쓴다.
RETRY_BACKOFF_MINUTES = [2, 5, 15, 30, 60]
# RUNNING으로 이보다 오래 머물러 있으면 worker 프로세스가 죽었다고 보고 회수한다.
STALE_RUNNING_TIMEOUT_MINUTES = 15


class ShipmentNotReadyError(Exception):
    """배송이 READY 상태가 아니거나 필수 정보(운송사/송장번호)가 없어 전송할 수 없다."""


class ShipmentPlatformMismatchError(Exception):
    """같은 배송(합포장)에 서로 다른 플랫폼의 주문이 섞여 있어 단일 채널로 전송할 수 없다."""


class ShipmentAlreadyRunningError(Exception):
    """같은 명령이 이미 RUNNING(다른 worker가 처리 중)이다."""


@dataclass
class ShipmentDispatchOutcome:
    command: ExternalCommand
    already_processed: bool  # True면 idempotency로 기존 SUCCESS 결과를 재사용(신규 API 호출 없음)


def _retry_backoff(attempt_count: int) -> timedelta:
    idx = min(max(attempt_count, 1), len(RETRY_BACKOFF_MINUTES)) - 1
    return timedelta(minutes=RETRY_BACKOFF_MINUTES[idx])


class ShipmentDispatchService:
    def __init__(self, session: Session, connector_factory: ConnectorFactory = get_mall_connector) -> None:
        self.session = session
        self.connector_factory = connector_factory
        self.shipment_repo = ShipmentRepository(session)
        self.order_repo = OrderRepository(session)
        self.platform_repo = PlatformRepository(session)
        self.command_repo = ExternalCommandRepository(session)
        self.channel_sync_service = OrderChannelSyncService(session)

    def submit_many(self, shipment_ids: list[int], dispatch_date: Optional[date] = None) -> list[dict[str, Any]]:
        """대량(일괄) 전송 - 한 건 실패가 나머지 건을 막지 않는다.

        각 건을 SAVEPOINT(session.begin_nested())로 감싸 독립 처리한다 -
        한 건의 실패로 인한 rollback이 그 건의 변경만 되돌리고, 이미 커밋 대기
        중인 다른 건의 변경에는 영향을 주지 않는다. 부분 성공/부분 실패 목록을
        그대로 반환한다(6단계 대량처리 UI가 이 결과를 그대로 소비할 수 있게
        설계했다). 아직 이 메서드를 호출하는 API 엔드포인트는 없다(동기 실행 편의
        메서드 submit()을 그대로 사용 - 6단계에서 API/outbox worker 경로로 옮긴다)."""
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
        execute_command()로 수행)."""
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
        """outbox worker(scheduler.jobs.outbox_dispatch_job)가 호출 - PENDING/RETRY_WAIT/
        FAILED(운영자가 원인을 고친 뒤 수동 재시도) 명령을 실제로 채널에 전송한다.

        이미 SUCCESS면 그대로 반환(재실행 없음). RUNNING(다른 worker가 처리 중)이면
        ShipmentAlreadyRunningError로 겹쳐 실행하지 않는다."""
        command = self.command_repo.get_by_id(command_id)
        if command is None:
            raise ValueError(f"명령을 찾을 수 없습니다: command_id={command_id}")
        if command.status == "SUCCESS":
            return ShipmentDispatchOutcome(command=command, already_processed=True)
        if command.status == "RUNNING":
            raise ShipmentAlreadyRunningError(f"이미 처리 중인 전송 요청입니다: command_id={command_id}")

        command.status = "RUNNING"
        command.attempt_count += 1
        self.session.flush()

        try:
            # 배송/주문 상태 재검증도 이 try 안에서 한다 - enqueue() 이후 시간이 지나
            # 배송이 취소되는 등 상태가 바뀌었을 수 있고(worker는 비동기로 나중에 실행),
            # 이런 검증 실패도 채널 호출 실패와 동일하게 outbox에 안전히 기록해야
            # "재시도해도 매번 같은 이유로 실패"하는 명령이 조용히 매 주기 반복되지 않는다.
            shipment, orders, platform = self._load_shipment_orders_platform(command.target_id)
            connector = self.connector_factory(platform.connector_class, self.session, platform.id)
            self._dispatch_all_lines(connector, platform.code, shipment, dispatch_date or date.today())
        except MarketplaceError as e:
            self._mark_failed_or_retry(command, e)
            raise
        except Exception as e:  # noqa: BLE001 - 예상 밖 예외도 실패로 안전하게 기록하고 다시 던진다.
            self._mark_failed_or_retry(command, e)
            raise

        command.status = "SUCCESS"
        command.response_summary = "채널 확인 완료"
        command.completed_at = datetime.now(timezone.utc)
        self.session.flush()
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
        PENDING으로 되돌려 다음 실행에서 다시 시도되게 한다. 회수한 건수를 반환한다."""
        threshold = datetime.now(timezone.utc) - timedelta(minutes=STALE_RUNNING_TIMEOUT_MINUTES)
        stale = self.command_repo.list_stale_running(command_type, threshold)
        for command in stale:
            command.status = "PENDING"
        if stale:
            self.session.flush()
        return len(stale)

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
        self, connector: BaseMallConnector, platform_code: str, shipment: Shipment, dispatch_date: date
    ) -> None:
        assert shipment.carrier is not None and shipment.tracking_no is not None
        carrier_code = normalize_carrier_code(shipment.carrier, platform_code)

        order_cache: dict[int, Optional[Order]] = {}
        for shipment_item in shipment.items:
            order = order_cache.setdefault(shipment_item.order_id, self.order_repo.get_by_id(shipment_item.order_id))
            if order is None:
                continue
            # order_item_id가 있으면 부분출고(그 라인 하나만) - 없으면 이 주문의 라인 전체가
            # 이 배송(택배 1건)의 대상이다. order.items 전체를 무조건 순회하지 않는다 -
            # 그러면 부분출고 배송이 아직 발송 안 된 다른 라인까지 잘못 전송하게 된다.
            if shipment_item.order_item_id is not None:
                line_items = [i for i in order.items if i.id == shipment_item.order_item_id]
            else:
                line_items = order.items
            for item in line_items:
                if item.platform_order_item_no is None:
                    # 상품주문번호가 없는(구채널/과거) 주문은 라인 단위 전송 대상이 아니다.
                    continue
                result = connector.submit_shipment(
                    platform_order_item_no=item.platform_order_item_no,
                    carrier_code=carrier_code,
                    tracking_no=shipment.tracking_no,
                    dispatch_date=dispatch_date,
                    platform_order_no=order.platform_order_no,
                    platform_shipment_box_id=item.platform_shipment_box_id,
                )
                if not result.accepted:
                    # 실패를 성공으로 표시하지 않는다 - 안전한 코드만 예외 메시지에 담는다.
                    raise ShipmentSubmitRejectedError(platform_code, result.platform_result_code)

    def _mark_failed_or_retry(self, command: ExternalCommand, exc: Exception) -> None:
        retryable = bool(getattr(exc, "retryable", None))
        command.error_code = getattr(exc, "reason_code", None) or type(exc).__name__
        if retryable and command.attempt_count < MAX_ATTEMPTS:
            command.status = "RETRY_WAIT"
            command.retryable = True
            command.next_retry_at = datetime.now(timezone.utc) + _retry_backoff(command.attempt_count)
        else:
            # 재시도 불가능한 실패(자격증명 없음/미지원/채널의 명시적 거부) 또는 재시도
            # 소진 - 자동 재시도 대상에서 제외한다(사람의 조치가 필요).
            command.status = "FAILED"
            command.retryable = False
        self.session.flush()

    def _sync_channel_status_after_success(self, orders: list[Order]) -> None:
        """송장 전송이 채널에 성공적으로 접수된 직후, 그 사실 자체를 SHIPPING으로 반영한다
        (모듈 docstring 참고) - 허용되지 않는 전이면 자동 반영 대신 충돌로 남긴다."""
        for order in orders:
            self.channel_sync_service.sync_channel_status(order, "SHIPPING")


class ShipmentSubmitRejectedError(MarketplaceError):
    """채널이 명시적으로 거부(실패)를 응답했다 - 원본 응답 전문은 담지 않는다."""

    def __init__(self, marketplace_code: str, result_code: Optional[str]) -> None:
        self.marketplace_code = marketplace_code
        self.reason_code = result_code or "REJECTED"
        super().__init__(f"{marketplace_code}: 송장 전송이 거부되었습니다(code={self.reason_code}).")
