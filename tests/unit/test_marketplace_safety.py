"""
tests/unit/test_marketplace_safety.py
----------------------------------------
운영 수집 경로에 더미 데이터가 유입되지 않도록 하는 안전장치를 검증한다.

- 팩토리: 미검증 채널(ESM/카카오/11번가)·미등록 커넥터는 미지원 오류.
- 커넥터: 미구현 기능(주문상세/정산/쿠팡 상품)은 [] 위장이 아니라 미지원 오류.
- 실 API 성공 + 0건은 정상 []( 실패와 구분).
- 스케줄러(주문/상품/정산): 한 채널 실패가 다른 채널을 막지 않고 채널별로 격리된다.

실제 쇼핑몰 API는 호출하지 않는다(httpx.MockTransport / 스텁 / monkeypatch만 사용).
"""

from contextlib import contextmanager
from datetime import date

import httpx
import pytest

import scheduler.jobs.order_collect_job as order_job
import scheduler.jobs.product_sync_job as product_job
import scheduler.jobs.settlement_sync_job as settlement_job
from integrations.malls import SUPPORTED_CONNECTORS, get_mall_connector
from integrations.malls.base_mall_connector import BaseMallConnector
from integrations.malls.coupang_connector import COUPANG_API_BASE, CoupangConnector
from integrations.malls.elevenst_connector import ElevenstConnector
from integrations.malls.errors import MarketplaceCapabilityUnsupportedError, MarketplaceCredentialMissingError
from integrations.malls.esm_connector import EsmConnector
from integrations.malls.kakao_shopping_connector import KakaoShoppingConnector
from integrations.malls.naver_smartstore_connector import NaverSmartstoreConnector


class TestFactoryFailsClosed:
    def test_supported_set_is_naver_and_coupang_only(self):
        assert NaverSmartstoreConnector in SUPPORTED_CONNECTORS
        assert CoupangConnector in SUPPORTED_CONNECTORS
        assert len(SUPPORTED_CONNECTORS) == 2

    @pytest.mark.parametrize("connector_class", ["EsmConnector", "ElevenstConnector", "KakaoShoppingConnector"])
    def test_unverified_channels_blocked(self, connector_class):
        with pytest.raises(MarketplaceCapabilityUnsupportedError):
            get_mall_connector(connector_class)

    def test_unknown_connector_class_blocked(self):
        with pytest.raises(MarketplaceCapabilityUnsupportedError):
            get_mall_connector("TotallyUnknownConnector")

    def test_supported_channels_instantiate(self):
        assert isinstance(get_mall_connector("NaverSmartstoreConnector"), NaverSmartstoreConnector)
        assert isinstance(get_mall_connector("CoupangConnector"), CoupangConnector)


class TestCapabilityUnsupported:
    """미구현 기능은 빈 목록(정상 0건 위장)이 아니라 명시적 미지원 오류를 던진다."""

    def test_naver_order_detail_unsupported(self):
        with pytest.raises(MarketplaceCapabilityUnsupportedError):
            NaverSmartstoreConnector().fetch_order_detail("X")

    def test_naver_settlements_unsupported(self):
        with pytest.raises(MarketplaceCapabilityUnsupportedError):
            NaverSmartstoreConnector().fetch_settlements(date(2026, 1, 1), date(2026, 1, 2))

    def test_coupang_order_detail_unsupported(self):
        with pytest.raises(MarketplaceCapabilityUnsupportedError):
            CoupangConnector().fetch_order_detail("X")

    def test_coupang_settlements_unsupported(self):
        with pytest.raises(MarketplaceCapabilityUnsupportedError):
            CoupangConnector().fetch_settlements(date(2026, 1, 1), date(2026, 1, 2))

    def test_coupang_products_unsupported(self):
        with pytest.raises(MarketplaceCapabilityUnsupportedError):
            CoupangConnector().fetch_products()


class TestShipmentSubmitCapabilityNeverFakesSuccess:
    """상용 ERP 확장(1단계) - submit_shipment()는 supports_shipment_submit=True인
    채널(네이버/쿠팡)만 오버라이드하며, 나머지는 base 기본 구현(미지원 오류)을
    그대로 물려받아 절대 accepted=True를 반환하지 않는다."""

    def test_naver_and_coupang_declare_support(self):
        assert NaverSmartstoreConnector.supports_shipment_submit is True
        assert CoupangConnector.supports_shipment_submit is True

    @pytest.mark.parametrize("connector_cls", [EsmConnector, ElevenstConnector, KakaoShoppingConnector])
    def test_unverified_channels_default_to_unsupported_not_true(self, connector_cls):
        assert connector_cls.supports_shipment_submit is False
        with pytest.raises(MarketplaceCapabilityUnsupportedError):
            connector_cls().submit_shipment("PO-1", "CJGLS", "TRACK-1", date(2026, 1, 2))


class TestEmptyRealResultIsSuccess:
    def test_coupang_empty_orders_returns_empty_list_not_error(self, db_session, platform):
        from services.settings_service import ApiCredentialService

        svc = ApiCredentialService(db_session)
        svc.upsert_credential("PLATFORM", platform.id, "access_key", "ak")
        svc.upsert_credential("PLATFORM", platform.id, "secret_key", "sk")
        svc.upsert_credential("PLATFORM", platform.id, "vendor_id", "A00012345")
        db_session.flush()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [], "nextToken": ""})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)
        # 실 API 성공 + 주문 0건 -> 정상 성공([]), 예외 아님.
        assert connector.fetch_orders(date(2026, 1, 1), date(2026, 1, 2)) == []


