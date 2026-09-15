"""
scheduler/scheduler.py
--------------------------
APScheduler 기반 배치 스케줄러 프로세스.

실행 방법 (Windows, 프로젝트 루트에서, venv 활성화 후, init_db.py 실행 후):
    python scheduler\\scheduler.py

Ctrl+C로 종료한다. 각 작업의 시작/완료/실패는 콘솔과 logs/erp.log뿐 아니라
task_execution_history 테이블에도 남는다(SRS FR-LOG-01 연장, 보고서 화면에서
작업 이력을 조회할 수 있도록 report_generate_job 추가 시점에 전체 잡에
공통으로 연동했다). 잡 실행 자체와 이력 기록을 같은 트랜잭션에 묶지 않도록
이력 기록은 별도의 짧은 session_scope()로 처리한다.
"""

import logging
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apscheduler.schedulers.blocking import BlockingScheduler  # noqa: E402
from apscheduler.triggers.cron import CronTrigger  # noqa: E402
from apscheduler.triggers.date import DateTrigger  # noqa: E402
from apscheduler.triggers.interval import IntervalTrigger  # noqa: E402

from config.logging_config import setup_logging  # noqa: E402
from config.settings import settings  # noqa: E402
from core.crypto import validate_startup_secrets  # noqa: E402
from core.database import session_scope  # noqa: E402
from repositories.extra_repository import TaskExecutionHistoryRepository  # noqa: E402
from scheduler.jobs import (  # noqa: E402
    ad_collect_job,
    alert_evaluation_job,
    backup_job,
    channel_status_sync_job,
    claim_sync_job,
    cs_inquiry_sync_job,
    customer_stats_job,
    order_collect_job,
    outbox_dispatch_job,
    product_option_publish_dispatch_job,
    product_publish_dispatch_job,
    product_sync_dispatch_job,
    product_sync_job,
    profit_calculation_job,
    report_generate_job,
    settlement_sync_job,
)

logger = logging.getLogger(__name__)


def _run_job(name: str, func: Callable[[], object], task_type: str, trigger_type: str = "SCHEDULE") -> None:
    """trigger_type 기본값은 기존 모든 정기 cron/interval 잡과 동일한 "SCHEDULE" -
    이 매개변수를 추가해도 기존 호출부(아래 run_* 함수들)는 전부 그대로다.
    run_backup_catchup()만 "CATCHUP"을 명시적으로 넘긴다."""
    logger.info(f"[{name}] 작업을 시작합니다.")
    with session_scope() as db:
        history_id = (
            TaskExecutionHistoryRepository(db).start(task_type=task_type, trigger_type=trigger_type, target=name).id
        )

    try:
        result = func()
        logger.info(f"[{name}] 작업 완료: {result}")
        with session_scope() as db:
            repo = TaskExecutionHistoryRepository(db)
            history = repo.get_by_id(history_id)
            if history is not None:
                repo.finish(history, status="SUCCESS", result_summary=str(result))
    except Exception as e:
        logger.exception(f"[{name}] 작업 실패")
        with session_scope() as db:
            repo = TaskExecutionHistoryRepository(db)
            history = repo.get_by_id(history_id)
            if history is not None:
                repo.finish(history, status="FAILED", error_message=str(e))


def run_product_sync() -> None:
    _run_job("product_sync", product_sync_job.run, task_type="PRODUCT_SYNC")


def run_order_collect() -> None:
    _run_job("order_collect", order_collect_job.run, task_type="ORDER_COLLECT")


def run_ad_collect() -> None:
    _run_job("ad_collect", ad_collect_job.run, task_type="AD_COLLECT")


def run_settlement_sync() -> None:
    _run_job("settlement_sync", settlement_sync_job.run, task_type="FULL_SYNC")


def run_profit_calculation() -> None:
    _run_job("profit_calculation", profit_calculation_job.run, task_type="REPORT_GENERATE")


def run_customer_stats() -> None:
    _run_job("customer_stats", customer_stats_job.run, task_type="FULL_SYNC")


def run_backup() -> None:
    _run_job("backup", backup_job.run, task_type="BACKUP")


