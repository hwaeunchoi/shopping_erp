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

from datetime import date
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
    """상용 ERP 확장 5단계 B묶음 보완: _record_integration_status()가 이제 status
    문자열 하나만이 아니라 sync_all_inquiries()의 반환값 전체(dict)를 받아, by_source의
    reason_code(PAGE_LIMIT_EXCEEDED 등 안전한 코드)를 메시지에 반영하는지도 검증한다."""

    def test_success_calls_upsert_success(self):
        repo = MagicMock()
        cs_inquiry_sync_job._record_integration_status(repo, "coupang", {"status": "SUCCESS", "by_source": {}})
        repo.upsert_success.assert_called_once_with("CS_INQUIRY", "coupang")

    def test_partial_success_calls_upsert_partial(self):
        repo = MagicMock()
        cs_inquiry_sync_job._record_integration_status(repo, "coupang", {"status": "PARTIAL_SUCCESS", "by_source": {}})
        repo.upsert_partial.assert_called_once()
        assert repo.upsert_partial.call_args[0][0] == "CS_INQUIRY"

    def test_failed_calls_upsert_error(self):
        repo = MagicMock()
        cs_inquiry_sync_job._record_integration_status(repo, "coupang", {"status": "FAILED", "by_source": {}})
        repo.upsert_error.assert_called_once()

    def test_unsupported_records_nothing(self):
        repo = MagicMock()
        cs_inquiry_sync_job._record_integration_status(repo, "coupang", {"status": "UNSUPPORTED", "by_source": {}})
        repo.upsert_success.assert_not_called()
        repo.upsert_partial.assert_not_called()
        repo.upsert_error.assert_not_called()

    def test_partial_success_message_includes_safe_reason_code(self):
        """한 소스(상품별 문의)가 PAGE_LIMIT_EXCEEDED로 실패해도 integration_status의
        메시지에서 그 사실을 바로 확인할 수 있어야 한다(요구사항: 기존 운영 관찰
        경로에서 확인 가능하게 할 것) - 문의 본문/주문번호/ID는 당연히 담기지 않는다."""
        repo = MagicMock()
        result = {
            "status": "PARTIAL_SUCCESS",
            "by_source": {
                "COUPANG_CALL_CENTER": {"status": "SUCCESS", "created": 1, "updated": 0, "failed": 0},
                "COUPANG_PRODUCT_INQUIRY": {
                    "status": "FAILED",
                    "created": 0,
                    "updated": 0,
                    "failed": 0,
                    "reason_code": "PAGE_LIMIT_EXCEEDED",
                },
            },
        }
        cs_inquiry_sync_job._record_integration_status(repo, "coupang", result)
        message = repo.upsert_partial.call_args[0][2]
        assert "PAGE_LIMIT_EXCEEDED" in message

    def test_failed_message_has_no_reason_code_when_absent(self):
        """reason_code가 없는 일반적인 FAILED(예: 커넥터 레벨 전체 실패)는 기존
        메시지 그대로 유지된다 - 괄호 안 reason= 접미사를 억지로 붙이지 않는다."""
        repo = MagicMock()
        cs_inquiry_sync_job._record_integration_status(
            repo, "coupang", {"status": "FAILED", "by_source": {"COUPANG_CALL_CENTER": {"status": "FAILED"}}}
        )
        message = repo.upsert_error.call_args[0][2]
        assert "reason=" not in message


class TestCollectionWindow:
    """상용 ERP 확장 5단계 B묶음 보완 - settings.cs_inquiry_sync_window_days가 실제
    조회 기간 계산에 반영되는지 session_scope() 없이 검증한다(이 파일의 테스트
    방침과 동일하게 순수 함수만 직접 호출)."""

    def test_window_days_one_means_today_only(self, monkeypatch):
        monkeypatch.setattr(settings, "cs_inquiry_sync_window_days", 1)
        start, end = cs_inquiry_sync_job._collection_window(date(2026, 9, 30))
        assert start == end == date(2026, 9, 30)

    def test_window_days_seven_spans_seven_calendar_days_inclusive(self, monkeypatch):
        monkeypatch.setattr(settings, "cs_inquiry_sync_window_days", 7)
        start, end = cs_inquiry_sync_job._collection_window(date(2026, 9, 30))
        assert end == date(2026, 9, 30)
        assert start == date(2026, 9, 24)
        assert (end - start).days == 6  # 7일(both ends inclusive).
