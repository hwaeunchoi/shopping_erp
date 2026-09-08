"""
tests/unit/test_fulfillment_carrier.py
------------------------------------------
services.fulfillment_carrier - 내부 택배사 코드 검증 + 송장번호 정규화 검증.
"""

import pytest

from services.fulfillment_carrier import (
    INTERNAL_CARRIER_CODES,
    InvalidCarrierError,
    InvalidTrackingNoError,
    normalize_tracking_no,
    validate_carrier,
)


class TestValidateCarrier:
    def test_known_code_passes(self):
        assert validate_carrier("CJ_LOGISTICS") == "CJ_LOGISTICS"

    def test_all_declared_codes_are_valid(self):
        for code in INTERNAL_CARRIER_CODES:
            assert validate_carrier(code) == code

    def test_unknown_code_rejected(self):
        with pytest.raises(InvalidCarrierError):
            validate_carrier("DHL")

    def test_empty_code_rejected(self):
        with pytest.raises(InvalidCarrierError):
            validate_carrier("")


class TestNormalizeTrackingNo:
    def test_leading_zero_preserved(self):
        assert normalize_tracking_no("0012345678") == "0012345678"

    def test_hyphens_and_spaces_removed(self):
        assert normalize_tracking_no("123-456-789") == "123456789"
        assert normalize_tracking_no("123 456 789") == "123456789"
        assert normalize_tracking_no("  123-456 789  ") == "123456789"

    def test_hyphenated_and_plain_forms_are_equivalent(self):
        assert normalize_tracking_no("123-456-789") == normalize_tracking_no("123456789")

    def test_empty_after_normalization_rejected(self):
        with pytest.raises(InvalidTrackingNoError):
            normalize_tracking_no("   ")
        with pytest.raises(InvalidTrackingNoError):
            normalize_tracking_no("---")

    def test_control_characters_rejected(self):
        with pytest.raises(InvalidTrackingNoError):
            normalize_tracking_no("123\n456")
        with pytest.raises(InvalidTrackingNoError):
            normalize_tracking_no("123\t456")

    def test_too_long_rejected(self):
        with pytest.raises(InvalidTrackingNoError):
            normalize_tracking_no("1" * 51)

    def test_max_length_accepted(self):
        assert normalize_tracking_no("1" * 50) == "1" * 50
