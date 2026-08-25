"""
tests/unit/test_init_db.py
--------------------------------
scripts.init_db.seed_platforms() 단위 테스트.

신규 설치(플랫폼 테이블이 완전히 비어 있을 때)에서만 DEFAULT_PLATFORMS를
시딩하고, 행이 하나라도 이미 있으면 아무것도 추가·수정하지 않는 all-or-nothing
가드 계약을 고정한다. session_scope()는 CLI 진입점(main())에서만 열리므로,
여기서는 tests/conftest.py의 인메모리 SQLite db_session을 그대로 주입해
테스트한다(seed_platforms 자체는 세션을 커밋하지 않는다 - 커밋 책임은 호출자).
"""

from models.platform import Platform
from scripts.init_db import DEFAULT_PLATFORMS, seed_platforms


class TestSeedPlatformsFreshInstall:
    def test_creates_five_platforms_with_no_duplicate_codes(self, db_session):
        seed_platforms(db_session)
        db_session.flush()

        platforms = db_session.query(Platform).all()
        assert len(platforms) == 5
        codes = [p.code for p in platforms]
        assert len(codes) == len(set(codes))  # 중복 없음

    def test_naver_and_coupang_are_active(self, db_session):
        seed_platforms(db_session)
        db_session.flush()

        by_code = {p.code: p for p in db_session.query(Platform).all()}
        assert by_code["naver_smartstore"].is_active is True
        assert by_code["coupang"].is_active is True

    def test_esm_elevenst_kakao_are_inactive(self, db_session):
        seed_platforms(db_session)
        db_session.flush()

        by_code = {p.code: p for p in db_session.query(Platform).all()}
        assert by_code["esm"].is_active is False
        assert by_code["elevenst"].is_active is False
        assert by_code["kakao_shopping"].is_active is False

    def test_other_fields_match_default_platforms_definition(self, db_session):
        seed_platforms(db_session)
        db_session.flush()

        by_code = {p.code: p for p in db_session.query(Platform).all()}
        for code, name, connector_class, cycle_days, _is_active in DEFAULT_PLATFORMS:
            platform = by_code[code]
            assert platform.name == name
            assert platform.connector_class == connector_class
            assert platform.settlement_cycle_days == cycle_days


class TestSeedPlatformsIdempotency:
    def test_running_twice_does_not_duplicate(self, db_session):
        seed_platforms(db_session)
        db_session.flush()
        seed_platforms(db_session)
        db_session.flush()

        assert db_session.query(Platform).count() == 5

    def test_user_changed_active_state_is_preserved_on_rerun(self, db_session):
        """첫 실행 후 사용자가 네이버를 끄고 미지원 채널(ESM)을 켜도, 재실행이
        그 값을 원래 기본값으로 되돌리지 않아야 한다."""
        seed_platforms(db_session)
        db_session.flush()

        naver = db_session.query(Platform).filter_by(code="naver_smartstore").one()
        naver.is_active = False
        esm = db_session.query(Platform).filter_by(code="esm").one()
        esm.is_active = True
        db_session.flush()

        seed_platforms(db_session)
        db_session.flush()

        naver_after = db_session.query(Platform).filter_by(code="naver_smartstore").one()
        esm_after = db_session.query(Platform).filter_by(code="esm").one()
        assert naver_after.is_active is False
        assert esm_after.is_active is True
        assert db_session.query(Platform).count() == 5


class TestSeedPlatformsExistingData:
    def test_does_not_add_remaining_platforms_when_one_row_exists(self, db_session):
        db_session.add(
            Platform(
                code="only_one",
                name="이미 있던 플랫폼",
                connector_class="SomeConnector",
                settlement_cycle_days=None,
                is_active=True,
            )
        )
        db_session.flush()

        seed_platforms(db_session)
        db_session.flush()

        platforms = db_session.query(Platform).all()
        assert len(platforms) == 1  # DEFAULT_PLATFORMS 5개가 추가되지 않는다
        assert platforms[0].code == "only_one"

    def test_does_not_modify_existing_row_values(self, db_session):
        db_session.add(
            Platform(
                code="naver_smartstore",
                name="커스텀 이름으로 이미 등록됨",
                connector_class="NaverSmartstoreConnector",
                settlement_cycle_days=99,
                is_active=False,
            )
        )
        db_session.flush()

        seed_platforms(db_session)
        db_session.flush()

        naver = db_session.query(Platform).filter_by(code="naver_smartstore").one()
        assert naver.name == "커스텀 이름으로 이미 등록됨"
        assert naver.settlement_cycle_days == 99
        assert naver.is_active is False


class TestSeedPlatformsTransactionBoundary:
    def test_does_not_commit_itself(self, db_session):
        """seed_platforms()는 add()만 하고 커밋하지 않는다 - rollback하면
        아무것도 남지 않아야 한다(커밋 책임은 호출자인 main()에 있다)."""
        seed_platforms(db_session)
        db_session.flush()
        assert db_session.query(Platform).count() == 5

        db_session.rollback()

        assert db_session.query(Platform).count() == 0
