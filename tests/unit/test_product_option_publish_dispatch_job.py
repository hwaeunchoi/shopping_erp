"""
tests/unit/test_product_option_publish_dispatch_job.py
------------------------------------------------------
product_option_publish_dispatch_job의 기본 차단(OFF)만 검증한다 - 이 잡의 나머지
로직(claim/재시도/UNKNOWN 분류/중복접수)은 services.product_option_publish_service를
통해 이미 검증했다(tests/unit/test_product_publish_dispatch_job.py 모듈 docstring과
동일한 테스트 방침).
"""

from config.settings import settings
from scheduler.jobs import product_option_publish_dispatch_job


class TestDisabledByDefault:
    def test_default_is_disabled(self):
        assert settings.product_option_publish_enabled is False

    def test_run_returns_immediately_without_opening_a_session(self, monkeypatch):
        """기능이 꺼져 있으면(기본값) session_scope()조차 호출하지 않는다 - stale
        RUNNING 회수도, due 명령 조회도, 커넥터 생성도, 그로 인한 외부 HTTP 요청도
        없다는 뜻이다."""
        monkeypatch.setattr(settings, "product_option_publish_enabled", False)

        def _fail_if_called(*args, **kwargs):
            raise AssertionError("기능이 OFF인데 session_scope()가 호출되었습니다(DB/외부 호출 발생).")

        monkeypatch.setattr(product_option_publish_dispatch_job, "session_scope", _fail_if_called)

        result = product_option_publish_dispatch_job.run()

        assert result == {"skipped_disabled": 1}
