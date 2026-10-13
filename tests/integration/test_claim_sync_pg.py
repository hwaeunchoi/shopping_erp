"""
tests/integration/test_claim_sync_pg.py
----------------------------------------------
교환·반품(클레임) 수집의 저장 안전성을 실제 PostgreSQL(1회성 격리 컨테이너)에서 검증한다 - unique 제약 경합,
commit 실패 롤백, 미매칭 승격, 기능별 실패 격리, 취소 후보 cursor 순환. 샌드박스/러너 컨테이너 방식은
test_cs_sync_lock_pg.py와 같다(운영 DB·실제 쿠팡 API 미사용, 합성 데이터만).
"""

from tests.integration.test_cs_sync_lock_pg import _run_scenario as _run  # noqa: F401
from tests.integration.test_cs_sync_lock_pg import pg_sandbox  # noqa: F401

SCRIPT = "tests/integration/_claim_sync_pg_runner.py"


def _scenario(sandbox, name):
    return _run(sandbox, name, timeout=240, script=SCRIPT)


class TestConcurrencyAndConstraints:
    def test_two_sessions_saving_the_same_claim_leave_exactly_one_row(self, request):
        p = _scenario(request.getfixturevalue("pg_sandbox"), "concurrent_same_claim")
        workers = p["workers"]
        assert set(workers) == {"a", "b"} and all(w["exception"] is None for w in workers.values()), p
        assert p["counts"]["returns"] == 1, p  # (order_id, platform_claim_id) unique
        allowed = {("SUCCESS", None), ("FAILED", "DB_WRITE_FAILED")}
        assert {(w["returns"], w["reason"]) for w in workers.values()} <= allowed, p


class TestTransactionPolicy:
    def test_commit_failure_rolls_everything_back_and_a_rerun_converges(self, request):
        p = _scenario(request.getfixturevalue("pg_sandbox"), "commit_failure")
        assert p["commit_failed"] is True, p
        assert p["after_failure"] == {"returns": 0, "exchanges": 0, "unmatched": 0, "unmatched_resolved": 0}, p
        assert p["after_rerun"] == {"returns": 1, "exchanges": 1, "unmatched": 1, "unmatched_resolved": 0}, p

    def test_one_failed_feature_does_not_block_the_other_and_the_next_run_recovers_it(self, request):
        p = _scenario(request.getfixturevalue("pg_sandbox"), "partial_failure")
        assert p["first"] == {"returns": "FAILED", "exchanges": "SUCCESS", "overall": "PARTIAL"}, p
        assert p["first_reason"] == "PAGE_LIMIT_EXCEEDED", p
        assert (p["after_first"]["returns"], p["after_first"]["exchanges"]) == (0, 1), p  # 실패 기능은 부분 저장 없음
        assert p["second"] == {"returns": "SUCCESS", "exchanges": "SUCCESS"}, p
        assert (p["after_second"]["returns"], p["after_second"]["exchanges"]) == (1, 1), p  # 재수집해도 중복 없음


class TestUnmatchedPreservation:
    def test_claim_without_order_is_held_and_promoted_once_the_order_exists(self, request):
        p = _scenario(request.getfixturevalue("pg_sandbox"), "unmatched_then_promoted")
        assert p["held"] == {"returns": 0, "exchanges": 0, "unmatched": 1, "unmatched_resolved": 0}, p
        assert p["resolved_unmatched"] == 1, p
        assert p["after_promotion"]["returns"] == 1 and p["after_promotion"]["unmatched_resolved"] == 1, p
        assert p["after_third_run"]["returns"] == 1 and p["after_third_run"]["unmatched"] == 1, p  # 중복 없음


class TestCancellationRotation:
    def test_every_candidate_is_checked_within_one_rotation_and_a_bad_order_never_stalls_it(self, request):
        p = _scenario(request.getfixturevalue("pg_sandbox"), "cancellation_rotation")
        assert p["distinct_checked"] == 45, p  # 20 x 3회로 45개 후보를 한 바퀴
        assert p["total_lookups"] == 60, p  # 마지막 회차는 처음으로 되감겨 20개를 채운다
        assert p["skipped_failed"] >= 1, p  # 영구 오류 주문은 건너뛰어졌다
        assert p["cursor_rows"] == 1, p  # (platform, claim_type) unique cursor


