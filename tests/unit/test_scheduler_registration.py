"""
tests/unit/test_scheduler_registration.py
------------------------------------------------
scheduler/scheduler.py의 job "등록" 구조만 검증한다(session_scope()를 쓰는
스케줄러 잡 "실행"은 기존 방침대로 격리 PostgreSQL/실제 잡 모듈 단위테스트가
담당한다 - build_scheduler()는 add_job()만 호출하고 어떤 잡도 실제로 실행하지
않으므로 DB 접속 없이 안전하게 단위테스트할 수 있다).

fix/postgres-backup-missed-run-recovery: 재기동 후 놓친 예약 백업을 보충하는
backup_catchup job이 기존 backup cron job을 건드리지 않고 추가로만
등록됐는지 확인한다.
"""

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from scheduler.scheduler import build_scheduler


class TestSchedulerJobRegistration:
    def test_total_job_count_is_seventeen(self):
        """기존 16개 + backup_catchup 1개."""
        scheduler = build_scheduler()
        assert len(scheduler.get_jobs()) == 17

    def test_existing_backup_cron_job_is_unchanged(self):
        scheduler = build_scheduler()
        job = scheduler.get_job("backup")
        assert job is not None
        assert isinstance(job.trigger, CronTrigger)
        assert str(job.trigger) == "cron[hour='3', minute='0']"
        assert job.max_instances == 1

    def test_backup_catchup_job_registered_as_immediate_date_trigger(self):
        scheduler = build_scheduler()
        job = scheduler.get_job("backup_catchup")
        assert job is not None
        assert isinstance(job.trigger, DateTrigger)

    def test_backup_catchup_job_has_unlimited_misfire_grace_time(self):
        """등록~scheduler.start() 사이의 짧은 지연으로 기본 misfire_grace_time
        (라이브러리 기본값 1초)에 걸려 건너뛰지 않도록 명시적으로 무제한이어야
        한다."""
        scheduler = build_scheduler()
        job = scheduler.get_job("backup_catchup")
        assert job.misfire_grace_time is None

    def test_backup_catchup_job_max_instances_is_one(self):
        scheduler = build_scheduler()
        job = scheduler.get_job("backup_catchup")
        assert job.max_instances == 1

    def test_all_other_job_ids_unaffected(self):
        """backup_catchup 추가가 기존 16개 job id를 하나도 건드리지 않았는지."""
        scheduler = build_scheduler()
        ids = {j.id for j in scheduler.get_jobs()}
        expected_existing = {
            "product_sync",
            "order_collect",
            "ad_collect",
            "settlement_sync",
            "profit_calculation",
            "customer_stats",
            "backup",
            "report_generate",
            "alert_evaluation",
            "outbox_dispatch",
            "channel_status_sync",
            "claim_sync",
            "product_sync_dispatch",
            "product_publish_dispatch",
            "product_option_publish_dispatch",
            "cs_inquiry_sync",
        }
        assert expected_existing.issubset(ids)
        assert "backup_catchup" in ids
