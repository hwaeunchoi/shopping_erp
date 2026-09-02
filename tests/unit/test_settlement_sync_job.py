"""
tests/unit/test_settlement_sync_job.py
------------------------------------------
settlement_sync_job의 기본 차단(OFF)만 검증한다(테스트 방침은
tests/unit/test_outbox_dispatch_job.py 모듈 docstring과 동일).
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
