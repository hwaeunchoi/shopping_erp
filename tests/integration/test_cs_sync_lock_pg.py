"""
tests/integration/test_cs_sync_lock_pg.py
--------------------------------------------------
CS 문의 동기화 (platform, source) advisory lock과 이를 쓰는 catch-up 서비스·stale 작업 정리·수동
sync 경로를 실제 PostgreSQL 두 커넥션 경합으로 검증한다 - 운영 erp-postgres가 아니라 이 테스트
전용 1회성 컨테이너를 쓴다.

격리 원칙(tests/integration/test_cs_case_concurrency_pg.py와 동일): 고유 임시 docker network
(--internal)/컨테이너/볼륨(uuid 접미사)만 사용하고, 같은 network의 "러너" 컨테이너(shopping_erp-api
이미지, 레포 읽기전용 마운트)에서 시나리오를 실행해 JSON 한 줄만 host에서 파싱한다. 합성 데이터만
사용하고, 종료 후(성공/실패 무관) 전부 제거한다. Docker CLI가 없으면 skip한다.
"""

import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RUNNER_SCRIPT_REL = "tests/integration/_cs_sync_lock_runner.py"
RUNNER_IMAGE = "shopping_erp-api:latest"

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI가 없는 환경 - 통합 테스트 skip")