def run_backup_catchup() -> None:
    """scheduler 시작 시 즉시 1회만 실행되는 "date" 트리거 job(아래
    build_scheduler() 참고) - 재기동 후 놓친 정기 03:00 백업을 안전하게 최대
    1회 보충한다(services/postgres_backup_service.py의 run_catchup_if_needed
    참고). trigger_type="CATCHUP"으로 이력을 남겨 정기 실행(run_backup, target=
    "backup")과 target="backup_catchup"으로도 이미 구분되고, TaskExecutionHistory.
    trigger_type으로도 한 번 더 구분된다."""
    _run_job("backup_catchup", backup_job.run_catchup, task_type="BACKUP", trigger_type="CATCHUP")


def run_report_generate() -> None:
    _run_job("report_generate", report_generate_job.run, task_type="REPORT_GENERATE")


def run_alert_evaluation() -> None:
    _run_job("alert_evaluation", alert_evaluation_job.run, task_type="ALERT_EVALUATE")


def run_outbox_dispatch() -> None:
    _run_job("outbox_dispatch", outbox_dispatch_job.run, task_type="FULL_SYNC")


def run_channel_status_sync() -> None:
    _run_job("channel_status_sync", channel_status_sync_job.run, task_type="MALL_SYNC")


def run_claim_sync() -> None:
    _run_job("claim_sync", claim_sync_job.run, task_type="FULL_SYNC")


def run_product_sync_dispatch() -> None:
    _run_job("product_sync_dispatch", product_sync_dispatch_job.run, task_type="FULL_SYNC")


def run_product_publish_dispatch() -> None:
    _run_job("product_publish_dispatch", product_publish_dispatch_job.run, task_type="FULL_SYNC")


def run_product_option_publish_dispatch() -> None:
    _run_job("product_option_publish_dispatch", product_option_publish_dispatch_job.run, task_type="FULL_SYNC")


def run_cs_inquiry_sync() -> None:
    _run_job("cs_inquiry_sync", cs_inquiry_sync_job.run, task_type="FULL_SYNC")


