"""
tests/unit/test_cs_inquiry_sync_job.py
------------------------------------------
cs_inquiry_sync_job의 기본 차단(OFF)만 검증한다(테스트 방침은
tests/unit/test_outbox_dispatch_job.py 모듈 docstring과 동일: session_scope()를
쓰는 스케줄러 잡은 테스트 DB로 격리한 단위 테스트를 만들지 않는다). 나머지 동기화
로직은 services.cs_channel_sync_service.CsChannelSyncService의 기존 단위테스트가
이미 검증했다.

상용 ERP 확장(6단계): _record_integration_status()는 session_scope()를 쓰지 않는
순수 함수라 이 정책과 무관하게 직접 단위테스트한다.
"""

from unittest.mock import MagicMock

from config.settings import settings
from scheduler.jobs import cs_inquiry_sync_job


class TestDisabledByDefault:
    def test_default_is_disabled(self):
        assert settings.cs_inquiry_sync_enabled is False

    def test_run_returns_immediately_without_opening_a_session(self, monkeypatch):
        """기능이 꺼져 있으면(기본값) session_scope()조차 호출하지 않는다 - 커넥터
        생성/문의 조회(외부 HTTP 요청)도 전혀 발생하지 않는다는 뜻이다."""
        monkeypatch.setattr(settings, "cs_inquiry_sync_enabled", False)

        def _fail_if_called(*args, **kwargs):
            raise AssertionError("기능이 OFF인데 session_scope()가 호출되었습니다(DB/외부 호출 발생).")

        monkeypatch.setattr(cs_inquiry_sync_job, "session_scope", _fail_if_called)

        result = cs_inquiry_sync_job.run()

        assert result == {"skipped_disabled": {"skipped": "disabled"}}


class TestRecordIntegrationStatus:
    def test_success_calls_upsert_success(self):
        repo = MagicMock()
        cs_inquiry_sync_job._record_integration_status(repo, "coupang", "SUCCESS")
        repo.upsert_success.assert_called_once_with("CS_INQUIRY", "coupang")

    def test_partial_success_calls_upsert_partial(self):
        repo = MagicMock()
        cs_inquiry_sync_job._record_integration_status(repo, "coupang", "PARTIAL_SUCCESS")
        repo.upsert_partial.assert_called_once()
        assert repo.upsert_partial.call_args[0][0] == "CS_INQUIRY"

    def test_failed_calls_upsert_error(self):
        repo = MagicMock()
        cs_inquiry_sync_job._record_integration_status(repo, "coupang", "FAILED")
        repo.upsert_error.assert_called_once()

    def test_unsupported_records_nothing(self):
        repo = MagicMock()
        cs_inquiry_sync_job._record_integration_status(repo, "coupang", "UNSUPPORTED")
        repo.upsert_success.assert_not_called()
        repo.upsert_partial.assert_not_called()
        repo.upsert_error.assert_not_called()