def _docker(*args: str, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess:
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


@pytest.fixture()
def pg_sandbox():
    if not _docker_available():
        pytest.skip("Docker 데몬에 연결할 수 없어 통합 테스트를 skip합니다.")

    suffix = uuid.uuid4().hex[:10]
    network = f"cslock_test_net_{suffix}"
    volume = f"cslock_test_vol_{suffix}"
    container = f"cslock_test_db_{suffix}"
    db_user = "cslock_test_user"
    db_password = f"cslock-{suffix}-pw"
    db_name = "cslock_test_db"

    _docker("network", "create", "--internal", network)
    _docker("volume", "create", volume)

    try:
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
            raise RuntimeError("임시 PostgreSQL 컨테이너가 제한 시간 내에 준비되지 않았습니다.")

        yield {
            "network": network,
            "container": container,
            "db_user": db_user,
            "db_name": db_name,
            "db_password": db_password,
        }
    finally:
        _docker("rm", "-f", container, check=False)
        _docker("volume", "rm", "-f", volume, check=False)
        _docker("network", "rm", network, check=False)


def _run_scenario(sandbox: dict, scenario: str, timeout: int = 120) -> dict[str, Any]:
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            sandbox["network"],
            "-v",
            f"{REPO_ROOT.as_posix()}:/app:ro",
            "-w",
            "/app",
            "-e",
            f"CSLOCK_DB_HOST={sandbox['container']}",
            "-e",
            f"CSLOCK_DB_USER={sandbox['db_user']}",
            "-e",
            f"CSLOCK_DB_PASSWORD={sandbox['db_password']}",
            "-e",
            f"CSLOCK_DB_NAME={sandbox['db_name']}",
            "-e",
            f"CSLOCK_SCENARIO={scenario}",
            "-e",
            f"JWT_SECRET_KEY=cslock-test-jwt-{uuid.uuid4().hex}{uuid.uuid4().hex}",
            "-e",
            f"CREDENTIAL_ENCRYPTION_KEY=cslock-test-cred-{uuid.uuid4().hex}{uuid.uuid4().hex}",
            "-e",
            "MSYS_NO_PATHCONV=1",
            RUNNER_IMAGE,
            "python",
            RUNNER_SCRIPT_REL,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    assert result.returncode == 0, (
        f"runner 컨테이너가 비정상 종료(exit={result.returncode})했습니다.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestAdvisoryLockContention:
    def test_same_platform_and_source_is_exclusive_others_are_independent(self, pg_sandbox):
        p = _run_scenario(pg_sandbox, "lock_contention")

        assert p["first_acquired"] is True, p
        assert p["same_source_second_acquired"] is False, p  # 같은 (platform, source)는 하나만
        assert p["other_source_acquired"] is True, p  # 다른 source는 막지 않는다
        assert p["other_platform_acquired"] is True, p  # 다른 platform도 막지 않는다
        assert p["reacquired_after_release"] is True, p  # 정상 해제 후 다시 잡힌다

    def test_lock_is_freed_when_the_holder_connection_dies(self, pg_sandbox):
        p = _run_scenario(pg_sandbox, "lock_contention")

        assert p["raw_held"] is True, p
        assert p["acquired_while_raw_connection_holds_it"] is False, p
        assert p["acquired_after_holder_connection_closed"] is True, p  # 크래시 후 자동 해제


class TestConcurrentCatchupRuns:
    def test_two_simultaneous_runs_execute_each_source_exactly_once(self, pg_sandbox):
        p = _run_scenario(pg_sandbox, "concurrent_sync_platform")

        assert p["a"]["exception"] is None, p
        assert p["b"]["exception"] is None, p
        assert p["attempts"] == 4, p  # 두 worker x 두 source의 잠금 시도가 모두 fetch 전에 끝났다(결정적 겹침)
        for source in ("COUPANG_CALL_CENTER", "COUPANG_PRODUCT_INQUIRY"):
            statuses = sorted([p["a"]["by_source"][source], p["b"]["by_source"][source]])
            assert statuses == ["ALREADY_RUNNING", "SUCCESS"], (source, p)
            fetches = p["a"]["calls"].count(source) + p["b"]["calls"].count(source)
            assert fetches == 1, (source, p)  # 외부 호출은 승자 한 쪽만
        # DB: source마다 case 정확히 1건, checkpoint도 source마다 1행(진행 위치 있음).
        assert p["case_counts"] == {"COUPANG_CALL_CENTER": 1, "COUPANG_PRODUCT_INQUIRY": 1}, p
        assert len(p["checkpoint_codes"]) == 2, p
        assert p["checkpoints_all_have_progress"] is True, p


class TestStaleRecoveryRespectsRealLocks:
    def test_running_row_is_kept_while_lock_held_and_recovered_after(self, pg_sandbox):
        p = _run_scenario(pg_sandbox, "stale_probe_sees_real_lock")

        assert p["lock_acquired"] is True, p
        assert p["during_lock"] == {"recovered": 0, "skipped_active": 1}, p
        assert p["status_during_lock"] == "RUNNING", p
        assert p["after_release"] == {"recovered": 1, "skipped_active": 0}, p
        assert p["final_status"] == "FAILED", p
        assert p["final_error_message"] == "PROCESS_INTERRUPTED", p


class TestManualSyncVersusAutomaticRun:
    def test_manual_style_all_source_lock_is_refused_while_automatic_run_is_in_progress(self, pg_sandbox):
        p = _run_scenario(pg_sandbox, "manual_vs_running_sync")

        assert p["auto"] == "SUCCESS", p
        assert p["manual_all_acquired"] is False, p
        assert p["manual_acquired_per_source"] == [False, False], p
        assert p["manual_all_acquired_after_auto_finished"] is True, p


class TestSegmentCommitAtomicityOnPostgres:
    def test_commit_failure_leaves_no_partial_state_and_rerun_converges(self, pg_sandbox):
        p = _run_scenario(pg_sandbox, "atomic_segment_commit_failure")

        # 첫 구간(콜센터)의 commit이 실패 -> 그 source의 case/history/checkpoint는 하나도 남지 않는다.
        assert p["first_by_source"]["COUPANG_CALL_CENTER"] == "FAILED", p
        assert p["first_reason_codes"]["COUPANG_CALL_CENTER"] == "COMMIT_FAILED:RuntimeError", p
        # 다른 source(상품별)는 영향 없이 정상 commit - case 1, history 1, checkpoint 1.
        assert p["first_by_source"]["COUPANG_PRODUCT_INQUIRY"] == "SUCCESS", p
        assert p["after_first"] == {"cases": 1, "history": 1, "success_checkpoints": 1}, p
        # 재실행하면 실패했던 source도 따라와 source마다 정확히 1건(중복 없음)으로 수렴한다.
        assert p["second_status"] == "SUCCESS", p
        assert p["after_second"]["cases"] == 2, p
        assert p["after_second"]["success_checkpoints"] == 2, p


FIXED_NOW_ISO = "2026-10-07T01:00:00"  # 러너가 고정한 시계(KST 2026-10-07 10:00)
CHUNK1_END_ISO = "2026-09-30T15:00:00"  # 2026-10-01 00:00 KST


def _counts(snapshot: dict, short: str) -> tuple[int, int, object]:
    row = snapshot[short]
    return row["cases"], row["history"], row["checkpoint"]


class TestAtomicityMatrixOnPostgres:
    """실패 유형별로 "다른 커넥션에서 보이는(커밋된)" case/history/checkpoint 행 수를 검증한다.
    표기: (case, history, checkpoint)  CC=콜센터, PI=상품별."""

    @pytest.fixture()
    def matrix(self, pg_sandbox):
        return _run_scenario(pg_sandbox, "atomicity_matrix", timeout=240)["matrix"]

    def test_a_failure_after_case_flush_but_before_checkpoint_leaves_nothing_then_rerun_converges(self, matrix):
        m = matrix["A_fail_before_checkpoint"]
        assert m["first"]["CC"]["status"] == "FAILED" and m["first"]["PI"]["status"] == "FAILED", m
        assert m["first"]["CC"]["reason"] == "COMMIT_FAILED:RuntimeError", m
        assert _counts(m["after_first"], "CC") == (0, 0, None), m
        assert _counts(m["after_first"], "PI") == (0, 0, None), m
        assert _counts(m["after_rerun"], "CC") == (1, 1, FIXED_NOW_ISO), m
        assert _counts(m["after_rerun"], "PI") == (1, 1, FIXED_NOW_ISO), m

    def test_b_commit_failure_after_checkpoint_flush_is_isolated_to_the_first_source(self, matrix):
        m = matrix["B_flush_then_commit_failure"]
        assert m["first"]["CC"]["status"] == "FAILED", m
        assert m["first"]["CC"]["reason"] == "COMMIT_FAILED:RuntimeError", m
        assert m["first"]["PI"]["status"] == "SUCCESS", m
        assert _counts(m["after_first"], "CC") == (0, 0, None), m  # flush된 checkpoint도 남지 않는다
        assert _counts(m["after_first"], "PI") == (1, 1, FIXED_NOW_ISO), m  # 두 번째 source는 정상 반영
        assert _counts(m["after_rerun"], "CC") == (1, 1, FIXED_NOW_ISO), m
        assert _counts(m["after_rerun"], "PI") == (1, 1, FIXED_NOW_ISO), m  # 중복 없이 수렴

    def test_c_chunk2_failure_keeps_chunk1_and_checkpoint_at_end_of_chunk1(self, matrix):
        m = matrix["C_chunk2_fails_after_chunk1"]
        assert m["first"]["CC"]["status"] == "PARTIAL_SUCCESS", m
        assert m["first"]["CC"]["reason"] == "SERVER_ERROR", m
        assert _counts(m["after_first"], "CC") == (1, 1, CHUNK1_END_ISO), m
        assert m["rerun"]["CC"]["status"] == "SUCCESS", m
        assert _counts(m["after_rerun"], "CC") == (2, 2, FIXED_NOW_ISO), m

    def test_d_item_level_database_error_keeps_good_items_and_does_not_advance(self, matrix):
        m = matrix["D_item_failure_failed_gt_0"]
        assert m["first"]["CC"]["status"] == "PARTIAL_SUCCESS", m
        assert _counts(m["after_first"], "CC") == (1, 1, None), m
        assert m["rerun"]["CC"]["status"] == "SUCCESS", m
        assert _counts(m["after_rerun"], "CC") == (1, 1, FIXED_NOW_ISO), m  # good은 갱신으로 수렴, 중복 없음

    def test_e_page_limit_exceeded_writes_nothing_for_that_source(self, matrix):
        m = matrix["E_page_limit_exceeded"]
        assert m["first"]["CC"] == {"status": "FAILED", "reason": "PAGE_LIMIT_EXCEEDED"}, m
        assert _counts(m["after_first"], "CC") == (0, 0, None), m
        assert _counts(m["after_first"], "PI") == (1, 1, FIXED_NOW_ISO), m
        assert _counts(m["after_rerun"], "CC") == (1, 1, FIXED_NOW_ISO), m

    def test_f_request_budget_exceeded_writes_nothing_and_defers_the_other_source(self, matrix):
        m = matrix["F_request_budget_exceeded"]
        assert m["first"]["CC"] == {"status": "FAILED", "reason": "REQUEST_BUDGET_EXCEEDED"}, m
        assert m["first"]["PI"]["status"] == "DEFERRED", m
        assert _counts(m["after_first"], "CC") == (0, 0, None), m
        assert _counts(m["after_first"], "PI") == (0, 0, None), m
        assert _counts(m["after_rerun"], "CC") == (1, 1, FIXED_NOW_ISO), m
        assert _counts(m["after_rerun"], "PI") == (1, 1, FIXED_NOW_ISO), m

    def test_g_base_exception_before_commit_leaves_nothing_frees_locks_and_rerun_converges(self, matrix):
        m = matrix["G_base_exception_before_commit"]
        assert m["raised_system_exit"] is True, m
        assert _counts(m["after_first"], "CC") == (0, 0, None), m
        assert _counts(m["after_first"], "PI") == (0, 0, None), m
        assert m["locks_free_after_death"] is True, m
        assert _counts(m["after_rerun"], "CC") == (1, 1, FIXED_NOW_ISO), m
        assert _counts(m["after_rerun"], "PI") == (1, 1, FIXED_NOW_ISO), m


class TestStaleRecoveryTwoInstances:
    def test_two_instances_recovering_the_same_row_end_in_the_same_state_without_errors(self, pg_sandbox):
        p = _run_scenario(pg_sandbox, "stale_concurrent_recovery")

        assert "exception" not in p["a"] and "exception" not in p["b"], p
        assert p["final_status"] == "FAILED", p
        assert p["final_error_message"] == "PROCESS_INTERRUPTED", p
        assert p["finished_at_set"] is True, p


class TestInt4BoundaryPlatformIds:
    def test_checkpoint_and_lock_work_for_the_whole_integer_range(self, pg_sandbox):
        p = _run_scenario(pg_sandbox, "int4_boundary_ids")

        for pid, row in p["ids"].items():
            for source in ("COUPANG_CALL_CENTER", "COUPANG_PRODUCT_INQUIRY"):
                assert row[f"{source}:held_then_second_attempt"] == [True, False], (pid, row)  # 같은 키는 하나만
                assert row[f"{source}:sibling_source_acquired"] is True, (pid, row)  # 다른 source는 막지 않는다
                assert row[f"{source}:reacquired_after_release"] is True, (pid, row)  # unlock이 같은 두 키로 해제
        assert p["adjacent_platforms_independent"] == [True, True], p
        assert p["checkpoint_rows_stored"] == p["expected_rows"] == 10, p
        assert p["max_code_length"] <= 13 <= 30, p  # varchar(30) 안, 가장 긴 키 "2147483647:CC"


class TestCrossDomainAdvisoryLocks:
    def test_domains_do_not_block_each_other_and_release_on_every_exit_path(self, pg_sandbox):
        from core.advisory_locks import EXTERNAL_COMMAND_CLASSID

        p = _run_scenario(pg_sandbox, "cross_domain_locks")

        # 실제 PostgreSQL 잠금 테이블에 registry가 만든 키가 그대로 올라간다.
        assert p["advisory_rows_in_pg_locks"] == [p["registry_key"]], p
        assert p["registry_key"][0] == EXTERNAL_COMMAND_CLASSID and p["classid_is_fixed_external_value"] is True, p
        # 같은 영역: 같은 자원(형제 집합의 최소 id 포함)만 직렬화한다.
        assert p["same_resource_blocked"] is True, p
        assert p["sibling_set_min_id_blocked"] == [True, True], p
        assert p["other_id_not_blocked"] is True, p
        assert p["other_target_type_same_id_not_blocked"] is True, p
        assert p["huge_ids_ok"] is True, p
        # 다른 영역: objid가 같아도 서로 막지 않는다.
        assert p["cs_locks_with_equal_objid_acquired_while_external_held"] == [True, True], p
        assert p["backup_lock_acquired_while_external_held"] is True, p
        assert p["external_not_blocked_while_cs_held_with_equal_objid"] == [True, True], p
        assert p["external_not_blocked_while_backup_held"] is True, p
        # 해제: 커밋 / 롤백 / 예외 / BaseException / 연결 종료.
        r = p["release"]
        assert r["after_commit"] is True, p
        assert r["after_rollback"] is True, p
        assert r["after_exception_and_session_close"] is True, p
        assert r["base_exception_type"] is True and r["after_base_exception_and_session_close"] is True, p
        assert r["after_connection_killed"] is True, p


class TestFailureRecordResilienceOnPostgres:
    """_fail()/실패 기록 경로 - 표기: (case, history, checkpoint)  CC=콜센터, PI=상품별."""

    @pytest.fixture()
    def res(self, pg_sandbox):
        return _run_scenario(pg_sandbox, "failure_record_resilience", timeout=240)

    def test_no_secret_or_pii_reaches_logs_db_or_results(self, res):
        assert res["sentinel_leaked"] is False, res
        for name, case in res["cases"].items():
            assert case["sentinel_leaked"] is False, name

    def test_1_fetch_failure_plus_record_commit_failure_returns_the_original_reason(self, res):
        c = res["cases"]["1_fetch_fail_and_record_commit_fail"]
        assert c["record_commit_failures"] == 2, c
        assert c["summary"] == {
            "CC": {"status": "FAILED", "reason": "SERVER_ERROR"},
            "PI": {"status": "FAILED", "reason": "SERVER_ERROR"},
        }, c
        assert _counts(c["snapshot"], "CC") == (0, 0, None) and _counts(c["snapshot"], "PI") == (0, 0, None), c
        assert c["status_rows"] == {"CC": None, "PI": None}, c  # 기록 실패가 checkpoint 행도 만들지 않는다
        assert all("type=RuntimeError" in m for m in c["log_messages"]) and len(c["log_messages"]) == 2, c

    def test_2_first_source_record_failure_does_not_stop_the_second_source_and_the_rerun_succeeds(self, res):
        c = res["cases"]["2_first_source_record_fails_second_succeeds_then_rerun"]
        assert c["summary"]["CC"] == {"status": "FAILED", "reason": "SERVER_ERROR"}, c
        assert c["summary"]["PI"]["status"] == "SUCCESS", c
        assert _counts(c["after_first"]["snapshot"], "CC") == (0, 0, None), c
        assert _counts(c["after_first"]["snapshot"], "PI") == (1, 1, FIXED_NOW_ISO), c
        assert c["after_first"]["status_rows"]["CC"] is None, c
        assert c["rerun_same_session"]["CC"]["status"] == "SUCCESS", c  # 같은 세션이 다음 실행에서 정상
        assert _counts(c["snapshot"], "CC") == (1, 1, FIXED_NOW_ISO), c
        assert _counts(c["snapshot"], "PI") == (1, 1, FIXED_NOW_ISO), c  # 재실행해도 중복 없음

    def test_3_real_connection_kill_never_raises_and_the_session_stays_usable(self, res):
        c = res["cases"]["3_connection_killed_record_and_rollback_fail"]
        assert c["raised"] is None and "rerun_raised" not in c, c
        assert c["summary"]["PI"]["status"] == "SUCCESS", c
        assert c["rerun_same_session"]["CC"]["status"] == "SUCCESS", c
        assert _counts(c["snapshot"], "CC") == (1, 1, FIXED_NOW_ISO), c

    @pytest.mark.parametrize(
        "name,reason",
        [
            ("3b_call_site_rollback_fails_after_fetch_failure", "SERVER_ERROR"),
            ("3c_commit_fails_then_rollback_fails", "COMMIT_FAILED:RuntimeError"),
            ("3d_unexpected_exception_then_rollback_fails", "INTERNAL_ERROR:ZeroDivisionError"),
        ],
    )
    def test_3x_rollback_itself_failing_keeps_the_reason_and_the_other_source(self, res, name, reason):
        c = res["cases"][name]
        assert c["rollback_failures"] == 1 and "raised" not in c, c
        assert c["summary"]["CC"] == {"status": "FAILED", "reason": reason}, c
        assert c["summary"]["PI"]["status"] == "SUCCESS", c
        assert _counts(c["snapshot"], "CC") == (0, 0, None), c
        assert _counts(c["snapshot"], "PI") == (1, 1, FIXED_NOW_ISO), c
        assert c["status_rows"]["CC"] == {"status": "ERROR", "error": reason, "covered": None}, c
        assert any("rollback 실패: type=RuntimeError" in m for m in c["log_messages"]), c

    def test_4_error_then_success_returns_to_normal_and_clears_the_message_in_the_same_commit(self, res):
        c = res["cases"]["4_error_recorded_then_success_recovers_to_normal"]
        assert c["after_first"]["status_rows"]["CC"] == {"status": "ERROR", "error": "SERVER_ERROR", "covered": None}, c
        assert c["status_rows"]["CC"] == {"status": "NORMAL", "error": None, "covered": FIXED_NOW_ISO}, c
        assert _counts(c["snapshot"], "CC") == (1, 1, FIXED_NOW_ISO), c

    def test_5_commit_failure_during_recovery_keeps_error_and_the_next_run_recovers(self, res):
        c = res["cases"]["5_recovery_commit_fails_then_recovers"]
        assert c["record_commit_failures"] == 1, c
        assert c["recovery_attempt"]["CC"] == {"status": "FAILED", "reason": "COMMIT_FAILED:RuntimeError"}, c
        after = c["after_recovery_attempt"]
        assert _counts(after["snapshot"], "CC") == (0, 0, None), c  # 데이터·checkpoint 전진 모두 롤백
        assert after["status_rows"]["CC"]["status"] == "ERROR", c  # NORMAL로 먼저 바뀌지 않는다
        assert c["status_rows"]["CC"] == {"status": "NORMAL", "error": None, "covered": FIXED_NOW_ISO}, c
        assert _counts(c["snapshot"], "CC") == (1, 1, FIXED_NOW_ISO), c


class TestPartialSuccessContractAOnPostgres:
    def test_success_item_is_kept_checkpoint_stalls_rerun_has_no_duplicates_and_recovers_when_fixed(self, pg_sandbox):
        p = _run_scenario(pg_sandbox, "partial_success_contract_a", timeout=240)

        # 첫 실행: 성공 항목 1개만 저장, checkpoint 미전진, ERROR 상태(안전한 코드만).
        assert p["run1"]["CC"] == {"status": "PARTIAL_SUCCESS", "reason": "PARTIAL_SUCCESS"}, p
        assert p["run1"]["PI"]["status"] == "SUCCESS", p  # 다른 source는 영향 없음
        assert _counts(p["after_run1"]["snapshot"], "CC") == (1, 1, None), p
        assert p["after_run1"]["status_rows"]["CC"] == {
            "status": "ERROR",
            "error": "PARTIAL_SUCCESS",
            "covered": None,
        }, p
        # 두 번째 실행(로컬 필드를 수동 변경한 뒤): 중복 없음, 여전히 미전진, ERROR 유지.
        assert p["run2"]["CC"]["status"] == "PARTIAL_SUCCESS", p
        assert _counts(p["after_run2"]["snapshot"], "CC") == (1, 1, None), p
        assert p["after_run2"]["status_rows"]["CC"]["status"] == "ERROR", p
        # 실패 항목이 정상화된 실행: checkpoint 전진, NORMAL 복구, 오류 메시지 제거, case/history 중복 없음.
        assert p["run3"]["CC"]["status"] == "SUCCESS", p
        assert _counts(p["after_run3"]["snapshot"], "CC") == (2, 2, FIXED_NOW_ISO), p
        assert p["after_run3"]["status_rows"]["CC"] == {"status": "NORMAL", "error": None, "covered": FIXED_NOW_ISO}, p
        # 로컬 필드는 두 번의 재실행에도 보존되고, 새 항목은 기본값이다.
        assert p["cases"] == [
            ["good", "LOCAL-TAG", "LOCAL-DRAFT", "HIGH", "IN_PROGRESS"],
            ["poison-fixed", None, None, "NORMAL", "OPEN"],
        ], p
