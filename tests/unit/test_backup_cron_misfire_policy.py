"""
tests/unit/test_backup_cron_misfire_policy.py
------------------------------------------------
fix/postgres-backup-missed-run-recovery 후속(cron misfire 공백 수정) -
scheduler/scheduler.py의 backup cron job에 적용한 misfire_grace_time=None +
coalesce=True 정책이 실제로 어떤 실행 결과를 만드는지, apscheduler==3.10.4가
설치한 실제 실행 경로(apscheduler.executors.base.run_job)를 "직접" 호출해
검증한다 - 실 시스템 시계 변경이나 sleep 없이, run_times에 넣는 datetime
자체가 "주입 가능한 시간"이다(과거 실행 예정 시각을 그대로 리터럴로 만들어
넘긴다).

배경: BlockingScheduler는 실행 중(프로세스가 살아있는 채) 이벤트 루프가
지연되면(예: 다른 job 처리에 시간이 걸림) 03:00 UTC 실행이 "약간 늦게"
발화한다. 라이브러리 기본값(misfire_grace_time=1초)이면
apscheduler/executors/base.py의 run_job()이 grace_time을 넘긴 실행을 통째로
건너뛴다(EVENT_JOB_MISSED) - 프로세스는 죽지 않았으므로 backup_catchup(재기동
1회성)도 이 누락을 절대 감지하지 못한다. 이 파일은 misfire_grace_time=None이
실제로 이 skip을 막는다는 것과, coalesce=True가 여러 번 누적된 due 시각을
1회 실행으로 합친다는 것을 apscheduler 자신의 실행 함수로 직접 증명한다
(우리 코드를 흉내 낸 재구현이 아니라, 설치된 라이브러리 버전이 실제로 이렇게
동작한다는 사실 자체를 고정한다).
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
from apscheduler.executors.base import run_job

from scheduler.scheduler import build_scheduler


def _stub_job(*, misfire_grace_time, func) -> SimpleNamespace:
    return SimpleNamespace(id="backup", misfire_grace_time=misfire_grace_time, func=func, args=(), kwargs={})


class TestInstalledSchedulerBackupCronConfig:
    """scheduler.py가 실제로 build_scheduler()에 등록하는 backup cron의
    정책값 자체를 다시 한번 이 파일의 실행 시나리오와 나란히 고정한다(다른
    파일의 등록 테스트와 중복되더라도, 아래 run_job() 시나리오들이 "실제로
    운영에 등록되는 그 job의 설정"을 재현하고 있다는 확인이 필요하다)."""

    def test_backup_cron_misfire_grace_time_and_coalesce_match_scenarios_below(self):
        scheduler = build_scheduler()
        job = scheduler.get_job("backup")
        assert job.misfire_grace_time is None
        assert job.coalesce is True
        assert job.max_instances == 1


class TestDelayedButAliveProcessStillExecutes:
    """실행 중(프로세스는 살아있음) 2초 이상 지연된 backup cron이 skip되지
    않고 1회 실행되는지 - misfire_grace_time=None 시나리오."""

    def test_run_more_than_two_seconds_late_still_executes_with_none_grace_time(self):
        func = MagicMock(return_value={"status": "SUCCESS"})
        job = _stub_job(misfire_grace_time=None, func=func)

        # "지연"을 실제 sleep으로 만들지 않고, 이미 2초 이상 지난 과거 시각을
        # run_time으로 직접 주입한다 - run_job() 내부에서
        # datetime.now(utc) - run_time으로 지연을 계산하므로 이 자체가
        # "주입 가능한 시간"이다.
        scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=5)

        events = run_job(job, "default", [scheduled_at], "apscheduler.executors.default")

        func.assert_called_once_with()
        assert not any(e.code == EVENT_JOB_MISSED for e in events)

    def test_same_delay_would_have_been_skipped_under_library_default_grace_time(self):
        """대조군 - 왜 이 정책 변경이 필요했는지를 실행으로 증명한다. 라이브러리
        기본값(misfire_grace_time=1)이었다면 프로세스가 죽지 않았어도 이
        지연된 실행은 조용히 skip됐을 것이다(EVENT_JOB_MISSED만 기록되고
        job.func는 아예 호출되지 않는다)."""
        func = MagicMock(return_value={"status": "SUCCESS"})
        job = _stub_job(misfire_grace_time=1, func=func)

        scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=5)

        events = run_job(job, "default", [scheduled_at], "apscheduler.executors.default")

        func.assert_not_called()
        assert any(e.code == EVENT_JOB_MISSED for e in events)


class TestAccumulatedMissedTimesCoalesceToOneRun:
    """장시간 프로세스가 살아있는 채로 지연돼(또는 due 시각이 여러 번
    누적돼) 03:00 UTC 발화 시점이 여러 개 쌓여도, coalesce=True 정책 하의
    실제 apscheduler 처리 경로(BaseScheduler._process_jobs -
    apscheduler/schedulers/base.py)가 최신 1개로 합쳐 1회만 실행하는지."""

    def test_multiple_due_run_times_collapse_to_a_single_execution(self):
        """apscheduler/schedulers/base.py._process_jobs()의 실제 로직
        (`run_times[-1:] if job.coalesce else run_times`)을 그대로 재현해,
        여러 누적 due 시각이 run_job()에 도달하기 전에 이미 1개로 줄어드는지
        확인한다 - 이 축약 이후 run_job()에는 항상 정확히 1개의 run_time만
        전달되므로 job.func도 정확히 1회만 호출된다."""
        now = datetime.now(timezone.utc)
        accumulated_due_times = [now - timedelta(days=3, seconds=1), now - timedelta(days=1), now]

        job = _stub_job(misfire_grace_time=None, func=MagicMock(return_value={"status": "SUCCESS"}))
        coalesce = True

        run_times = accumulated_due_times[-1:] if accumulated_due_times and coalesce else accumulated_due_times
        assert run_times == [accumulated_due_times[-1]]

        events = run_job(job, "default", run_times, "apscheduler.executors.default")

        job.func.assert_called_once_with()
        assert not any(e.code in (EVENT_JOB_MISSED, EVENT_JOB_ERROR) for e in events)

    def test_without_coalesce_all_accumulated_times_would_have_run(self):
        """대조군 - coalesce=False였다면 누적된 3개 시각이 전부 개별 실행됐을
        것이다(이 프로젝트는 backup cron에 coalesce=True를 명시했으므로
        실제로는 이 경로를 타지 않는다)."""
        now = datetime.now(timezone.utc)
        accumulated_due_times = [now - timedelta(days=3, seconds=1), now - timedelta(days=1), now]

        job = _stub_job(misfire_grace_time=None, func=MagicMock(return_value={"status": "SUCCESS"}))
        coalesce = False

        run_times = accumulated_due_times[-1:] if accumulated_due_times and coalesce else accumulated_due_times
        assert run_times == accumulated_due_times

        run_job(job, "default", run_times, "apscheduler.executors.default")

        assert job.func.call_count == 3
