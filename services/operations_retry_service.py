"""
services/operations_retry_service.py
------------------------------------------
상용 ERP 확장(6단계) - 통합 실패 작업함의 "안전한 라우팅 계층".

이 서비스는 검증 로직을 새로 만들지 않는다 - command_type별로 이미 있는 단건
서비스(services.shipment_dispatch_service.ShipmentDispatchService,
services.product_publish_service.ProductPublishService,
services.product_option_publish_service.ProductOptionPublishService,
services.product_sync_dispatch_service.ProductSyncDispatchService)의
retry_failed_command()/resolve_unknown_command()를 command_type으로 찾아
호출만 한다(services.product_bulk_service.ProductBulkService.retry_commands와
동일한 라우팅 방식 - 이 서비스는 그 범위를 SHIPMENT_SUBMIT까지 넓힌 버전이다).
검증을 우회하는 범용 DB 상태변경(UPDATE external_commands SET status=...)은
어디에도 없다 - 항상 해당 도메인 서비스의 메서드를 통과한다.

재처리(대량) 트랜잭션 정책(services.product_bulk_service와 동일 원칙):
- 항목마다 진짜 commit()/rollback()을 쓴다(SAVEPOINT 아님) - 뒤 항목의
  rollback이 앞서 커밋된 항목의 결과를 지우지 않는다.
- 이미 처리 로직이 다루는 "예상된" 실패(ValueError - 예: 이미 FAILED가 아닌
  상태로 바뀜, 대상 없음)는 그 항목만 실패로 기록하고 배치를 계속한다.
- 그 외 예외(DB 오류 등 세션 상태를 신뢰할 수 없는 경우)는 배치를 즉시
  중단하고, 아직 시도하지 않은 나머지 항목을 전부 명시적으로
  ABORTED_DUE_TO_PRIOR_ERROR로 표시한다(조용히 빠뜨리지 않는다 - 부분성공
  위장 금지).

UNKNOWN은 이 서비스의 bulk_retry()가 절대 재처리하지 않는다(항목만
UNKNOWN_REQUIRES_RESOLUTION으로 표시) - resolve_unknown()으로 운영자가 세 가지
해소값 중 하나를 직접 선택해야만 벗어날 수 있다. resolve_unknown()은 항상
evidence_note(확인 근거)를 요구하고, 어떤 도메인 서비스를 거쳤든 상관없이 이
서비스 자체가 AuditLog를 하나 더 남긴다(entity_type=EXTERNAL_COMMAND,
command="operations.resolve_unknown") - reason 필드에 운영자가 입력한 근거
원문을 담아, 기존 도메인 서비스의 감사로그(상태값 전이만 기록)에는 없던
"왜 그렇게 판단했는가"를 별도로 남긴다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from models.extra import AuditLog
from models.integration_sync import ExternalCommand
from repositories.integration_sync_repository import ExternalCommandRepository
from services.product_option_publish_service import ProductOptionPublishService
from services.product_publish_service import ProductPublishService
from services.product_sync_dispatch_service import (
    INVENTORY_UPDATE,
    PRODUCT_INFO_UPDATE,
    SALE_STATUS_UPDATE,
    ProductSyncDispatchService,
)
from services.shipment_dispatch_service import ShipmentDispatchService

logger = logging.getLogger(__name__)

MAX_BULK_RETRY_ITEMS = 50
RESOLUTION_VALUES = ("CONFIRMED_NOT_SENT", "CONFIRMED_SUCCESS", "CONFIRMED_FAILED")
MIN_EVIDENCE_NOTE_LENGTH = 5

RETRIED = "RETRIED"
NOT_FOUND = "NOT_FOUND"
NOT_RETRYABLE = "NOT_RETRYABLE"
UNKNOWN_REQUIRES_RESOLUTION = "UNKNOWN_REQUIRES_RESOLUTION"
FAILED_TO_ENQUEUE = "FAILED_TO_ENQUEUE"

_ERROR_CODE_MAX_LENGTH = 200
_SYNC_DISPATCH_TYPES = (INVENTORY_UPDATE, SALE_STATUS_UPDATE, PRODUCT_INFO_UPDATE)


class OperationsRetryValidationError(ValueError):
    """요청 자체가 잘못됐을 때(대상 없음/범위 초과/근거 누락 등) - API가 400으로 변환한다."""


@dataclass
class RetryItemResult:
    command_id: int
    outcome: str
    error_code: Optional[str] = None


@dataclass
class BulkRetryResult:
    items: list[RetryItemResult]
    aborted: bool = False


class OperationsRetryService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.command_repo = ExternalCommandRepository(session)
        self.shipment_service = ShipmentDispatchService(session)
        self.publish_service = ProductPublishService(session)
        self.option_publish_service = ProductOptionPublishService(session)
        self.sync_service = ProductSyncDispatchService(session)

    def _service_for(self, command_type: str):
        return {
            "SHIPMENT_SUBMIT": self.shipment_service,
            "PRODUCT_CREATE": self.publish_service,
            "PRODUCT_OPTION_CREATE": self.option_publish_service,
            "INVENTORY_UPDATE": self.sync_service,
            "SALE_STATUS_UPDATE": self.sync_service,
            "PRODUCT_INFO_UPDATE": self.sync_service,
        }.get(command_type)

    # --- UNKNOWN 해소 -------------------------------------------------------

    def list_unknown_actions(self, command_id: int) -> list[str]:
        """UNKNOWN이 아니면 빈 목록 - 화면이 "가능한 조치"를 물을 때 쓴다."""
        command = self.command_repo.get_by_id(command_id)
        if command is None or command.status != "UNKNOWN":
            return []
        return list(RESOLUTION_VALUES)

    def resolve_unknown(
        self, command_id: int, resolution: str, evidence_note: str, actor_id: Optional[int]
    ) -> ExternalCommand:
        if resolution not in RESOLUTION_VALUES:
            raise OperationsRetryValidationError(f"알 수 없는 해소 방식입니다: {resolution}")
        if not evidence_note or len(evidence_note.strip()) < MIN_EVIDENCE_NOTE_LENGTH:
            raise OperationsRetryValidationError(
                f"확인 근거를 {MIN_EVIDENCE_NOTE_LENGTH}자 이상 입력해야 합니다(예: 채널 관리자 화면에서"
                "확인한 방법과 시각)."
            )
        command = self.command_repo.get_by_id(command_id)
        if command is None:
            raise OperationsRetryValidationError(f"명령을 찾을 수 없습니다: command_id={command_id}")
        service = self._service_for(command.command_type)
        if service is None:
            raise OperationsRetryValidationError(f"지원하지 않는 명령종류입니다: {command.command_type}")

        updated = service.resolve_unknown_command(command_id, resolution, resolved_by=actor_id)
        self.session.add(
            AuditLog(
                entity_type="EXTERNAL_COMMAND",
                entity_id=command_id,
                action="UPDATE",
                changed_by=actor_id,
                changed_at=datetime.now(timezone.utc),
                command="operations.resolve_unknown",
                reason=f"[{resolution}] {evidence_note.strip()}"[:500],
            )
        )
        self.session.flush()
        return updated

    # --- 대량 재처리(FAILED 전용) --------------------------------------------

    def bulk_retry(self, command_ids: list[int], actor_id: Optional[int]) -> BulkRetryResult:
        if not command_ids:
            raise OperationsRetryValidationError("재처리할 항목을 선택해야 합니다.")
        if len(command_ids) > MAX_BULK_RETRY_ITEMS:
            raise OperationsRetryValidationError(f"한 번에 최대 {MAX_BULK_RETRY_ITEMS}건까지 재처리할 수 있습니다.")

        seen: set[int] = set()
        deduped: list[int] = []
        for cid in command_ids:
            if cid not in seen:
                seen.add(cid)
                deduped.append(cid)

        results: list[RetryItemResult] = []
        aborted = False
        for command_id in deduped:
            if aborted:
                results.append(RetryItemResult(command_id, FAILED_TO_ENQUEUE, "ABORTED_DUE_TO_PRIOR_ERROR"))
                continue
            try:
                outcome = self._retry_one(command_id, actor_id)
                results.append(outcome)
            except Exception:  # noqa: BLE001 - 세션 상태를 신뢰할 수 없어 배치를 중단한다.
                self.session.rollback()
                logger.exception("통합 실패 작업함 대량 재처리 중 예상하지 못한 오류: command_id=%s", command_id)
                results.append(RetryItemResult(command_id, FAILED_TO_ENQUEUE, "DB_OR_INTERNAL_ERROR"))
                aborted = True

        return BulkRetryResult(items=results, aborted=aborted)

    def _retry_one(self, command_id: int, actor_id: Optional[int]) -> RetryItemResult:
        command = self.command_repo.get_by_id(command_id)
        if command is None:
            return RetryItemResult(command_id, NOT_FOUND)
        if command.status == "UNKNOWN":
            return RetryItemResult(command_id, UNKNOWN_REQUIRES_RESOLUTION)
        if command.status != "FAILED":
            return RetryItemResult(command_id, NOT_RETRYABLE, f"CURRENT_STATUS_{command.status}")

        service = self._service_for(command.command_type)
        if service is None:
            return RetryItemResult(command_id, NOT_RETRYABLE, "UNSUPPORTED_COMMAND_TYPE")

        try:
            if command.command_type in _SYNC_DISPATCH_TYPES:
                service.retry_failed_command(command_id, resolved_by=actor_id)
            else:
                service.retry_failed_command(command_id)
        except ValueError as exc:
            # 이미 다른 요청이 먼저 처리해 상태가 바뀐 경우 등 - 예상된 실패이므로
            # 배치를 중단하지 않고 이 항목만 실패로 남긴다(services.product_bulk_service.
            # _BUSINESS_EXCEPTIONS와 동일한 분류 원칙).
            self.session.rollback()
            return RetryItemResult(command_id, NOT_RETRYABLE, str(exc)[:_ERROR_CODE_MAX_LENGTH])

        self.session.add(
            AuditLog(
                entity_type="EXTERNAL_COMMAND",
                entity_id=command_id,
                action="UPDATE",
                changed_by=actor_id,
                changed_at=datetime.now(timezone.utc),
                command="operations.bulk_retry",
                reason="운영 대시보드 통합 실패 작업함에서 선택 재처리",
            )
        )
        self.session.commit()
        return RetryItemResult(command_id, RETRIED)