class TestPlatformLock:
    def test_job_and_manual_sync_share_one_lock_so_requests_and_cursor_are_not_doubled(self, request):
        p = _scenario(request.getfixturevalue("pg_sandbox"), "job_lock_exclusion")
        assert p["second_status"] == ["ALREADY_RUNNING"], p  # 두 번째 job은 아무 것도 하지 않고 종료
        assert p["manual_acquired_while_job_runs"] is False, p  # 수동 sync-claims도 같은 잠금에서 막힌다
        assert p["return_fetches"] == 1 and p["exchange_fetches"] == 1, p  # 외부 목록 조회는 1회분
        assert p["lookups"] == 5 == p["distinct_lookups"], p  # 취소 후보도 각 1번(이중 처리 없음)
        assert p["cursor_is_last_order"] is True, p
        assert p["manual_acquired_after"] is True and p["advisory_locks_left"] == 0, p  # 잔류 없음


class TestCancellationCursorTransaction:
    def test_cursor_moves_only_with_the_commit_and_a_rerun_rechecks_the_same_orders(self, request):
        p = _scenario(request.getfixturevalue("pg_sandbox"), "cursor_commit_failure")
        assert p["commit_failed"] is True, p
        assert p["cursor_after_failure"] in (None, 0), p  # 데이터와 함께 롤백 - 전진하지 않았다
        assert p["same_orders_rechecked"] is True and len(p["lookups"]) == 3, p
        assert p["cursor_after_rerun_is_third_order"] is True, p


class TestUnmatchedPromotionIsolation:
    def test_a_failing_promotion_is_skipped_atomically_and_does_not_stop_the_rest(self, request):
        p = _scenario(request.getfixturevalue("pg_sandbox"), "promotion_poison")
        assert p["run_status"] != "FAILED", p  # 수집 전체가 막히지 않았다
        assert p["resolved_in_poison_run"] == 1, p  # 정상 항목만 승격
        # 독성 항목은 claim 행도 남기지 않았고(반쪽 승격 없음) 해결 표시도 없다.
        assert p["after_poison_run"]["returns"] == 1 and p["after_poison_run"]["unmatched_resolved"] == 1, p
        assert p["resolved_after_recovery"] == 1, p  # 원인이 사라지면 다음 실행에서 승격
        assert p["after_recovery"]["returns"] == 2 and p["after_recovery"]["unmatched_resolved"] == 2, p


class TestClaimLockDomainOnPostgres:
    def test_platforms_are_independent_and_the_lock_is_freed_on_every_exit_path(self, request):
        p = _scenario(request.getfixturevalue("pg_sandbox"), "claim_lock_domain")
        assert p["classid_is_claim"] is True and p["holder_acquired"] is True, p
        assert p["same_platform_blocked"] is True, p  # 같은 플랫폼의 패자는 대기 없이 거절
        assert p["other_platform_not_blocked"] is True and p["two_platforms_held_together"] is True, p
        assert p["free_after_with"] is True, p
        assert p["after_exception"] is True, p
        assert p["base_exception_type"] is True and p["after_base_exception"] is True, p
        assert p["kill_exit_clean"] is True and p["after_connection_killed"] is True, p
        assert p["advisory_locks_left"] == 0, p


class TestCancellationPresetOnPostgres:
    def test_commit_failure_rolls_back_the_cursor_and_the_lock_is_shared(self, request):
        p = _scenario(request.getfixturevalue("pg_sandbox"), "cancel_preset")
        assert p["commit_failure_status"] == [500, "DB_WRITE_FAILED"], p
        assert p["cursor_after_failure"] in (None, 0), p  # 데이터와 함께 롤백 - 전진하지 않았다
        assert p["lookups_in_failed_run"] == 2, p
        assert p["rerun_status"] == 200 and p["rerun_requests_used"] == 2 and p["rerun_checked"] == 2, p
        assert p["rerun_same_orders_as_failed_run"] is True, p  # 실패한 실행과 같은 주문부터 다시
        assert p["cursor_after_rerun_is_second_candidate"] is True, p
        assert p["lock_held"] is True and p["locked_run"] == [200, "ALREADY_RUNNING"], p
        assert p["lookups_while_locked"] == 0, p  # 패자는 외부 조회가 없다
