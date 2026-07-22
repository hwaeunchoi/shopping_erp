"""
tests/unit/test_platform_fee_rule_repository.py
-----------------------------------------------------
PlatformFeeRuleRepository.get_effective_rule() 단위 테스트.

ProfitCalculationService가 수수료율을 platform_fee_rules 테이블에서
조회하도록 바뀐 것(하드코딩 제거)에 대한 선택 규칙 검증.
"""

from datetime import date

from models.platform import PlatformFeeRule
from repositories.platform_repository import PlatformFeeRuleRepository


def _make_rule(db_session, platform, fee_rate, effective_from, effective_to=None):
    rule = PlatformFeeRule(
        platform_id=platform.id, fee_rate=fee_rate, effective_from=effective_from, effective_to=effective_to
    )
    db_session.add(rule)
    db_session.flush()
    return rule


class TestGetEffectiveRule:
    def test_returns_rule_when_platform_fee_exists(self, db_session, platform):
        _make_rule(db_session, platform, fee_rate=10.0, effective_from=date(2020, 1, 1))
        repo = PlatformFeeRuleRepository(db_session)

        rule = repo.get_effective_rule(platform.id, date(2026, 1, 1))

        assert rule is not None
        assert float(rule.fee_rate) == 10.0

    def test_selects_rule_matching_target_date_among_different_periods(self, db_session, platform):
        old_rule = _make_rule(
            db_session, platform, fee_rate=8.0, effective_from=date(2020, 1, 1), effective_to=date(2024, 12, 31)
        )
        new_rule = _make_rule(db_session, platform, fee_rate=10.0, effective_from=date(2025, 1, 1))
        repo = PlatformFeeRuleRepository(db_session)

        rule_for_2023 = repo.get_effective_rule(platform.id, date(2023, 6, 1))
        rule_for_2026 = repo.get_effective_rule(platform.id, date(2026, 1, 1))

        assert rule_for_2023 is not None and rule_for_2023.id == old_rule.id
        assert rule_for_2026 is not None and rule_for_2026.id == new_rule.id

    def test_returns_none_when_no_rule_exists(self, db_session, platform):
        repo = PlatformFeeRuleRepository(db_session)

        rule = repo.get_effective_rule(platform.id, date(2026, 1, 1))

        assert rule is None

    def test_excludes_rule_outside_effective_period(self, db_session, platform):
        """조회일이 effective_from~effective_to 범위 밖이면(만료됨) 제외한다."""
        _make_rule(db_session, platform, fee_rate=8.0, effective_from=date(2020, 1, 1), effective_to=date(2020, 12, 31))
        repo = PlatformFeeRuleRepository(db_session)

        rule = repo.get_effective_rule(platform.id, date(2026, 1, 1))

        assert rule is None

    def test_prefers_latest_effective_from_when_multiple_match(self, db_session, platform):
        _make_rule(db_session, platform, fee_rate=8.0, effective_from=date(2020, 1, 1))
        newer_rule = _make_rule(db_session, platform, fee_rate=10.0, effective_from=date(2023, 1, 1))
        repo = PlatformFeeRuleRepository(db_session)

        rule = repo.get_effective_rule(platform.id, date(2026, 1, 1))

        assert rule is not None and rule.id == newer_rule.id
