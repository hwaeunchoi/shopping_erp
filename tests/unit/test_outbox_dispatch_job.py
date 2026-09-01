"""
tests/unit/test_outbox_dispatch_job.py
------------------------------------------
outbox_dispatch_job의 기본 차단(OFF)만 검증한다 - 이 잡의 나머지 로직(claim/재시도/
UNKNOWN 분류)은 services.shipment_dispatch_service를 통해 이미 검증했고, 이 잡
자체는 core.database.session_scope()(실제 엔진에 바인딩)를 쓰므로 다른 스케줄러
잡들과 마찬가지로 테스트 DB로 격리한 단위 테스트를 만들지 않는다(tests/unit/
test_report_generate_job.py 모듈 docstring 참고).

기능이 기본 OFF임을 확인하는 것은 "실전송 기본 차단" 요구사항의 핵심이므로,
session_scope() 호출조차 일어나지 않는지(=DB 접속도, 외부 HTTP 요청도 0건)를
직접 검증한다.
"""

from config.settings import settings
from scheduler.jobs import outbox_dispatch_job


class TestDisabledByDefault:
    def test_default_is_disabled(self):
        assert settings.shipment_channel_submit_enabled is False

    def test_run_returns_immediately_without_opening_a_session(self, monkeypatch):
        """기능이 꺼져 있으면(기본값) session_scope()조차 호출하지 않는다 - stale RUNNING
        회수도, due 명령 조회도, 커넥터 생성도, 그로 인한 외부 HTTP 요청도 없다는 뜻이다."""
        monkeypatch.setattr(settings, "shipment_channel_submit_enabled", False)

        def _fail_if_called(*args, **kwargs):
            raise AssertionError("기능이 OFF인데 session_scope()가 호출되었습니다(DB/외부 호출 발생).")

        monkeypatch.setattr(outbox_dispatch_job, "session_scope", _fail_if_called)

        result = outbox_dispatch_job.run()

        assert result == {"skipped_disabled": 1}
