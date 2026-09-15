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

fix/postgres-backup-missed-run-recovery 후속(cron misfire 공백 수정):
backup cron만 명시적으로 misfire_grace_time=None(무제한)을 부여해, 프로세스가
살아있는 채로 이벤트 루프가 지연돼도 03:00 UTC 실행이 조용히 skip되지 않도록
한다. 이 정책 변경이 backup에만 적용되고 나머지 15개 job의 라이브러리
기본값(misfire_grace_time=1, coalesce=True - apscheduler.schedulers.base의
BaseScheduler._create_default_executor/job_defaults)은 건드리지 않았는지
함께 고정한다.
"""

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from scheduler.scheduler import build_scheduler

# backup/backup_catchup을 제외한 기존 job 중, 이번 misfire 정책 변경의
# "무영향 대조군"으로 확인할 대표 표본 - 전체 15개를 다 나열하지 않아도
# CronTrigger/IntervalTrigger 각 한 개씩만 확인하면 라이브러리 기본값
# job_defaults가 여전히 build_scheduler() 전역에 적용되고 있음을 알 수 있다
# (add_job() 호출에서 misfire_grace_time/coalesce를 명시하지 않은 job은 전부
# 동일한 BlockingScheduler(...)._job_defaults를 물려받는다).
_UNCHANGED_POLICY_SAMPLE_JOB_IDS = ["product_sync", "ad_collect", "report_generate"]

# apscheduler/schedulers/base.py의 add_job()은 misfire_grace_time을 명시하지
# 않으면(undefined) Job 생성 kwargs에서 그 키 자체를 제외한다 - scheduler.start()가
# 실제로 호출돼 _real_add_job()이 job_defaults를 병합하기 전까지는 Job 객체에
# misfire_grace_time 슬롯 자체가 존재하지 않는다(getattr 시 AttributeError).
# build_scheduler()는 테스트에서 절대 start()하지 않으므로(session_scope()를 쓰는
# 잡을 실제로 실행하지 않기 위해), "다른 job이 라이브러리 기본값을 물려받는지"는
# 개별 Job 인스턴스가 아니라 scheduler 자체의 _job_defaults 설정으로 확인해야 한다
# - 이것이 실제로 start() 이후 모든 미지정 job에 적용될 값이다.


class TestSchedulerJobRegistration:
    def test_total_job_count_is_seventeen(self):
        """기존 16개 + backup_catchup 1개(이번 misfire 정책 변경은 job을
        추가/삭제하지 않는다 - 개수는 그대로다)."""
        scheduler = build_scheduler()
        assert len(scheduler.get_jobs()) == 17

    def test_existing_backup_cron_trigger_and_id_are_unchanged(self):
        """cron 스케줄 자체(매일 03:00 UTC)와 job id는 이전과 동일하다 - 이번
        수정은 misfire/coalesce 정책만 명시적으로 바꾼다."""
        scheduler = build_scheduler()
        job = scheduler.get_job("backup")
        assert job is not None
        assert isinstance(job.trigger, CronTrigger)
        assert str(job.trigger) == "cron[hour='3', minute='0']"
        assert job.max_instances == 1

    def test_backup_cron_has_unlimited_misfire_grace_time(self):
        """설치된 apscheduler==3.10.4의 apscheduler/job.py 공식 docstring:
        misfire_grace_time=None은 "allow the job to run no matter how late it
        is"를 의미한다(추측이 아니라 라이브러리 소스로 확인한 계약). 프로세스가
        죽지 않은 채 이벤트 루프가 단 1초 이상 지연돼 03:00 UTC를 넘기더라도
        (라이브러리 기본값 misfire_grace_time=1이면 apscheduler/executors/base.py의
        run_job()이 그 실행을 통째로 건너뛴다) 이 job은 절대 skip되지 않아야
        한다."""
        scheduler = build_scheduler()
        job = scheduler.get_job("backup")
        assert job.misfire_grace_time is None

    def test_backup_cron_coalesce_is_true(self):
        """apscheduler/schedulers/base.py의 _process_jobs()는
        `run_times[-1:] if job.coalesce else run_times`로 누적된 여러 due
        시각을 최신 1개로 합친다 - 장시간 프로세스가 살아있는 채로 지연돼
        03:00 시각이 여러 번(예: 재시도 루프 등으로) 누적돼도 실제 실행은
        1회여야 한다는 정책을 이 플래그가 보장한다."""
        scheduler = build_scheduler()
        job = scheduler.get_job("backup")
        assert job.coalesce is True

    def test_scheduler_level_job_defaults_unchanged(self):
        """build_scheduler()가 BlockingScheduler(timezone="UTC")에 커스텀
        job_defaults를 넘기지 않아, 라이브러리 기본값(misfire_grace_time=1,
        coalesce=True, max_instances=1)이 전역 기본으로 여전히 살아있는지
        확인한다 - backup cron만 add_job() 호출에서 개별적으로 override했을
        뿐, 전역 기본값 자체는 바꾸지 않았어야 한다."""
        scheduler = build_scheduler()
        assert scheduler._job_defaults == {"misfire_grace_time": 1, "coalesce": True, "max_instances": 1}

    def test_other_jobs_do_not_override_misfire_grace_time_individually(self):
        """backup cron 전용 misfire 정책 변경이 다른 job의 add_job() 호출에
        새지 않았는지 확인한다. scheduler.start() 전에는 명시적으로 넘기지
        않은 misfire_grace_time 슬롯 자체가 Job 인스턴스에 존재하지 않으므로
        (apscheduler/schedulers/base.py의 add_job()이 undefined 값인 키를
        job 생성 kwargs에서 제외한다), "여전히 지정 안 됨" 상태 자체가 곧
        "전역 기본값을 그대로 물려받는다"는 뜻이다."""
        scheduler = build_scheduler()
        for job_id in _UNCHANGED_POLICY_SAMPLE_JOB_IDS:
            job = scheduler.get_job(job_id)
            assert job is not None, f"{job_id} job이 등록되지 않았습니다."
            assert not hasattr(
                job, "misfire_grace_time"
            ), f"{job_id}이 misfire_grace_time을 개별적으로 override하고 있습니다(대상은 backup cron뿐이어야 합니다)."

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
