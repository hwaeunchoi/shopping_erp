"""
tests/integration/test_postgres_backup_catchup_pg.py
------------------------------------------------------------
fix/postgres-backup-missed-run-recovery: 재기동 후 놓친 예약 백업을 1회
보충하는 기능(services/postgres_backup_service.py의 run_catchup_if_needed())을
"진짜" PostgreSQL + 실제 pg_dump/pg_restore 바이너리로 검증한다.

격리 원칙은 tests/integration/test_postgres_backup_restore_pg.py와 동일하다
(고유 임시 docker network/컨테이너/볼륨, 러너 컨테이너 경유 접근, 합성 데이터,
테스트 종료 후 전체 정리, 운영 컨테이너/볼륨과는 이름·네트워크가 달라 애초에
접근 불가). 이 파일은 그 파일의 고정장치(runner_image/pg_sandbox)를 그대로
복제해서 쓴다(기존 tests/integration/test_rotate_postgres_password_pg.py도
동일 패턴으로 고정장치를 파일마다 독립적으로 둔다 - 파일 간 결합을 피하기
위한 이 저장소의 기존 방침).

이 파일이 실증하는 것(사용자가 요구한 "실제 PostgreSQL 동시성 검증" 8단계):
1. 마지막 성공 시각을 과거로 시딩 (seed_backup_history 러너 액션)
2. 두 scheduler-start 프로세스를 실제로 동시에 재현 (host에서 별도 스레드로
   두 개의 독립된 "docker run" 컨테이너를 동시에 실행 - 진짜 프로세스 두 개)
3. 실제 dump가 정확히 1건 생성되는지 (backup_dir_listing)
4. BackupHistory·task 실행 이력이 정책대로 기록되는지 (backup_history_summary -
   CATCHUP 1건 + 기존 SCHEDULE 시딩 1건, 나머지 하나는 ALREADY_RUNNING으로
   끝나 새 히스토리를 추가하지 않음)
5. SOURCE DB 지문이 전후 동일한지 (fingerprint 전후 비교 - pg_dump는 읽기전용)
6. pg_restore --list 성공 (run_backup_job() 내부에서 이미 통과해야 SUCCESS가
   된다 - status가 SUCCESS인 것 자체가 이 조건의 증거)
7. PGPASSFILE·.tmp 잔존 0건 (backup_dir_listing)
8. 테스트 자원 완전 정리 (pg_sandbox/runner_image fixture의 finally 블록)
"""

import json
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RUNNER_SCRIPT_REL = "tests/integration/_pg_backup_runner.py"

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI가 없는 환경 - 통합 테스트 skip")


def _docker(*args: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=check,
    )


