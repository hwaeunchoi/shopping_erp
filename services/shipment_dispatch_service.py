"""
services/shipment_dispatch_service.py
------------------------------------------
Shipment(내부 배송 레코드)를 실제 채널(네이버/쿠팡)에 전송한다.

전송 전 내부 출고 상태(READY)를 검증하고, 성공을 채널이 확인한 뒤에만
Shipment/ExternalCommand를 SUCCESS로 반영한다 - 실패를 성공으로 표시하지
않는다. 동일 (shipment_id, tracking_no) 조합은 idempotency_key로 묶여
재전송해도 새 API 호출을 만들지 않고 기존 결과를 재사용한다.

한 채널 전송 실패가 다른 채널/다른 배송 처리를 막지 않도록, submit_many()는
건별로 SAVEPOINT(session.begin_nested())로 감싸 독립 처리한다.

carrier 표기 규약: Shipment.carrier는 자유 텍스트가 아니라
integrations.malls.carrier_codes에 등록된 내부 표준 코드(예: "CJ_LOGISTICS")를
저장해야 한다 - 화면에 보여줄 한글 이름("CJ대한통운")은 UI 레이어의 표시
전용 매핑이며, 저장값은 채널 무관 표준 코드로 고정한다(운영자가 임의
문자열을 입력해도 채널별 코드로 추측 변환하지 않기 위함). 등록되지 않은
코드는 UnknownCarrierError로 안전하게 거부된다.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from integrations.malls import get_mall_connector
from integrations.malls.base_mall_connector import BaseMallConnector
from integrations.malls.carrier_codes import UnknownCarrierError, normalize_carrier_code
from integrations.malls.errors import MarketplaceError
from models.integration_sync import ExternalCommand
from models.order import Order, Shipment
from repositories.integration_sync_repository import ExternalCommandRepository
from repositories.order_repository import OrderRepository, ShipmentRepository
from repositories.platform_repository import PlatformRepository

ConnectorFactory = Callable[[str, Any, Optional[int]], BaseMallConnector]


class ShipmentNotReadyError(Exception):
    """배송이 READY 상태가 아니거나 필수 정보(운송사/송장번호)가 없어 전송할 수 없다."""


class ShipmentPlatformMismatchError(Exception):
    """같은 배송(합포장)에 서로 다른 플랫폼의 주문이 섞여 있어 단일 채널로 전송할 수 없다."""


class ShipmentAlreadyRunningError(Exception):
    """같은 idempotency_key로 이미 처리 중인 요청이 있다."""


@dataclass
class ShipmentDispatchOutcome:
    command: ExternalCommand
    already_processed: bool  # True면 idempotency로 기존 SUCCESS 결과를 재사용(신규 API 호출 없음)


class ShipmentDispatchService:
    def __init__(self, session: Session, connector_factory: ConnectorFactory = get_mall_connector) -> None:
        self.session = session
        self.connector_factory = connector_factory
        self.shipment_repo = ShipmentRepository(session)
        self.order_repo = OrderRepository(session)
        self.platform_repo = PlatformRepository(session)
        self.command_repo = ExternalCommandRepository(session)

    def submit_many(self, shipment_ids: list[int], dispatch_date: Optional[date] = None) -> list[dict[str, Any]]:
        """대량(일괄) 전송 - 한 건 실패가 나머지 건을 막지 않는다.

        각 건을 SAVEPOINT(session.begin_nested())로 감싸 독립 처리한다 -
        한 건의 실패로 인한 rollback이 그 건의 변경만 되돌리고, 이미 커밋 대기
        중인 다른 건의 변경에는 영향을 주지 않는다. 부분 성공/부분 실패 목록을
        그대로 반환한다(6단계 대량처리 UI가 이 결과를 그대로 소비할 수 있게
        설계했다)."""
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

    def submit(self, shipment_id: int, dispatch_date: Optional[date] = None) -> ShipmentDispatchOutcome:
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

        idempotency_key = f"SHIPMENT_SUBMIT:{shipment.id}:{shipment.tracking_no}"
        existing = self.command_repo.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            if existing.status == "SUCCESS":
                return ShipmentDispatchOutcome(command=existing, already_processed=True)
            if existing.status == "RUNNING":
                raise ShipmentAlreadyRunningError(f"이미 처리 중인 전송 요청입니다: shipment_id={shipment.id}")

        command = existing or self.command_repo.add(
            ExternalCommand(
                idempotency_key=idempotency_key,
                command_type="SHIPMENT_SUBMIT",
                platform_id=platform_id,
                platform_code=platform.code,
                target_type="SHIPMENT",
                target_id=shipment.id,
                status="PENDING",
                trace_id=uuid.uuid4().hex,
            )
        )
        command.status = "RUNNING"
        command.attempt_count += 1
        self.session.flush()

        try:
            connector = self.connector_factory(platform.connector_class, self.session, platform_id)
            self._dispatch_all_lines(connector, platform.code, orders, shipment, dispatch_date or date.today())
        except MarketplaceError as e:
            self._mark_failed(command, e)
            raise
        except Exception as e:  # noqa: BLE001 - 예상 밖 예외도 실패로 안전하게 기록하고 다시 던진다.
            self._mark_failed(command, e)
            raise

        command.status = "SUCCESS"
        command.response_summary = "채널 확인 완료"
        command.completed_at = datetime.now(timezone.utc)
        self.session.flush()
        return ShipmentDispatchOutcome(command=command, already_processed=False)

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
        orders: list[Order],
        shipment: Shipment,
        dispatch_date: date,
    ) -> None:
        assert shipment.carrier is not None and shipment.tracking_no is not None
        try:
            carrier_code = normalize_carrier_code(shipment.carrier, platform_code)
        except UnknownCarrierError:
            raise

        for order in orders:
            for item in order.items:
                if item.platform_order_item_no is None:
                    # 상품주문번호가 없는(구채널/과거) 주문은 라인 단위 전송 대상이 아니다.
                    continue
                result = connector.submit_shipment(
                    platform_order_item_no=item.platform_order_item_no,
                    carrier_code=carrier_code,
                    tracking_no=shipment.tracking_no,
                    dispatch_date=dispatch_date,
                    platform_order_no=order.platform_order_no,
                    platform_shipment_box_id=order.platform_shipment_box_id,
                )
                if not result.accepted:
                    # 실패를 성공으로 표시하지 않는다 - 안전한 코드만 예외 메시지에 담는다.
                    raise ShipmentSubmitRejectedError(platform_code, result.platform_result_code)

    def _mark_failed(self, command: ExternalCommand, exc: Exception) -> None:
        command.status = "FAILED"
        retryable = getattr(exc, "retryable", None)
        command.retryable = bool(retryable) if retryable is not None else False
        command.error_code = getattr(exc, "reason_code", None) or type(exc).__name__
        self.session.flush()


class ShipmentSubmitRejectedError(MarketplaceError):
    """채널이 명시적으로 거부(실패)를 응답했다 - 원본 응답 전문은 담지 않는다."""

    def __init__(self, marketplace_code: str, result_code: Optional[str]) -> None:
        self.marketplace_code = marketplace_code
        self.reason_code = result_code or "REJECTED"
        super().__init__(f"{marketplace_code}: 송장 전송이 거부되었습니다(code={self.reason_code}).")