def build_scheduler() -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone="UTC")
    # 상품 동기화는 주문 수집보다 먼저 실행되도록 더 짧은 주기(10분보다 여유를 둔 20분)로
    # 등록한다 - 상품이 먼저 등록/매핑돼 있어야 주문 수집 시 자동매칭이 잘 된다.
    scheduler.add_job(run_product_sync, IntervalTrigger(minutes=20), id="product_sync", max_instances=1)
    scheduler.add_job(run_order_collect, IntervalTrigger(minutes=10), id="order_collect", max_instances=1)
    scheduler.add_job(run_ad_collect, CronTrigger(hour=7, minute=0), id="ad_collect", max_instances=1)
    scheduler.add_job(run_settlement_sync, CronTrigger(hour=7, minute=30), id="settlement_sync", max_instances=1)
    scheduler.add_job(run_profit_calculation, CronTrigger(hour=1, minute=0), id="profit_calculation", max_instances=1)
    scheduler.add_job(run_customer_stats, CronTrigger(hour=1, minute=30), id="customer_stats", max_instances=1)
    scheduler.add_job(run_backup, CronTrigger(hour=3, minute=0), id="backup", max_instances=1)
    # 재기동 후 놓친 예약 백업 보충 - scheduler.start() 직후 즉시(1회만) 실행되는
    # "date" 트리거다. run_date를 지정하지 않으면 DateTrigger가 "지금"으로 잡는데,
    # add_job() 호출 시점과 scheduler.start()의 실제 루프 시작 사이에는 항상 약간의
    # 지연이 있어(다른 잡 등록/이벤트루프 초기화) 이 job은 등록 시점 기준으로 보면
    # 이미 "약간 지각"한 상태로 발화한다 - misfire_grace_time=None(무제한)을 명시해
    # BlockingScheduler(timezone="UTC")의 라이브러리 기본값(1초)에 걸려 건너뛰지
    # 않게 한다(이 job 자체는 "정해진 시각에 맞춰 도는" 게 아니라 "뜨면 무조건 한 번
    # 확인하는" 성격이라 misfire 개념 자체가 무의미하다 - services/
    # postgres_backup_service.py의 run_catchup_if_needed가 실제로 백업이 필요한지는
    # DB의 BackupHistory를 보고 스스로 판단하므로, 이 job이 몇 초 늦게 발화해도
    # 정책에 전혀 영향이 없다). 실패해도(예: DB 아직 준비 안 됨) 정기 "backup" cron은
    # 그대로 등록돼 있으므로 scheduler 자체는 계속 정상 동작한다.
    scheduler.add_job(run_backup_catchup, DateTrigger(), id="backup_catchup", max_instances=1, misfire_grace_time=None)
    scheduler.add_job(run_report_generate, CronTrigger(hour=2, minute=0), id="report_generate", max_instances=1)
    scheduler.add_job(run_alert_evaluation, IntervalTrigger(minutes=30), id="alert_evaluation", max_instances=1)
    # outbox(ExternalCommand) 실행 - API는 enqueue()만 하고 실제 채널 호출은 이 잡이 한다.
    # order_collect(10분)보다 짧게 둬서 송장 전송 요청이 오래 PENDING으로 방치되지 않게 한다.
    scheduler.add_job(run_outbox_dispatch, IntervalTrigger(minutes=2), id="outbox_dispatch", max_instances=1)
    # 채널 상태 읽기 전용 재조회 - order_collect_job과 조회 범위가 겹치므로 주기를 다르게
    # 둬 과도한 중복 채널 호출을 피한다(order_collect_job.py는 그대로 둔다).
    scheduler.add_job(run_channel_status_sync, IntervalTrigger(minutes=15), id="channel_status_sync", max_instances=1)
    # 취소/반품/교환 클레임 자동 수집 - 상용 ERP 확장(2단계, 기본 OFF: claims_settlement_sync_enabled).
    scheduler.add_job(run_claim_sync, IntervalTrigger(minutes=20), id="claim_sync", max_instances=1)
    # 재고/판매상태 전송 outbox 실행 - 상용 ERP 확장(3단계 첫 묶음, 기본 OFF:
    # product_channel_sync_enabled). outbox_dispatch_job(송장 전용)과는 완전히 분리된
    # 별도 잡이다 - command_type이 달라 서로의 명령을 집어가지 않는다.
    scheduler.add_job(
        run_product_sync_dispatch, IntervalTrigger(minutes=2), id="product_sync_dispatch", max_instances=1
    )
    # 신규 상품 등록 outbox 실행 - 상용 ERP 확장(3단계 두 번째 묶음, 기본 OFF:
    # product_publish_enabled). command_type(PRODUCT_CREATE)과 target_type
    # (PRODUCT_PUBLISH_DRAFT)이 모두 달라 product_sync_dispatch_job과 섞이지 않는다.
    scheduler.add_job(
        run_product_publish_dispatch, IntervalTrigger(minutes=2), id="product_publish_dispatch", max_instances=1
    )
    # 옵션조합 상품 등록 outbox 실행 - 상용 ERP 확장(3단계 세 번째 묶음, 기본 OFF:
    # product_option_publish_enabled). command_type(PRODUCT_OPTION_CREATE)과
    # target_type(PRODUCT_OPTION_PUBLISH_DRAFT)이 모두 달라 위 두 잡과 섞이지 않는다.
    scheduler.add_job(
        run_product_option_publish_dispatch,
        IntervalTrigger(minutes=2),
        id="product_option_publish_dispatch",
        max_instances=1,
    )
    # 채널 CS(고객문의) 조회 동기화 - 상용 ERP 확장(5단계 B묶음, 기본 OFF:
    # cs_inquiry_sync_enabled). 조회 전용이다 - 어떤 코드 경로도 채널에 답변을
    # 쓰지 않는다(services/cs_channel_sync_service.py 모듈 docstring 참고).
    scheduler.add_job(run_cs_inquiry_sync, IntervalTrigger(minutes=15), id="cs_inquiry_sync", max_instances=1)
    return scheduler


def main() -> None:
    # DB 접속·잡 등록 전에 먼저 검증한다 - Secret이 안전하지 않으면 그 어떤 작업도
    # 시도하지 않고 즉시 종료한다(fail-closed). 값 자체는 예외 메시지에 담기지 않는다.
    validate_startup_secrets(settings.jwt_secret_key, settings.credential_encryption_key)
    setup_logging()
    scheduler = build_scheduler()
    logger.info("스케줄러를 시작합니다. (Ctrl+C로 종료)")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("스케줄러를 종료합니다.")


if __name__ == "__main__":
    main()
