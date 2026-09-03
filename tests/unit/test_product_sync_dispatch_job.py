"""
tests/unit/test_product_sync_dispatch_job.py
------------------------------------------------------
product_sync_dispatch_job의 기본 차단(OFF)만 검증한다 - 이 잡의 나머지 로직
(claim/재시도/UNKNOWN 분류/버전검증)은 services.product_sync_dispatch_service를
통해 이미 검증했고, 이 잡 자체는 core.database.session_scope()(실제 엔진에
바인딩)를 쓰므로 다른 스케줄러 잡들과 마찬가지로 테스트 DB로 격리한 단위
테스트를 만들지 않는다(tests/unit/test_outbox_dispatch_job.py 모듈 docstring과
동일한 테스트 방침).
"""

from config.settings import settings
from scheduler.jobs import product_sync_dispatch_job
from services.product_sync_dispatch_service import INVENTORY_UPDATE, PRODUCT_INFO_UPDATE, SALE_STATUS_UPDATE


class TestDisabledByDefault:
    def test_default_is_disabled(self):
        assert settings.product_channel_sync_enabled is False
        assert settings.product_info_update_enabled is False
        assert settings.product_publish_enabled is False

    def test_run_returns_immediately_without_opening_a_session(self, monkeypatch):
        """세 플래그가 모두 꺼져 있으면(기본값) session_scope()조차 호출하지 않는다 -
        stale RUNNING 회수도, due 명령 조회도, 커넥터 생성도, 그로 인한 외부 HTTP
        요청도 없다는 뜻이다."""
        monkeypatch.setattr(settings, "product_channel_sync_enabled", False)
        monkeypatch.setattr(settings, "product_info_update_enabled", False)

        def _fail_if_called(*args, **kwargs):
            raise AssertionError("기능이 OFF인데 session_scope()가 호출되었습니다(DB/외부 호출 발생).")

        monkeypatch.setattr(product_sync_dispatch_job, "session_scope", _fail_if_called)

        result = product_sync_dispatch_job.run()

        assert result == {"skipped_disabled": 1}


class TestEnabledCommandTypesAreIndependent:
    """product_channel_sync_enabled(재고/판매상태)와 product_info_update_enabled
    (정보수정)는 서로 완전히 독립된 플래그다 - 하나만 켜져 있으면 그 플래그가
    통제하는 command_type만 대상에 포함되어야 한다. product_publish_enabled(신규
    등록 전용, 이 잡이 다루지 않는 PRODUCT_CREATE만 통제)를 켜는 것만으로는 이 잡의
    어떤 command_type도 활성화되지 않아야 한다(감사 지적: "신규 등록 플래그와도
    독립적으로 검증"). 순수 함수라 DB 없이 검증한다."""

    def test_all_three_disabled_returns_empty(self, monkeypatch):
        monkeypatch.setattr(settings, "product_channel_sync_enabled", False)
        monkeypatch.setattr(settings, "product_info_update_enabled", False)
        monkeypatch.setattr(settings, "product_publish_enabled", False)
        assert product_sync_dispatch_job._enabled_command_types() == ()

    def test_only_channel_sync_enabled_excludes_info_update(self, monkeypatch):
        monkeypatch.setattr(settings, "product_channel_sync_enabled", True)
        monkeypatch.setattr(settings, "product_info_update_enabled", False)
        types = product_sync_dispatch_job._enabled_command_types()
        assert set(types) == {INVENTORY_UPDATE, SALE_STATUS_UPDATE}

    def test_only_info_update_enabled_includes_only_info_update(self, monkeypatch):
        monkeypatch.setattr(settings, "product_channel_sync_enabled", False)
        monkeypatch.setattr(settings, "product_info_update_enabled", True)
        assert product_sync_dispatch_job._enabled_command_types() == (PRODUCT_INFO_UPDATE,)

    def test_publish_flag_alone_enables_nothing_in_this_job(self, monkeypatch):
        """신규 등록 플래그(product_publish_enabled)는 이 잡이 아니라
        product_publish_dispatch_job(PRODUCT_CREATE 전용)이 본다 - 이 잡 쪽
        command_type은 하나도 활성화되면 안 된다."""
        monkeypatch.setattr(settings, "product_channel_sync_enabled", False)
        monkeypatch.setattr(settings, "product_info_update_enabled", False)
        monkeypatch.setattr(settings, "product_publish_enabled", True)
        assert product_sync_dispatch_job._enabled_command_types() == ()
