"""
tests/unit/test_base_mall_connector.py
-----------------------------------------
BaseMallConnector의 클레임 capability 계약을 검증한다.

- 세 capability 기본값은 모두 False.
- 기본 fetch_cancellations/returns/exchanges 는 []가 아니라
  MarketplaceCapabilityUnsupportedError를 던진다(defense-in-depth: 서비스는 capability
  False면 호출하지 않지만, 직접 호출 시 base가 2차 방어).
"""

from datetime import date
from typing import Any

import pytest

from integrations.malls.base_mall_connector import BaseMallConnector
from integrations.malls.errors import MarketplaceCapabilityUnsupportedError


class _MinimalConnector(BaseMallConnector):
    """추상 메서드만 구현한 최소 커넥터(클레임은 base 기본 동작 그대로 사용)."""

    platform_code = "minimal"

    def fetch_orders(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        return []

    def fetch_order_detail(self, platform_order_no: str) -> dict[str, Any]:
        return {}

    def update_shipment(self, platform_order_no: str, carrier: str, tracking_no: str) -> bool:
        return False

    def fetch_settlements(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        return []


class TestClaimCapabilityDefaults:
    def test_three_capabilities_default_false(self):
        c = _MinimalConnector()
        assert c.supports_cancellation_sync is False
        assert c.supports_return_sync is False
        assert c.supports_exchange_sync is False

    def test_class_level_defaults_false(self):
        assert BaseMallConnector.supports_cancellation_sync is False
        assert BaseMallConnector.supports_return_sync is False
        assert BaseMallConnector.supports_exchange_sync is False


class TestBaseClaimMethodsFailClosed:
    def test_fetch_cancellations_raises_unsupported(self):
        with pytest.raises(MarketplaceCapabilityUnsupportedError) as ei:
            _MinimalConnector().fetch_cancellations(date(2026, 1, 1), date(2026, 1, 2))
        assert ei.value.marketplace_code == "minimal"

    def test_fetch_returns_raises_unsupported(self):
        with pytest.raises(MarketplaceCapabilityUnsupportedError):
            _MinimalConnector().fetch_returns(date(2026, 1, 1), date(2026, 1, 2))

    def test_fetch_exchanges_raises_unsupported(self):
        with pytest.raises(MarketplaceCapabilityUnsupportedError):
            _MinimalConnector().fetch_exchanges(date(2026, 1, 1), date(2026, 1, 2))


class TestRealConnectorsClaimCapabilitiesMatchConfirmedContract:
    """상용 ERP 확장(2단계): 공식 문서로 확인된 API만 capability=True로 켠다.

    - Naver: 클레임 일괄조회 API가 확인되지 않아 전부 False 유지.
    - Coupang: returnRequests(v6)/exchangeRequests(v4)는 확인됨 -> True.
      cancellations는 cancelType=CANCEL 조회 시 orderId가 필수가 되어(status
      파라미터를 쓸 수 없음) 날짜range 일괄조회가 불가능함을 확인 -> False 유지
      (integrations/malls/coupang_connector.py 상단 주석의 2026-09 조회 근거 참고).
    """

    def test_naver_claim_capabilities_all_false(self):
        from integrations.malls.naver_smartstore_connector import NaverSmartstoreConnector

        assert NaverSmartstoreConnector.supports_cancellation_sync is False
        assert NaverSmartstoreConnector.supports_return_sync is False
        assert NaverSmartstoreConnector.supports_exchange_sync is False

    def test_coupang_claim_capabilities_per_confirmed_contract(self):
        from integrations.malls.coupang_connector import CoupangConnector

        assert CoupangConnector.supports_cancellation_sync is False
        assert CoupangConnector.supports_return_sync is True
        assert CoupangConnector.supports_exchange_sync is True
