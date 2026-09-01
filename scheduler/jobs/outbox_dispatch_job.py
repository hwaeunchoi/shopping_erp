"""
scheduler/jobs/outbox_dispatch_job.py
---------------------------------------
ExternalCommand(outbox)에 쌓인 채널 전송 명령을 실제로 실행한다.

API(POST /api/shipments/{id}/submit)는 더 이상 채널 HTTP 호출을 API 요청
스레드에서 동기 실행하지 않는다 - enqueue()로 PENDING 명령만 만들고 즉시
202를 반환한다(외부 API 지연이 API 응답시간에 전가되지 않도록). 실제 채널
호출은 이 잡이 주기적으로 수행한다.

순서:
1. recover_stale_running() - RUNNING으로 너무 오래 머물러 있는(worker가
   실행 도중 죽었다고 추정되는) 명령을 먼저 PENDING으로 회수한다.
2. PENDING + (RETRY_WAIT이고 next_retry_at이 지난) 명령을 모아 하나씩
   execute_command()로 실행한다.

한 명령의 실패가 다른 명령 처리를 막지 않도록 명령별로 커밋한다 - 실패해도
execute_command() 내부에서 이미 FAILED/RETRY_WAIT으로 세션에 반영해 두므로,
그 상태 변화까지 그대로 커밋한다(롤백하면 outbox 이력 자체가 사라져 "저장만
되고 방치" 상태를 재현하게 된다).

실제 API 호출은 이 잡이 사용하는 커넥터(get_mall_connector가 반환하는 실제
Naver/Coupang 커넥터)를 통해 나간다 - 테스트는 MockTransport로만 검증했고,
이 잡 자체를 실 계정으로 실행한 적은 없다(운영 배포 전 별도 확인 필요).
"""

import logging

from core.database import session_scope
from repositories.integration_sync_repository import ExternalCommandRepository
from services.shipment_dispatch_service import ShipmentAlreadyRunningError, ShipmentDispatchService

logger = logging.getLogger(__name__)

COMMAND_TYPE = "SHIPMENT_SUBMIT"


def run() -> dict[str, int]:
    recovered = 0
    executed = succeeded = retry_wait = failed = 0
    due_ids: list[int] = []

    with session_scope() as db:
        recovered = ShipmentDispatchService(db).recover_stale_running(COMMAND_TYPE)
        db.commit()

        due_ids = [c.id for c in ExternalCommandRepository(db).list_due_for_execution(COMMAND_TYPE)]

        for command_id in due_ids:
            service = ShipmentDispatchService(db)
            try:
                outcome = service.execute_command(command_id)
                executed += 1
                if outcome.command.status == "SUCCESS":
                    succeeded += 1
                db.commit()
            except ShipmentAlreadyRunningError:
                # 다른 worker가 동시에 처리 중 - 이 회차에서는 건너뛴다(다음 주기에 재확인).
                db.rollback()
                continue
            except Exception as e:  # noqa: BLE001 - 한 명령의 실패가 다른 명령을 막지 않는다.
                # execute_command()가 이미 FAILED/RETRY_WAIT을 세션에 반영해 두었다 -
                # 그 상태 변화를 그대로 커밋한다(위 docstring 참고).
                executed += 1
                db.commit()
                command = ExternalCommandRepository(db).get_by_id(command_id)
                if command is not None:
                    if command.status == "RETRY_WAIT":
                        retry_wait += 1
                    elif command.status == "FAILED":
                        failed += 1
                logger.info("outbox 명령 실행 실패(안전 기록됨): command_id=%s, %s", command_id, type(e).__name__)

    return {
        "recovered_stale": recovered,
        "due": len(due_ids),
        "executed": executed,
        "succeeded": succeeded,
        "retry_wait": retry_wait,
        "failed": failed,
    }
