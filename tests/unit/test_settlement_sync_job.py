"""
tests/unit/test_settlement_sync_job.py
------------------------------------------
settlement_sync_job의 기본 차단(OFF)만 검증한다(테스트 방침은
tests/unit/test_outbox_dispatch_job.py 모듈 docstring과 동일).

상용 ERP 확장(6단계): _combine_statuses()는 session_scope()를 쓰지 않는 순수
함수라 이 정책과 무관하게 직접 단위테스트한다.
"""

from config.settings import settings
from scheduler.jobs import settlement_sync_job


class TestDisabledByDefault:
    def test_default_is_disabled(self):
        assert settings.claims_settlement_sync_enabled is False

    def test_run_returns_immediately_without_opening_a_session(self, monkeypatch):
        monkeypatch.setattr(settings, "claims_settlement_sync_enabled", False)

        def _fail_if_called(*args, **kwargs):
            raise AssertionError("기능이 OFF인데 session_scope()가 호출되었습니다(DB/외부 호출 발생).")

        monkeypatch.setattr(settlement_sync_job, "session_scope", _fail_if_called)

        result = settlement_sync_job.run()

        assert result == {"skipped_disabled": {"skipped": "disabled"}}


class TestCombineStatuses:
    def test_all_success_is_success(self):
        assert settlement_sync_job._combine_statuses(["SUCCESS", "SUCCESS"]) == "SUCCESS"

    def test_mixed_success_and_failed_is_partial(self):
        assert settlement_sync_job._combine_statuses(["SUCCESS", "FAILED"]) == "PARTIAL"

    def test_all_failed_is_failed(self):
        assert settlement_sync_job._combine_statuses(["FAILED", "FAILED"]) == "FAILED"

    def test_all_unsupported_is_unsupported(self):
        assert settlement_sync_job._combine_statuses(["UNSUPPORTED", "UNSUPPORTED"]) == "UNSUPPORTED"

    def test_unsupported_mixed_with_success_is_success(self):
        """하나라도 실제로 조회를 지원해 성공했으면 미지원이 아니다."""
        assert settlement_sync_job._combine_statuses(["SUCCESS", "UNSUPPORTED"]) == "SUCCESS"
