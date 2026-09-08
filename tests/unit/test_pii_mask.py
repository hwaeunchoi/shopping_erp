"""
tests/unit/test_pii_mask.py
------------------------------
services.pii_mask - CS 케이스 화면용 이름/전화번호 마스킹 유틸 검증.
"""

from services.pii_mask import mask_name, mask_phone


class TestMaskName:
    def test_reveals_only_first_char(self):
        assert mask_name("홍길동") == "홍**"

    def test_single_char_name(self):
        assert mask_name("김") == "*"

    def test_empty_string_returned_as_is(self):
        assert mask_name("") == ""


class TestMaskPhone:
    def test_reveals_only_last_four_digits(self):
        assert mask_phone("010-9876-5432") == "***-****-5432"

    def test_no_separators(self):
        assert mask_phone("01098765432") == "*******5432"

    def test_short_number_fully_masked(self):
        assert mask_phone("123") == "***"

    def test_empty_string_returned_as_is(self):
        assert mask_phone("") == ""

    def test_exactly_four_digits_fully_masked(self):
        """숫자가 4자리뿐이면 "마지막 4자리"가 전체와 같으므로 전부 마스킹한다
        (노출할 게 하나도 없다는 뜻 - 그대로 노출하면 마스킹의 의미가 없다)."""
        assert mask_phone("1234") == "****"
