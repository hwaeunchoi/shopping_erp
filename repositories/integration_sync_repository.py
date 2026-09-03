"""
repositories/integration_sync_repository.py
--------------------------------------------------
ExternalCommand(outbox)/ExternalCommandLineResult(라인별 결과)/OrderStatusConflict 저장소.
"""

from datetime import datetime, timezone
from typing import Any, Iterable, Optional, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from models.integration_sync import (
    ExternalCommand,
    ExternalCommandLineResult,
    OrderStatusConflict,
    ProductSyncCommandDetail,
)


class ExternalCommandRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[ExternalCommand]:
        return self.session.execute(
            select(ExternalCommand).where(ExternalCommand.idempotency_key == idempotency_key)
        ).scalar_one_or_none()

    def add(self, command: ExternalCommand) -> ExternalCommand:
        self.session.add(command)
        self.session.flush()
        return command

    def get_by_id(self, command_id: int) -> Optional[ExternalCommand]:
        return self.session.get(ExternalCommand, command_id)

    def list_retryable_due(self, now: Optional[datetime] = None) -> list[ExternalCommand]:
        """자동 백오프 재처리 대상(6단계에서 사용) - RETRY_WAIT이고 next_retry_at이 지난 것."""
        now = now or datetime.now(timezone.utc)
        return list(
            self.session.execute(
                select(ExternalCommand).where(
                    ExternalCommand.status == "RETRY_WAIT",
                    ExternalCommand.next_retry_at.is_not(None),
                    ExternalCommand.next_retry_at <= now,
                )
            ).scalars()
        )

    def list_due_for_execution(self, command_type: str, now: Optional[datetime] = None) -> list[ExternalCommand]:
        """outbox worker(scheduler.jobs.outbox_dispatch_job)가 실행할 대상 - PENDING(항상 즉시
        대상) + RETRY_WAIT인데 next_retry_at이 지난 것. command_type으로 범위를 좁힌다
        (예: "SHIPMENT_SUBMIT")."""
        now = now or datetime.now(timezone.utc)
        return list(
            self.session.execute(
                select(ExternalCommand)
                .where(
                    ExternalCommand.command_type == command_type,
                    (ExternalCommand.status == "PENDING")
                    | (
                        (ExternalCommand.status == "RETRY_WAIT")
                        & ExternalCommand.next_retry_at.is_not(None)
                        & (ExternalCommand.next_retry_at <= now)
                    ),
                )
                .order_by(ExternalCommand.id)
            ).scalars()
        )

    def list_stale_running(self, command_type: str, older_than: datetime) -> list[ExternalCommand]:
        """RUNNING 상태로 너무 오래 머물러 있는 명령(worker 프로세스가 실행 중 죽은 경우
        추정) - updated_at이 older_than보다 이전인 RUNNING 명령. worker가 시작할 때마다
        먼저 이 목록을 회수한다(services.shipment_dispatch_service.recover_stale_running -
        PENDING이 아니라 UNKNOWN으로 보낸다. 채널에 실제로 도달했는지 알 수 없는 채로
        자동 재전송하면 안 되기 때문)."""
        return list(
            self.session.execute(
                select(ExternalCommand).where(
                    ExternalCommand.command_type == command_type,
                    ExternalCommand.status == "RUNNING",
                    ExternalCommand.updated_at < older_than,
                )
            ).scalars()
        )

    def claim(self, command_id: int, lease_token: str) -> bool:
        """PENDING/RETRY_WAIT 명령을 RUNNING으로 원자적으로 선점(claim)한다.

        UPDATE ... WHERE status IN (...) 자체가 DB 레벨에서 원자적이므로, 두 worker가
        동시에 같은 명령을 claim 시도해도 정확히 하나만 성공한다(rowcount==1). 나머지
        worker는 rowcount==0을 받아 자신이 선점하지 못했음을 알 수 있다 - ORM 객체를
        먼저 읽고 나중에 쓰는 방식(select-then-update)은 두 조회 사이에 다른 worker가
        끼어들 수 있어(TOCTOU) 이 목적에 안전하지 않다."""
        stmt = (
            update(ExternalCommand)
            .where(ExternalCommand.id == command_id, ExternalCommand.status.in_(("PENDING", "RETRY_WAIT")))
            .values(status="RUNNING", lease_token=lease_token, attempt_count=ExternalCommand.attempt_count + 1)
        )
        result = cast(CursorResult, self.session.execute(stmt))
        return result.rowcount == 1

    def list_active_for_target(
        self, command_type: str, target_type: str, target_ids: Iterable[int] | int
    ) -> list[ExternalCommand]:
        """같은 대상(target_type+target_ids)·같은 명령종류에 대해 아직 종료되지 않은
        (PENDING/RETRY_WAIT/RUNNING) 명령을 전부 찾는다 - 새로운 목표값이 들어왔을 때
        낡은 명령을 취소 대상으로 찾는 데 쓴다(services.product_sync_dispatch_service
        참고). list_due_for_execution과 달리 next_retry_at이 아직 지나지 않은
        RETRY_WAIT도 포함한다(대기 중이어도 낡은 값이면 취소해야 하므로).

        target_ids는 단일 target_id 또는 그 목록을 받는다 - 서로 다른 내부 대상이
        같은 외부 대상(예: 네이버 원상품번호)을 공유하는 경우, 호출부가 그 전부를
        모아 넘긴다(services.product_sync_dispatch_service._resolve_contention_target_ids
        참고)."""
        ids = [target_ids] if isinstance(target_ids, int) else list(target_ids)
        return list(
            self.session.execute(
                select(ExternalCommand).where(
                    ExternalCommand.command_type == command_type,
                    ExternalCommand.target_type == target_type,
                    ExternalCommand.target_id.in_(ids),
                    ExternalCommand.status.in_(("PENDING", "RETRY_WAIT", "RUNNING")),
                )
            ).scalars()
        )

    def exists_unresolved_unknown_predecessor(
        self, command_type: str, target_type: str, target_ids: Iterable[int] | int, before_id: int
    ) -> bool:
        """같은 대상(들)·같은 명령종류에 대해 이 명령(before_id)보다 먼저 생성된(id가
        더 작은) UNKNOWN(결과 확인 필요) 명령이 있는지 확인한다 - 채널이 그 이전
        명령을 실제로 처리했는지 알 수 없는 채로 새 명령을 실행하면, 이후 UNKNOWN이
        실은 "이미 처리됨"으로 확인될 경우 두 요청이 뒤섞여 어떤 값이 실제로
        반영됐는지 알 수 없게 된다 - 그래서 UNKNOWN이 해소되기 전에는 새 명령을
        실행하지 않는다(services.product_sync_dispatch_service.execute_command 참고).
        target_ids는 list_active_for_target과 동일하게 단일값 또는 목록을 받는다."""
        ids = [target_ids] if isinstance(target_ids, int) else list(target_ids)
        stmt = select(func.count()).where(
            ExternalCommand.command_type == command_type,
            ExternalCommand.target_type == target_type,
            ExternalCommand.target_id.in_(ids),
            ExternalCommand.id < before_id,
            ExternalCommand.status == "UNKNOWN",
        )
        return int(self.session.execute(stmt).scalar_one()) > 0

    def exists_newer_command_for_target(
        self, command_type: str, target_type: str, target_ids: Iterable[int] | int, after_id: int
    ) -> bool:
        """같은 대상(들)·같은 명령종류(command_type)에 대해 이 명령(after_id)보다
        나중에 생성된(id가 더 큰) 다른 명령이 있는지 확인한다 - 오래된 명령이
        나중에 실행되어 최신 목표값을 덮어쓰지 않도록 실행 직전에 확인하는
        근거다(services.product_sync_dispatch_service 참고). CANCELLED는 더 이상
        진행되지 않을 것이 확정된 명령이라 "더 최신"으로 치지 않는다. target_ids는
        list_active_for_target과 동일하게 단일값 또는 목록을 받는다(서로 다른 내부
        매핑이 같은 외부 대상을 공유하는 경우 그 전부를 하나의 대상으로 취급)."""
        ids = [target_ids] if isinstance(target_ids, int) else list(target_ids)
        stmt = select(func.count()).where(
            ExternalCommand.command_type == command_type,
            ExternalCommand.target_type == target_type,
            ExternalCommand.target_id.in_(ids),
            ExternalCommand.id > after_id,
            ExternalCommand.status != "CANCELLED",
        )
        return int(self.session.execute(stmt).scalar_one()) > 0

    def exists_other_running_for_targets(
        self, target_type: str, target_ids: Iterable[int] | int, exclude_command_id: int
    ) -> bool:
        """같은 대상(들)에 대해 **명령종류를 가리지 않고** 지금 RUNNING인 다른 명령이
        있는지 확인한다 - list_active_for_target류와 달리 command_type으로 좁히지
        않는다: 재고 변경과 판매상태 변경은 취소 대상으로는 서로 독립이어야 하지만
        (다른 종류의 대기 명령을 잘못 취소하면 안 됨), 네이버처럼 "현재 상태 조회 ->
        변경 요청"을 하나의 채널 API로 묶어 처리하는 채널에서는 두 종류가 같은
        외부 대상에 대해 동시에 채널로 나가면 서로의 조회 결과를 덮어쓸 위험이
        있다(services.product_sync_dispatch_service 모듈 docstring 참고) - 그래서
        "지금 실제로 채널을 호출 중인가"만큼은 명령종류와 무관하게 상호 배제한다."""
        ids = [target_ids] if isinstance(target_ids, int) else list(target_ids)
        stmt = select(func.count()).where(
            ExternalCommand.target_type == target_type,
            ExternalCommand.target_id.in_(ids),
            ExternalCommand.status == "RUNNING",
            ExternalCommand.id != exclude_command_id,
        )
        return int(self.session.execute(stmt).scalar_one()) > 0

    def try_transition(self, command_id: int, expected_lease_token: str, **values: Any) -> bool:
        """lease_token이 아직 자신의 것일 때만 원자적으로 최종 상태를 반영한다(소유권 검증).

        recover_stale_running()이 그 사이 이 명령을 회수(lease_token을 바꾸거나 비움)했다면
        여기서 rowcount==0이 되어 갱신되지 않는다 - 소유권을 잃은 worker가 뒤늦게 끝나도
        다른 worker/회수 절차의 결과를 덮어쓰지 못하게 하기 위함이다."""
        stmt = (
            update(ExternalCommand)
            .where(ExternalCommand.id == command_id, ExternalCommand.lease_token == expected_lease_token)
            .values(**values)
        )
        result = cast(CursorResult, self.session.execute(stmt))
        return result.rowcount == 1


class ExternalCommandLineResultRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record_result(
        self, command_id: int, order_item_id: int, status: str, quantity: int, result_code: Optional[str]
    ) -> ExternalCommandLineResult:
        """(command_id, order_item_id) 라인 결과를 기록한다 - 이미 이 조합의 행이 있으면
        (예: FAILED로 기록된 라인을 같은 명령으로 재시도) 새로 만들지 않고 갱신한다
        (uq_command_line_result 유니크 제약 위반 방지)."""
        stmt = select(ExternalCommandLineResult).where(
            ExternalCommandLineResult.command_id == command_id, ExternalCommandLineResult.order_item_id == order_item_id
        )
        existing = self.session.execute(stmt).scalar_one_or_none()
        if existing is not None:
            existing.status = status
            existing.quantity = quantity
            existing.result_code = result_code
            self.session.flush()
            return existing
        line_result = ExternalCommandLineResult(
            command_id=command_id,
            order_item_id=order_item_id,
            status=status,
            quantity=quantity,
            result_code=result_code,
        )
        self.session.add(line_result)
        self.session.flush()
        return line_result

    def list_success_order_item_ids(self, command_id: int) -> set[int]:
        """이 명령에서 이미 성공 확인된 라인(order_item_id) 집합 - 재시도 시 이 집합에
        속한 라인은 다시 전송하지 않는다(부분성공 보호)."""
        stmt = select(ExternalCommandLineResult.order_item_id).where(
            ExternalCommandLineResult.command_id == command_id, ExternalCommandLineResult.status == "SUCCESS"
        )
        return set(self.session.execute(stmt).scalars())

    def sum_success_quantity(self, order_item_id: int) -> int:
        """이 주문상품(order_item)에 대해 지금까지(여러 Shipment/여러 명령에 걸쳐) 성공
        확인된 발송 수량 합계 - 주문 전체 이행 여부를 "라인 존재"가 아니라 "수량 합계"로
        판단하기 위함이다(같은 OrderItem이 여러 Shipment로 나뉘어 부분 발송되는 경우)."""
        stmt = select(func.coalesce(func.sum(ExternalCommandLineResult.quantity), 0)).where(
            ExternalCommandLineResult.order_item_id == order_item_id, ExternalCommandLineResult.status == "SUCCESS"
        )
        return int(self.session.execute(stmt).scalar_one())


class OrderStatusConflictRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, conflict: OrderStatusConflict) -> OrderStatusConflict:
        self.session.add(conflict)
        self.session.flush()
        return conflict

    def get_by_id(self, conflict_id: int) -> Optional[OrderStatusConflict]:
        return self.session.get(OrderStatusConflict, conflict_id)

    def list_unresolved(self, order_id: Optional[int] = None) -> list[OrderStatusConflict]:
        stmt = select(OrderStatusConflict).where(OrderStatusConflict.resolved_at.is_(None))
        if order_id is not None:
            stmt = stmt.where(OrderStatusConflict.order_id == order_id)
        return list(self.session.execute(stmt.order_by(OrderStatusConflict.detected_at.desc())).scalars())

    def get_unresolved_for_status(self, order_id: int, channel_status: str) -> Optional[OrderStatusConflict]:
        """같은 주문에 같은 채널상태로 이미 미해소 충돌이 있는지 확인한다(중복 생성 방지).

        채널 상태 재조회(스케줄 작업/발송 성공 후)가 반복 실행돼도, 운영자가 아직
        해소하지 않은 동일 충돌을 매번 새 행으로 쌓지 않는다."""
        return self.session.execute(
            select(OrderStatusConflict).where(
                OrderStatusConflict.order_id == order_id,
                OrderStatusConflict.channel_status == channel_status,
                OrderStatusConflict.resolved_at.is_(None),
            )
        ).scalar_one_or_none()


class ProductSyncCommandDetailRepository:
    """ExternalCommand(INVENTORY_UPDATE/SALE_STATUS_UPDATE)의 확정된 목표값
    (models.integration_sync.ProductSyncCommandDetail)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, detail: ProductSyncCommandDetail) -> ProductSyncCommandDetail:
        self.session.add(detail)
        self.session.flush()
        return detail

    def get_by_command_id(self, command_id: int) -> Optional[ProductSyncCommandDetail]:
        return self.session.execute(
            select(ProductSyncCommandDetail).where(ProductSyncCommandDetail.command_id == command_id)
        ).scalar_one_or_none()
