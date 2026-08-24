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


class TestRealConnectorsClaimsDisabled:
    def test_naver_and_coupang_claim_capabilities_all_false(self):
        from integrations.malls.coupang_connector import CoupangConnector
        from integrations.malls.naver_smartstore_connector import NaverSmartstoreConnector

        for cls in (NaverSmartstoreConnector, CoupangConnector):
            assert cls.supports_cancellation_sync is False
            assert cls.supports_return_sync is False
            assert cls.supports_exchange_sync is False