def _docker_available() -> bool:
    try:
        _docker("info", check=True, timeout=10)
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def runner_image():
    if not _docker_available():
        pytest.skip("Docker 데몬에 연결할 수 없어 통합 테스트를 skip합니다.")

    tag = f"shopping_erp_test/pgbackup_catchup_runner:{uuid.uuid4().hex[:10]}"
    build = subprocess.run(
        ["docker", "build", "-t", tag, "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
    )
    if build.returncode != 0:
        pytest.fail(f"러너 이미지 빌드 실패(exit={build.returncode}). stderr 마지막 2000자:\n{build.stderr[-2000:]}")
    try:
        yield tag
    finally:
        _docker("rmi", "-f", tag, check=False)


@pytest.fixture()
def pg_sandbox(runner_image):
    suffix = uuid.uuid4().hex[:10]
    network = f"pgcatchup_test_net_{suffix}"
    source_container = f"pgcatchup_test_src_{suffix}"
    backup_volume = f"pgcatchup_test_vol_{suffix}"
    source_pg_volume = f"pgcatchup_test_srcdata_{suffix}"
    db_user = "pgcatchup_test_user"
    db_password = f"pgcatchup-test-{suffix}-0000000000000000"
    db_name = "pgcatchup_test_db"

    _docker("network", "create", "--internal", network)
    _docker("volume", "create", backup_volume)
    _docker("volume", "create", source_pg_volume)

    def _start_pg(container: str, volume: str) -> None:
        _docker(
            "run",
            "-d",
            "--name",
            container,
            "--network",
            network,
            "-e",
            f"POSTGRES_USER={db_user}",
            "-e",
            f"POSTGRES_PASSWORD={db_password}",
            "-e",
            f"POSTGRES_DB={db_name}",
            "-v",
            f"{volume}:/var/lib/postgresql/data",
            "postgres:16-alpine",
        )
        deadline = time.time() + 60
        ready = False
        while time.time() < deadline:
            r = _docker("exec", container, "pg_isready", "-U", db_user, check=False)
            if r.returncode == 0:
                ready = True
                break
            time.sleep(1)
        if not ready:
            raise RuntimeError(f"임시 PostgreSQL 컨테이너({container})가 제한 시간 내에 준비되지 않았습니다.")

    try:
        _start_pg(source_container, source_pg_volume)

        yield {
            "network": network,
            "source_container": source_container,
            "backup_volume": backup_volume,
            "db_user": db_user,
            "db_password": db_password,
            "db_name": db_name,
            "source_url": f"postgresql+psycopg://{db_user}:{db_password}@{source_container}:5432/{db_name}",
        }
    finally:
        _docker("rm", "-f", source_container, check=False)
        _docker("volume", "rm", "-f", backup_volume, check=False)
        _docker("volume", "rm", "-f", source_pg_volume, check=False)
        _docker("network", "rm", network, check=False)


def _run_runner(
    runner_image: str,
    sandbox: dict,
    action: str,
    database_url: str,
    extra_env: dict[str, str] | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    env_args: list[str] = [
        "-e",
        f"DATABASE_URL={database_url}",
        "-e",
        f"PGBACKUP_ACTION={action}",
        "-e",
        "JWT_SECRET_KEY=test-only-dummy-jwt-secret-DO-NOT-USE-IN-PRODUCTION-0000",
        "-e",
        "CREDENTIAL_ENCRYPTION_KEY=test-only-dummy-credential-secret-DO-NOT-USE-IN-PRODUCTION-1111",
    ]
    for k, v in (extra_env or {}).items():
        env_args += ["-e", f"{k}={v}"]

    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            sandbox["network"],
            "--user",
            "0:0",
            "--tmpfs",
            "/app/logs",
            "-v",
            f"{REPO_ROOT.as_posix()}:/app:ro",
            "-v",
            f"{sandbox['backup_volume']}:/pgbackup",
            "-w",
            "/app",
            *env_args,
            "-e",
            "MSYS_NO_PATHCONV=1",
            runner_image,
            "python",
            RUNNER_SCRIPT_REL,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if result.returncode != 0 and not result.stdout.strip():
        pytest.fail(
            f"러너 실행 자체가 실패했습니다(exit={result.returncode}). stderr 마지막 2000자:\n{result.stderr[-2000:]}"
        )

    last_line = result.stdout.strip().splitlines()[-1]
    payload = json.loads(last_line)
    if not payload.get("ok"):
        pytest.fail(f"러너 액션 '{action}'이 실패했습니다: {payload.get('error')}")
    return payload


@pytest.fixture()
def seeded_source(runner_image, pg_sandbox):
    _run_runner(runner_image, pg_sandbox, "migrate_and_seed", pg_sandbox["source_url"])
    return pg_sandbox


_CATCHUP_ENV = {
    "POSTGRES_BACKUP_ENABLED": "true",
    "POSTGRES_BACKUP_CATCHUP_ENABLED": "true",
    "POSTGRES_BACKUP_DIR": "/pgbackup",
    "POSTGRES_BACKUP_RETENTION_COUNT": "5",
    "POSTGRES_BACKUP_TIMEOUT_SECONDS": "120",
}


def _most_recent_scheduled_time_utc(now: datetime) -> datetime:
    """services/postgres_backup_service.py의 _most_recent_scheduled_time()과
    동일한 계산을 host 쪽 테스트 코드에서도 그대로 흉내낸다 - "N일 전"처럼
    실제 UTC 벽시계 시각에 의존하는 상수를 시딩에 쓰면, 테스트를 실행하는
    시각이 마침 당일 03:00 UTC 근처일 때 경계값이 뒤집혀 오탐(flaky)이 날 수
    있다(실제로 이 파일 최초 작성 시 이 문제로 실패를 겪었다) - 항상 이
    함수로 "기준 시각"을 먼저 계산한 뒤 그로부터 상대적으로 시딩 시각을
    정해야 실행 시각과 무관하게 결정적이다."""
    scheduled_today = now.replace(hour=3, minute=0, second=0, microsecond=0)
    if now >= scheduled_today:
        return scheduled_today
    return scheduled_today - timedelta(days=1)


class TestCatchupConcurrency:
    def test_two_concurrent_scheduler_starts_produce_exactly_one_backup(self, runner_image, seeded_source):
        sandbox = seeded_source

        # 1. 마지막 성공 시각을 과거(기준 시각보다 확실히 이전인 2일 전)로
        #    시딩한다 - "재기동 시점에 아직 보충되지 않은 상태"를 실제 DB
        #    행으로 만든다. 기준 시각 자체를 먼저 계산한 뒤 상대적으로
        #    빼야(_most_recent_scheduled_time_utc) 테스트 실행 시각이 언제든
        #    경계값이 뒤집히지 않는다.
        reference = _most_recent_scheduled_time_utc(datetime.now(timezone.utc))
        seeded_at = (reference - timedelta(days=2)).replace(tzinfo=None)
        _run_runner(
            runner_image,
            sandbox,
            "seed_backup_history",
            sandbox["source_url"],
            extra_env={"PGBACKUP_SEED_CREATED_AT": seeded_at.isoformat()},
        )

        before_fp = _run_runner(runner_image, sandbox, "fingerprint", sandbox["source_url"])

        # 2. 두 scheduler-start 프로세스를 실제로 동시에 재현한다 - 각각 완전히
        #    독립된 "docker run" 컨테이너(진짜 별도 프로세스)이며, 같은 advisory
        #    lock(같은 SOURCE DB)을 두고 경합한다.
        results: list[dict] = [None, None]  # type: ignore[list-item]
        errors: list[Exception] = []

        def _start(idx: int) -> None:
            try:
                results[idx] = _run_runner(
                    runner_image, sandbox, "run_catchup", sandbox["source_url"], extra_env=_CATCHUP_ENV, timeout=150
                )
            except Exception as exc:  # noqa: BLE001 - 스레드 예외를 메인 스레드로 전달하기 위함.
                errors.append(exc)

        threads = [threading.Thread(target=_start, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=170)

        assert not errors, f"동시 catch-up 실행 중 예외 발생: {errors}"
        assert all(r is not None for r in results), "두 컨테이너 중 하나가 제한 시간 내에 끝나지 않았습니다."

        # 둘 다 성공하거나(먼저 lock을 잡은 쪽 SUCCESS) 하나가 ALREADY_RUNNING이어야
        # 한다 - 어느 쪽이든 "정확히 1건의 실제 백업"만 만든다는 사실은 아래
        # backup_history_summary/backup_dir_listing으로 별도 확인한다. 두 실행이
        # 타이밍상 겹치지 않고 순차 처리됐다면(advisory lock이 매우 짧게 걸렸다
        # 풀리는 경우) 두 번째 실행 시점엔 이미 최신 상태이므로 "skipped_up_to_date"
        # 후보가 될 수도 있다 - 어느 조합이든 SUCCESS는 정확히 1개 이하여야 한다.
        success_count = sum(1 for r in results if r["result"].get("status") == "SUCCESS")
        assert success_count <= 1, f"실제 백업 실행이 2건 이상 발생했습니다: {results}"
        assert success_count >= 1, f"두 시도 모두 백업을 실행하지 않았습니다(정책 위반): {results}"

        # 3+4. BackupHistory 기록 및 실제 dump 파일 개수 확인. run_backup_job()은
        # ALREADY_RUNNING으로 끝난 시도도(실제 dump는 만들지 않지만) 감사 이력으로
        # 남기는 기존 계약이 있으므로(TestRunBackupJobLocking 참고), CATCHUP
        # 트리거 이력이 최대 2건(SUCCESS 1건 + ALREADY_RUNNING 0~1건)일 수
        # 있다 - 여기서 실제로 검증해야 하는 것은 "SUCCESS는 정확히 1건"이다.
        history = _run_runner(runner_image, sandbox, "backup_history_summary", sandbox["source_url"])
        rows = history["rows"]
        catchup_rows = [r for r in rows if r[2] == "CATCHUP"]
        catchup_success_rows = [r for r in catchup_rows if r[1] == "SUCCESS"]
        catchup_other_rows = [r for r in catchup_rows if r[1] != "SUCCESS"]
        assert len(catchup_success_rows) == 1, f"CATCHUP SUCCESS 이력이 정확히 1건이어야 합니다: {rows}"
        assert all(
            r[1] == "ALREADY_RUNNING" for r in catchup_other_rows
        ), f"SUCCESS/ALREADY_RUNNING 외 다른 상태의 CATCHUP 이력이 있습니다(정책 위반): {rows}"
        assert catchup_success_rows[0][0] == "POSTGRES"

        listing = _run_runner(runner_image, sandbox, "backup_dir_listing", sandbox["source_url"])
        assert listing["dump_file_count"] == 1, f"실제 dump 파일은 정확히 1건이어야 합니다: {listing}"

        # 5. SOURCE DB 지문이 전후 동일한지 (pg_dump는 읽기 전용이어야 한다).
        after_fp = _run_runner(runner_image, sandbox, "fingerprint", sandbox["source_url"])
        assert after_fp == before_fp

        # 7. PGPASSFILE·.tmp 잔존 0건.
        assert listing["tmp_file_count"] == 0
        assert listing["pgpass_file_count"] == 0

    def test_repeated_restarts_after_success_create_no_additional_backup(self, runner_image, seeded_source):
        """재기동이 반복돼도(catch-up job이 여러 번 다시 실행돼도) 이미 한 번
        성공한 뒤에는 추가 백업이 생기지 않는다."""
        sandbox = seeded_source

        reference = _most_recent_scheduled_time_utc(datetime.now(timezone.utc))
        seeded_at = (reference - timedelta(days=1)).replace(tzinfo=None)
        _run_runner(
            runner_image,
            sandbox,
            "seed_backup_history",
            sandbox["source_url"],
            extra_env={"PGBACKUP_SEED_CREATED_AT": seeded_at.isoformat()},
        )

        first = _run_runner(runner_image, sandbox, "run_catchup", sandbox["source_url"], extra_env=_CATCHUP_ENV)
        assert first["result"]["status"] == "SUCCESS"

        second = _run_runner(runner_image, sandbox, "run_catchup", sandbox["source_url"], extra_env=_CATCHUP_ENV)
        assert second["result"] == {"skipped_up_to_date": 1}

        third = _run_runner(runner_image, sandbox, "run_catchup", sandbox["source_url"], extra_env=_CATCHUP_ENV)
        assert third["result"] == {"skipped_up_to_date": 1}

        listing = _run_runner(runner_image, sandbox, "backup_dir_listing", sandbox["source_url"])
        assert listing["dump_file_count"] == 1

    def test_manual_success_after_reference_time_prevents_catchup_duplicate(self, runner_image, seeded_source):
        """이미 (수동이든 예약이든) 최근 성공 기록이 기준 시각 이후에 있으면
        catch-up이 중복 백업을 만들지 않는다.

        "N분 전"처럼 실제 벽시계에 의존하는 값으로 시딩하면, 테스트 실행
        시각이 마침 03:00 UTC를 막 넘긴 직후(예: 03:02)일 때 "5분 전"이
        전날 03:00 UTC보다 더 이전이 되어 버려(당일 새 기준 시각보다
        이전이라 latest_success_since가 이 행을 못 찾음) 실제로 catch-up이
        새 백업을 만들어버리는 경계값 flaky가 있었다(2026-09-15 전체
        스위트 실행 중 실제로 이 경계를 넘겨 재현됨). _most_recent_scheduled_time_utc로
        먼저 계산한 기준 시각 그 자체를 시딩하면(latest_success_since는
        `created_at >= since`를 쓰므로) 실행 시각과 무관하게 항상 결정적이다."""
        sandbox = seeded_source

        reference = _most_recent_scheduled_time_utc(datetime.now(timezone.utc))
        recent_success_at = reference.replace(tzinfo=None)
        _run_runner(
            runner_image,
            sandbox,
            "seed_backup_history",
            sandbox["source_url"],
            extra_env={"PGBACKUP_SEED_CREATED_AT": recent_success_at.isoformat()},
        )

        result = _run_runner(runner_image, sandbox, "run_catchup", sandbox["source_url"], extra_env=_CATCHUP_ENV)
        assert result["result"] == {"skipped_up_to_date": 1}

        listing = _run_runner(runner_image, sandbox, "backup_dir_listing", sandbox["source_url"])
        assert listing["dump_file_count"] == 0

    def test_catchup_disabled_flag_makes_no_changes(self, runner_image, seeded_source):
        sandbox = seeded_source
        before_fp = _run_runner(runner_image, sandbox, "fingerprint", sandbox["source_url"])

        env = dict(_CATCHUP_ENV)
        env["POSTGRES_BACKUP_CATCHUP_ENABLED"] = "false"
        result = _run_runner(runner_image, sandbox, "run_catchup", sandbox["source_url"], extra_env=env)
        assert result["result"] == {"skipped_disabled": 1}

        after_fp = _run_runner(runner_image, sandbox, "fingerprint", sandbox["source_url"])
        assert after_fp == before_fp

        listing = _run_runner(runner_image, sandbox, "backup_dir_listing", sandbox["source_url"])
        assert listing["dump_file_count"] == 0

    def test_cron_and_catchup_racing_produce_exactly_one_dump(self, runner_image, seeded_source):
        """운영 시나리오 재현: 03:00 UTC 정기 cron(backup_job.run(), trigger_type
        기본값 SCHEDULE)과 재기동 직후 startup catch-up(run_catchup_if_needed(),
        trigger_type CATCHUP)이 우연히 같은 순간 실행되는 경우 - 서로 다른
        진입점(run() vs run_catchup_if_needed())이지만 결국 같은
        _run_backup_locked()/advisory lock을 공유하므로, 두 진입점을 진짜
        서로 다른 두 프로세스(docker run 컨테이너)로 동시에 실행해도 실제
        dump는 정확히 1건만 만들어져야 한다."""
        sandbox = seeded_source

        reference = _most_recent_scheduled_time_utc(datetime.now(timezone.utc))
        seeded_at = (reference - timedelta(days=2)).replace(tzinfo=None)
        _run_runner(
            runner_image,
            sandbox,
            "seed_backup_history",
            sandbox["source_url"],
            extra_env={"PGBACKUP_SEED_CREATED_AT": seeded_at.isoformat()},
        )

        before_fp = _run_runner(runner_image, sandbox, "fingerprint", sandbox["source_url"])

        results: list = [None, None]
        errors: list[Exception] = []

        def _start(idx: int, action: str) -> None:
            try:
                results[idx] = _run_runner(
                    runner_image, sandbox, action, sandbox["source_url"], extra_env=_CATCHUP_ENV, timeout=150
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=_start, args=(0, "run_backup")),
            threading.Thread(target=_start, args=(1, "run_catchup")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=170)

        assert not errors, f"cron/catch-up 동시 실행 중 예외 발생: {errors}"
        assert all(r is not None for r in results), "두 컨테이너 중 하나가 제한 시간 내에 끝나지 않았습니다."

        # run_backup 액션의 결과는 항상 dict(SUCCESS/ALREADY_RUNNING)이고,
        # run_catchup 액션의 결과는 정책상 후보가 아니면 skipped_up_to_date일
        # 수도 있다 - 실제 dump 파일 개수와 history로 최종 판정한다.
        listing = _run_runner(runner_image, sandbox, "backup_dir_listing", sandbox["source_url"])
        assert listing["dump_file_count"] == 1, f"cron/catch-up 경합으로 dump가 1건이 아닙니다: {listing}, {results}"
        assert listing["tmp_file_count"] == 0
        assert listing["pgpass_file_count"] == 0

        # backup_history_summary는 engine=POSTGRES인 모든 행을 반환한다 - 여기에는
        # 이번 레이스가 만든 새 행뿐 아니라 이 테스트가 맨 앞에서 seed_backup_history로
        # 미리 심어둔 과거 SUCCESS/SCHEDULE 행도 포함된다("아직 보충되지 않은 상태"를
        # 만들기 위한 사전조건이었다). 따라서 "새로 생성된 성공 백업이 정확히 1건"은
        # 전체 SUCCESS 개수가 (시딩 1건 + 레이스 결과 1건) = 2여야 한다는 것으로
        # 검증한다 - 만약 레이스가 dump를 2번 만들었다면 SUCCESS가 3건이 됐을 것이다.
        history = _run_runner(runner_image, sandbox, "backup_history_summary", sandbox["source_url"])
        rows = history["rows"]
        success_rows = [r for r in rows if r[1] == "SUCCESS"]
        assert len(success_rows) == 2, f"SUCCESS 이력이 (시딩 1건 + 새 백업 1건) 2건이어야 합니다: {rows}"
        non_success_rows = [r for r in rows if r[1] != "SUCCESS"]
        assert all(
            r[1] == "ALREADY_RUNNING" for r in non_success_rows
        ), f"SUCCESS/ALREADY_RUNNING 외 상태가 있습니다: {rows}"
        assert (
            len(rows) == 3
        ), f"seed 1건 + cron/catch-up 경합 결과(SUCCESS 1건 + ALREADY_RUNNING 1건)=3건이어야 합니다: {rows}"

        after_fp = _run_runner(runner_image, sandbox, "fingerprint", sandbox["source_url"])
        assert after_fp == before_fp