# --- 스케줄러 채널 격리 ---------------------------------------------------------


class _StubConnector(BaseMallConnector):
    """지정한 결과를 반환하거나 지정한 예외를 던지는 테스트용 스텁(실 네트워크 없음)."""

    def __init__(self, *, orders=None, error=None, products=None, settlements=None):
        self._orders = orders or []
        self._error = error
        self._products = products or []
        self._settlements = settlements or []

    def fetch_orders(self, start_date, end_date):
        if self._error:
            raise self._error
        return self._orders

    def fetch_order_detail(self, platform_order_no):
        raise NotImplementedError

    def update_shipment(self, platform_order_no, carrier, tracking_no):
        return True

    def fetch_settlements(self, start_date, end_date):
        if self._error:
            raise self._error
        return self._settlements

    def fetch_products(self):
        if self._error:
            raise self._error
        return self._products


@contextmanager
def _fake_scope(session):
    yield session


@pytest.fixture
def two_active_platforms(db_session):
    """활성 플랫폼 2개(정상/실패 채널 격리 검증용). 커넥터는 monkeypatch로 주입한다."""
    from models.platform import Platform

    good = Platform(code="good_ch", name="정상채널", connector_class="CoupangConnector", is_active=True)
    bad = Platform(code="bad_ch", name="실패채널", connector_class="NaverSmartstoreConnector", is_active=True)
    db_session.add_all([good, bad])
    db_session.flush()
    return good, bad


def _connector_router(good_id, good_conn, bad_conn):
    def _factory(connector_class, session=None, platform_id=None):
        return good_conn if platform_id == good_id else bad_conn

    return _factory


class TestSchedulerChannelIsolation:
    def test_order_collect_isolates_failing_channel(self, monkeypatch, db_session, warehouse, two_active_platforms):
        good, bad = two_active_platforms
        good_conn = _StubConnector(orders=[])  # 성공(0건)
        bad_conn = _StubConnector(error=MarketplaceCredentialMissingError("naver"))

        monkeypatch.setattr(order_job, "session_scope", lambda: _fake_scope(db_session))
        monkeypatch.setattr(order_job, "get_mall_connector", _connector_router(good.id, good_conn, bad_conn))

        results = order_job.run()

        # 두 채널 모두 처리됨(실패가 루프를 중단하지 않음).
        assert good.code in results and bad.code in results
        assert results[bad.code]["created"] == 0
        assert results[bad.code]["error"] == "CREDENTIAL_MISSING"

    def test_product_sync_isolates_and_skips_unsupported(self, monkeypatch, db_session, two_active_platforms):
        good, bad = two_active_platforms
        good_conn = _StubConnector(products=[])
        bad_conn = _StubConnector(error=MarketplaceCapabilityUnsupportedError("coupang", "products"))

        monkeypatch.setattr(product_job, "session_scope", lambda: _fake_scope(db_session))
        monkeypatch.setattr(product_job, "get_mall_connector", _connector_router(good.id, good_conn, bad_conn))

        results = product_job.run()

        assert good.code in results and bad.code in results
        assert results[bad.code] == {"skipped": "unsupported"}

    def test_settlement_sync_isolates_failing_channel(self, monkeypatch, db_session, two_active_platforms):
        good, bad = two_active_platforms
        good_conn = _StubConnector(settlements=[])
        bad_conn = _StubConnector(error=MarketplaceCapabilityUnsupportedError("naver", "settlement"))

        monkeypatch.setattr(settlement_job, "session_scope", lambda: _fake_scope(db_session))
        monkeypatch.setattr(settlement_job, "get_mall_connector", _connector_router(good.id, good_conn, bad_conn))

        results = settlement_job.run()

        assert good.code in results and bad.code in results
        assert results[bad.code] == {"skipped": "unsupported"}
        assert results[good.code] == {"created": 0, "updated": 0}


class TestSafeLogging:
    def test_coupang_unknown_status_warning_omits_order_id(self, db_session, platform, caplog):
        import logging

        from services.settings_service import ApiCredentialService

        svc = ApiCredentialService(db_session)
        svc.upsert_credential("PLATFORM", platform.id, "access_key", "ak")
        svc.upsert_credential("PLATFORM", platform.id, "secret_key", "sk")
        svc.upsert_credential("PLATFORM", platform.id, "vendor_id", "A00012345")
        db_session.flush()

        def handler(request: httpx.Request) -> httpx.Response:
            if dict(request.url.params).get("status") == "ACCEPT":
                return httpx.Response(
                    200,
                    json={
                        "data": [{"orderId": "SECRET-ORDER-123", "status": "WEIRDSTATUS", "orderItems": []}],
                        "nextToken": "",
                    },
                )
            return httpx.Response(200, json={"data": [], "nextToken": ""})

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)
        with caplog.at_level(logging.WARNING):
            connector.fetch_orders(date(2026, 1, 1), date(2026, 1, 2))

        # 알 수 없는 status 경고에 전체 주문번호가 노출되지 않아야 한다.
        assert "SECRET-ORDER-123" not in caplog.text
        assert "WEIRDSTATUS" in caplog.text  # 상태값 자체는 안전하게 기록
