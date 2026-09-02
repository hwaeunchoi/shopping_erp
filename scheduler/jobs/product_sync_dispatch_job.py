"""
scheduler/jobs/product_sync_dispatch_job.py
------------------------------------------------
ExternalCommand(outbox)에 쌓인 재고 수량/판매상태 전송 명령을 실제로 실행한다.
상용 ERP 확장(3단계, 첫 묶음).

API(POST /api/products/platform-map/{id}/sync-inventory,
/sync-sale-status)는 채널 HTTP 호출을 API 요청 스레드에서 동기 실행하지 않는다 -
enqueue_*()로 PENDING 명령만 만들고 즉시 202를 반환한다. 실제 채널 호출은 이 잡이
주기적으로 수행한다.

기본 차단: settings.product_channel_sync_enabled가 False(기본값)이면 이 잡은
아무 것도 하지 않고 즉시 반환한다(stale RUNNING 회수도, due 명령 조회도, 커넥터
생성도 하지 않는다 - 외부 HTTP 요청이 0건임을 보장한다).

명령종류 분리: INVENTORY_UPDATE/SALE_STATUS_UPDATE 두 command_type만 다룬다 -
scheduler.jobs.outbox_dispatch_job(SHIPMENT_SUBMIT 전용)과는 완전히 분리된 별도
잡이다. ExternalCommandRepository.list_due_for_execution()/list_stale_running()이
이미 command_type으로 필터링하므로, 기존 송장 worker가 이 잡의 명령을 집어가거나
그 반대로 섞이는 일은 없다(tests/unit/test_product_sync_dispatch_job.py의
회귀 테스트 참고).

한 명령의 실패가 다른 명령 처리를 막지 않도록 명령별로 커밋한다(scheduler.jobs.
outbox_dispatch_job과 동일 원칙).
"""

import logging

from config.settings import settings
from core.database import session_scope
from repositories.integration_sync_repository import ExternalCommandRepository
from services.product_sync_dispatch_service import (
    INVENTORY_UPDATE,
    SALE_STATUS_UPDATE,
    ProductSyncAlreadyRunningError,
    ProductSyncDispatchService,
)

logger = logging.getLogger(__name__)

COMMAND_TYPES = (INVENTORY_UPDATE, SALE_STATUS_UPDATE)


def run() -> dict[str, int]:
    if not settings.product_channel_sync_enabled:
        logger.debug("재고/판매상태 전송 기능이 비활성화(OFF) 상태라 product_sync_dispatch_job을 건너뜁니다.")
        return {"skipped_disabled": 1}

    recovered = 0
    executed = succeeded = retry_wait = failed = unknown = cancelled = blocked_by_unknown = 0
    due_ids: list[int] = []

    with session_scope() as db:
        for command_type in COMMAND_TYPES:
            recovered += ProductSyncDispatchService(db).recover_stale_running(command_type)
        db.commit()

        for command_type in COMMAND_TYPES:
            due_ids.extend(c.id for c in ExternalCommandRepository(db).list_due_for_execution(command_type))

        for command_id in due_ids:
            service = ProductSyncDispatchService(db)
            try:
                outcome = service.execute_command(command_id)
                if outcome.command.status == "SUCCESS" and not outcome.already_processed:
                    executed += 1
                    succeeded += 1
                elif outcome.command.status == "CANCELLED":
                    executed += 1
                    cancelled += 1
                elif outcome.command.status == "PENDING":
                    # UNKNOWN 선행 명령이 해소되지 않아 이번 회차는 건너뜀(그대로 PENDING).
                    blocked_by_unknown += 1
                db.commit()
            except ProductSyncAlreadyRunningError:
                # 다른 worker가 동시에 처리 중(claim 실패) - 이 회차에서는 건너뛴다.
                db.rollback()
                continue
            except Exception as e:  # noqa: BLE001 - 한 명령의 실패가 다른 명령을 막지 않는다.
                # execute_command()가 이미 FAILED/RETRY_WAIT/UNKNOWN을 세션에 반영해
                # 두었다 - 그 상태 변화를 그대로 커밋한다(outbox_dispatch_job과 동일 원칙).
                executed += 1
                db.commit()
                command = ExternalCommandRepository(db).get_by_id(command_id)
                if command is not None:
                    if command.status == "RETRY_WAIT":
                        retry_wait += 1
                    elif command.status == "FAILED":
                        failed += 1
                    elif command.status == "UNKNOWN":
                        unknown += 1
                logger.info(
                    "재고/판매상태 명령 실행 실패(안전 기록됨): command_id=%s, %s", command_id, type(e).__name__
                )

    return {
        "recovered_stale": recovered,
        "due": len(due_ids),
        "executed": executed,
        "succeeded": succeeded,
        "retry_wait": retry_wait,
        "failed": failed,
        "unknown": unknown,
        "cancelled": cancelled,
        "blocked_by_unknown": blocked_by_unknown,
    }
