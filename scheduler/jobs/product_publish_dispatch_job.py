"""
scheduler/jobs/product_publish_dispatch_job.py
------------------------------------------------
ExternalCommand(outbox)에 쌓인 신규 상품 등록(PRODUCT_CREATE) 명령을 실제로
실행한다. 상용 ERP 확장(3단계, 두 번째 묶음).

API(POST /api/products/publish-drafts/{id}/submit)는 채널 HTTP 호출을 API 요청
스레드에서 동기 실행하지 않는다 - enqueue_create()로 PENDING 명령만 만들고 즉시
202를 반환한다. 실제 채널 등록 호출은 이 잡이 주기적으로 수행한다.

기본 차단: settings.product_publish_enabled가 False(기본값)이면 이 잡은 아무
것도 하지 않고 즉시 반환한다(stale RUNNING 회수도, due 명령 조회도, 커넥터 생성도
하지 않는다 - 외부 HTTP 요청이 0건임을 보장한다).

명령종류 분리: PRODUCT_CREATE 하나만 다룬다 - scheduler.jobs.product_sync_dispatch_job
(INVENTORY_UPDATE/SALE_STATUS_UPDATE/PRODUCT_INFO_UPDATE 전용)과 완전히 분리된
별도 잡이다. target_type도 서로 다르다(PRODUCT_PUBLISH_DRAFT vs
PRODUCT_PLATFORM_MAP) - ExternalCommandRepository.list_due_for_execution()/
list_stale_running()이 command_type으로 필터링하므로 두 잡의 명령이 섞이지 않는다.

한 명령의 실패가 다른 명령 처리를 막지 않도록 명령별로 커밋한다(scheduler.jobs.
outbox_dispatch_job과 동일 원칙).
"""

import logging

from config.settings import settings
from core.database import session_scope
from repositories.integration_sync_repository import ExternalCommandRepository
from services.product_publish_service import PRODUCT_CREATE, ProductPublishAlreadyRunningError, ProductPublishService

logger = logging.getLogger(__name__)


def run() -> dict[str, int]:
    if not settings.product_publish_enabled:
        logger.debug("상품 등록 기능이 비활성화(OFF) 상태라 product_publish_dispatch_job을 건너뜁니다.")
        return {"skipped_disabled": 1}

    with session_scope() as db:
        recovered = ProductPublishService(db).recover_stale_running()
        db.commit()

        due_ids = [c.id for c in ExternalCommandRepository(db).list_due_for_execution(PRODUCT_CREATE)]

        executed = succeeded = retry_wait = failed = unknown = cancelled = blocked_by_unknown = 0
        for command_id in due_ids:
            service = ProductPublishService(db)
            try:
                outcome = service.execute_command(command_id)
                if outcome.command.status == "SUCCESS" and not outcome.already_processed:
                    executed += 1
                    succeeded += 1
                elif outcome.command.status == "CANCELLED":
                    executed += 1
                    cancelled += 1
                elif outcome.command.status == "PENDING":
                    blocked_by_unknown += 1
                db.commit()
            except ProductPublishAlreadyRunningError:
                db.rollback()
                continue
            except Exception as e:  # noqa: BLE001 - 한 명령의 실패가 다른 명령을 막지 않는다.
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
                logger.info("상품 등록 명령 실행 실패(안전 기록됨): command_id=%s, %s", command_id, type(e).__name__)

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
