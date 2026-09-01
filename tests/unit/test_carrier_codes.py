"""
tests/unit/test_carrier_codes.py
---------------------------------------
택배사 코드 정규화 - 공식 문서로 확인된 조합만 통과, 나머지는 추측 없이 거부.
"""

import pytest

from integrations.malls.carrier_codes import UnknownCarrierError, normalize_carrier_code, supported_carriers


class TestNormalizeCarrierCode:
    def test_confirmed_combination_resolves(self):
        assert normalize_carrier_code("CJ_LOGISTICS", "naver_smartstore") == "CJGLS"
        assert normalize_carrier_code("CJ_LOGISTICS", "coupang") == "CJGLS"

    def test_unknown_carrier_is_rejected_not_guessed(self):
        with pytest.raises(UnknownCarrierError):
            normalize_carrier_code("SOME_RANDOM_CARRIER", "naver_smartstore")

    def test_confirmed_carrier_on_unconfirmed_platform_is_rejected(self):
        with pytest.raises(UnknownCarrierError):
            normalize_carrier_code("CJ_LOGISTICS", "esm")

    def test_supported_carriers_lists_only_confirmed_ones(self):
        assert "CJ_LOGISTICS" in supported_carriers("naver_smartstore")
        assert supported_carriers("esm") == []
