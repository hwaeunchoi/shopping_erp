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
