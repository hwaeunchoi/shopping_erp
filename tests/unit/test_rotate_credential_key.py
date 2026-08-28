"""
tests/unit/test_rotate_credential_key.py
------------------------------------------------
scripts.rotate_credential_key 단위 테스트.

인메모리 SQLite(db_session)로 rotate_credentials()의 로직(전체 복호화 검증 ->
재암호화 -> 재검증 -> commit/rollback 경계)을 검증한다. with_for_update()는
SQLite에서 실제 잠금 효과가 없지만(조용히 무시됨) 오류 없이 통과하므로,
이 테스트들은 "잠금이 동시쓰기를 막는지"가 아니라 로직 자체만 검증한다.
"""

import io
from contextlib import redirect_stdout
from datetime import datetime

import pytest

from core.crypto import InsecureSecretError, build_fernet
from models.system import ApiCredential
from scripts.rotate_credential_key import RotationAborted, main, rotate_credentials

# 테스트 전용 dummy secret - 데모 기본값이 아니고 20자 이상(validate_production_secret 통과).
OLD_SECRET = "old-secret-for-rotation-tests-0000000000"
NEW_SECRET = "new-secret-for-rotation-tests-1111111111"


def _seed_credential(
    db_session, owner_type: str, owner_id: int, key_name: str, plain: str, secret: str
) -> ApiCredential:
    fernet = build_fernet(secret)
    cred = ApiCredential(
        owner_type=owner_type,
        owner_id=owner_id,
        key_name=key_name,
        key_value_encrypted=fernet.encrypt(plain.encode("utf-8")).decode("utf-8"),
        # naive datetime을 쓴다 - SQLite는 tz 정보를 저장하지 않으므로 tz-aware로
        # 넣으면 commit 후 재조회 시 naive로 바뀌어, 같은 시각인데도
        # datetime(...) == datetime(..., tzinfo=utc) 비교가 실패한다.
        updated_at=datetime(2026, 1, 1),
    )
    db_session.add(cred)
    db_session.flush()
    return cred


class TestRotateCredentialsHappyPath:
    def test_dry_run_reports_count_and_changes_nothing(self, db_session):
        c1 = _seed_credential(db_session, "PLATFORM", 1, "client_id", "plain-1", OLD_SECRET)
        c2 = _seed_credential(db_session, "PLATFORM", 2, "access_key", "plain-2", OLD_SECRET)
        before_1, before_2 = c1.key_value_encrypted, c2.key_value_encrypted
        before_updated_1, before_updated_2 = c1.updated_at, c2.updated_at

        result = rotate_credentials(db_session, OLD_SECRET, NEW_SECRET, execute=False)
        db_session.rollback()

        assert result.total == 2
        assert result.executed is False
        assert c1.key_value_encrypted == before_1
        assert c2.key_value_encrypted == before_2
        assert c1.updated_at == before_updated_1
        assert c2.updated_at == before_updated_2

    def test_execute_reencrypts_all_and_preserves_plaintext(self, db_session):
        c1 = _seed_credential(db_session, "PLATFORM", 1, "client_id", "plain-value-1", OLD_SECRET)
        c2 = _seed_credential(db_session, "PLATFORM", 2, "access_key", "plain-value-2", OLD_SECRET)
        c3 = _seed_credential(db_session, "PLATFORM", 2, "secret_key", "plain-value-3", OLD_SECRET)

        result = rotate_credentials(db_session, OLD_SECRET, NEW_SECRET, execute=True)
        db_session.commit()

        assert result.total == 3
        assert result.executed is True
        new_fernet = build_fernet(NEW_SECRET)
        assert new_fernet.decrypt(c1.key_value_encrypted.encode()).decode() == "plain-value-1"
        assert new_fernet.decrypt(c2.key_value_encrypted.encode()).decode() == "plain-value-2"
        assert new_fernet.decrypt(c3.key_value_encrypted.encode()).decode() == "plain-value-3"

    def test_execute_only_changes_ciphertext_column(self, db_session):
        c1 = _seed_credential(db_session, "PLATFORM", 1, "client_id", "plain-value-1", OLD_SECRET)
        owner_type, owner_id, key_name, updated_at = c1.owner_type, c1.owner_id, c1.key_name, c1.updated_at

        rotate_credentials(db_session, OLD_SECRET, NEW_SECRET, execute=True)
        db_session.commit()

        assert c1.owner_type == owner_type
        assert c1.owner_id == owner_id
        assert c1.key_name == key_name
        assert c1.updated_at == updated_at  # 회전은 updated_at을 바꾸지 않는다(사용자 수정 이벤트가 아님)

    def test_zero_credentials_succeeds_with_zero_count(self, db_session):
        result = rotate_credentials(db_session, OLD_SECRET, NEW_SECRET, execute=True)
        db_session.commit()

        assert result.total == 0
        assert result.executed is True

    def test_multiple_platforms_and_key_names(self, db_session):
        seeded = [
            _seed_credential(db_session, "PLATFORM", 1, "client_id", "v1", OLD_SECRET),
            _seed_credential(db_session, "PLATFORM", 1, "client_secret", "v2", OLD_SECRET),
            _seed_credential(db_session, "PLATFORM", 2, "access_key", "v3", OLD_SECRET),
            _seed_credential(db_session, "PLATFORM", 2, "secret_key", "v4", OLD_SECRET),
            _seed_credential(db_session, "PLATFORM", 2, "vendor_id", "v5", OLD_SECRET),
        ]

        result = rotate_credentials(db_session, OLD_SECRET, NEW_SECRET, execute=True)
        db_session.commit()

        assert result.total == 5
        new_fernet = build_fernet(NEW_SECRET)
        for i, cred in enumerate(seeded, start=1):
            assert new_fernet.decrypt(cred.key_value_encrypted.encode()).decode() == f"v{i}"


class TestRotateCredentialsFailureIsolation:
    def test_wrong_old_key_aborts_with_zero_changes(self, db_session):
        c1 = _seed_credential(db_session, "PLATFORM", 1, "client_id", "plain-1", OLD_SECRET)
        before = c1.key_value_encrypted

        wrong_old_secret = "totally-wrong-old-secret-9999999999"
        with pytest.raises(RotationAborted):
            rotate_credentials(db_session, wrong_old_secret, NEW_SECRET, execute=True)
        db_session.rollback()

        assert c1.key_value_encrypted == before

    def test_one_corrupted_row_aborts_entire_batch(self, db_session):
        """여러 건 중 하나라도 구키로 복호화 실패하면, 나머지 정상 레코드도
        전혀 변경되지 않아야 한다(부분 성공 금지)."""
        c1 = _seed_credential(db_session, "PLATFORM", 1, "client_id", "plain-1", OLD_SECRET)
        c2 = _seed_credential(db_session, "PLATFORM", 2, "access_key", "plain-2", OLD_SECRET)
        c2.key_value_encrypted = "not-a-valid-fernet-token"  # 손상 시뮬레이션
        db_session.flush()
        before_1 = c1.key_value_encrypted

        with pytest.raises(RotationAborted):
            rotate_credentials(db_session, OLD_SECRET, NEW_SECRET, execute=True)
        db_session.rollback()

        assert c1.key_value_encrypted == before_1

    def test_old_equals_new_aborts(self, db_session):
        _seed_credential(db_session, "PLATFORM", 1, "client_id", "plain-1", OLD_SECRET)

        with pytest.raises(RotationAborted):
            rotate_credentials(db_session, OLD_SECRET, OLD_SECRET, execute=True)

    def test_missing_or_empty_key_raises_insecure_secret_error(self, db_session):
        with pytest.raises(InsecureSecretError):
            rotate_credentials(db_session, "", NEW_SECRET, execute=True)
        with pytest.raises(InsecureSecretError):
            rotate_credentials(db_session, OLD_SECRET, "", execute=True)

    def test_demo_default_as_new_key_is_rejected(self, db_session):
        with pytest.raises(InsecureSecretError):
            rotate_credentials(db_session, OLD_SECRET, "please-change-this-to-a-generated-fernet-key", execute=True)

    def test_new_key_reverification_failure_rolls_back_everything(self, db_session, monkeypatch):
        """재암호화까지는 성공했지만 신키 재검증(2차)에서 실패하면 전체 롤백돼야
        한다 - new_fernet.decrypt만 강제로 실패하게 만들어 재현한다."""
        import scripts.rotate_credential_key as rotate_mod

        c1 = _seed_credential(db_session, "PLATFORM", 1, "client_id", "plain-1", OLD_SECRET)
        # 시드 데이터를 먼저 커밋해 둔다 - flush만 된 상태로 두면 재암호화
        # 시도(및 그 실패)까지 같은 트랜잭션에 묶여, rollback()이 재암호화
        # 변경뿐 아니라 이 INSERT 자체까지 되돌리면서 비교 기준(before)이
        # 불안정해진다. 실제 운영에서도 회전 대상은 이미 커밋된 기존 데이터이므로
        # 이 편이 더 현실에 가깝다.
        db_session.commit()
        before = c1.key_value_encrypted
        real_build_fernet = rotate_mod.build_fernet

        class _BrokenFernet:
            def __init__(self, real):
                self._real = real

            def encrypt(self, data: bytes) -> bytes:
                return self._real.encrypt(data)

            def decrypt(self, data: bytes) -> bytes:
                raise ValueError("forced failure for reverification test")

        def fake_build_fernet(secret: str):
            real = real_build_fernet(secret)
            if secret == NEW_SECRET:
                return _BrokenFernet(real)
            return real

        monkeypatch.setattr(rotate_mod, "build_fernet", fake_build_fernet)

        with pytest.raises(RotationAborted):
            rotate_mod.rotate_credentials(db_session, OLD_SECRET, NEW_SECRET, execute=True)
        db_session.rollback()

        assert c1.key_value_encrypted == before  # 롤백 후 구키 암호문 그대로

    def test_rerunning_after_successful_rotation_fails_safely_with_stale_old_key(self, db_session):
        """1차 회전 성공 후, 이미 신키로 바뀐 상태에서 예전 구키를 다시 구키로
        지정해 재실행하면 1차 검증에서 안전하게 중단돼야 한다(반복 실행 안전성)."""
        c1 = _seed_credential(db_session, "PLATFORM", 1, "client_id", "plain-1", OLD_SECRET)
        rotate_credentials(db_session, OLD_SECRET, NEW_SECRET, execute=True)
        db_session.commit()
        after_first_rotation = c1.key_value_encrypted

        with pytest.raises(RotationAborted):
            rotate_credentials(db_session, OLD_SECRET, NEW_SECRET, execute=True)
        db_session.rollback()

        assert c1.key_value_encrypted == after_first_rotation


class TestLegacyInsecureOldKeyMigration:
    """--allow-legacy-insecure-old-key(=allow_legacy_insecure_old_key=True)는
    구키가 알려진 공개 데모 기본값이라는 이유 하나만 우회한다. 신키 검증,
    구키 누락/빈값 검증, 구키==신키 거부, 구키 복호화 실패 시 전체 중단은
    이 옵션과 무관하게 항상 그대로 유지돼야 한다."""

    DEMO_OLD_SECRET = "CHANGE_ME_IN_PRODUCTION"  # 실제 운영에서 쓰이고 있던 값과 동일한 알려진 데모 기본값

    def test_demo_old_key_without_flag_is_still_rejected(self, db_session):
        _seed_credential(db_session, "PLATFORM", 1, "client_id", "plain-1", self.DEMO_OLD_SECRET)

        with pytest.raises(InsecureSecretError):
            rotate_credentials(db_session, self.DEMO_OLD_SECRET, NEW_SECRET, execute=False)

    def test_demo_old_key_with_flag_dry_run_succeeds_with_zero_changes(self, db_session):
        c1 = _seed_credential(db_session, "PLATFORM", 1, "client_id", "plain-1", self.DEMO_OLD_SECRET)
        before = c1.key_value_encrypted

        result = rotate_credentials(
            db_session, self.DEMO_OLD_SECRET, NEW_SECRET, execute=False, allow_legacy_insecure_old_key=True
        )
        db_session.rollback()

        assert result.total == 1
        assert result.executed is False
        assert c1.key_value_encrypted == before

    def test_demo_old_key_with_flag_execute_succeeds_and_new_key_decrypts(self, db_session):
        c1 = _seed_credential(db_session, "PLATFORM", 1, "client_id", "plain-value-1", self.DEMO_OLD_SECRET)

        result = rotate_credentials(
            db_session, self.DEMO_OLD_SECRET, NEW_SECRET, execute=True, allow_legacy_insecure_old_key=True
        )
        db_session.commit()

        assert result.total == 1
        assert result.executed is True
        new_fernet = build_fernet(NEW_SECRET)
        assert new_fernet.decrypt(c1.key_value_encrypted.encode()).decode() == "plain-value-1"

    def test_flag_does_not_weaken_new_key_validation(self, db_session):
        _seed_credential(db_session, "PLATFORM", 1, "client_id", "plain-1", self.DEMO_OLD_SECRET)

        with pytest.raises(InsecureSecretError):
            rotate_credentials(
                db_session,
                self.DEMO_OLD_SECRET,
                "please-change-this-to-a-generated-fernet-key",
                execute=True,
                allow_legacy_insecure_old_key=True,
            )
        with pytest.raises(InsecureSecretError):
            rotate_credentials(
                db_session, self.DEMO_OLD_SECRET, "short", execute=True, allow_legacy_insecure_old_key=True
            )

    def test_flag_does_not_bypass_old_equals_new_check(self, db_session):
        same_secret = "same-secret-for-both-old-and-new-00000000"
        _seed_credential(db_session, "PLATFORM", 1, "client_id", "plain-1", same_secret)

        with pytest.raises(RotationAborted):
            rotate_credentials(db_session, same_secret, same_secret, execute=True, allow_legacy_insecure_old_key=True)

    def test_flag_does_not_bypass_wrong_old_key_decrypt_failure(self, db_session):
        c1 = _seed_credential(db_session, "PLATFORM", 1, "client_id", "plain-1", self.DEMO_OLD_SECRET)
        before = c1.key_value_encrypted
        wrong_old_secret = "totally-wrong-old-secret-9999999999"

        with pytest.raises(RotationAborted):
            rotate_credentials(
                db_session, wrong_old_secret, NEW_SECRET, execute=True, allow_legacy_insecure_old_key=True
            )
        db_session.rollback()

        assert c1.key_value_encrypted == before

    def test_legacy_migration_stdout_and_errors_never_contain_secrets_or_plaintext(self, db_session, monkeypatch):
        _seed_credential(db_session, "PLATFORM", 1, "client_id", "super-secret-plaintext", self.DEMO_OLD_SECRET)
        monkeypatch.setenv("TEST_OLD_KEY", self.DEMO_OLD_SECRET)
        monkeypatch.setenv("TEST_NEW_KEY", NEW_SECRET)
        monkeypatch.setattr(
            "sys.argv",
            [
                "rotate_credential_key.py",
                "--old-key-env",
                "TEST_OLD_KEY",
                "--new-key-env",
                "TEST_NEW_KEY",
                "--allow-legacy-insecure-old-key",
            ],
        )
        monkeypatch.setattr("scripts.rotate_credential_key.SessionLocal", lambda: db_session)
        monkeypatch.setattr(db_session, "close", lambda: None)

        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = main()
        output = buf.getvalue()

        assert exit_code == 0
        assert self.DEMO_OLD_SECRET not in output
        assert NEW_SECRET not in output
        assert "super-secret-plaintext" not in output


class TestRotateCredentialsNoSecretLeakage:
    def test_dry_run_stdout_never_contains_secrets_or_plaintext(self, db_session, monkeypatch):
        _seed_credential(db_session, "PLATFORM", 1, "client_id", "super-secret-plaintext", OLD_SECRET)
        monkeypatch.setenv("TEST_OLD_KEY", OLD_SECRET)
        monkeypatch.setenv("TEST_NEW_KEY", NEW_SECRET)
        monkeypatch.setattr(
            "sys.argv", ["rotate_credential_key.py", "--old-key-env", "TEST_OLD_KEY", "--new-key-env", "TEST_NEW_KEY"]
        )
        monkeypatch.setattr("scripts.rotate_credential_key.SessionLocal", lambda: db_session)
        # db_session.close()가 fixture의 세션을 실제로 닫아버리지 않도록 무력화한다.
        monkeypatch.setattr(db_session, "close", lambda: None)

        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = main()
        output = buf.getvalue()

        assert exit_code == 0
        assert OLD_SECRET not in output
        assert NEW_SECRET not in output
        assert "super-secret-plaintext" not in output

    def test_error_message_of_aborted_rotation_has_no_secret_or_plaintext(self, db_session):
        _seed_credential(db_session, "PLATFORM", 1, "client_id", "super-secret-plaintext", OLD_SECRET)
        wrong_old = "totally-wrong-old-secret-9999999999"

        try:
            rotate_credentials(db_session, wrong_old, NEW_SECRET, execute=True)
        except RotationAborted as e:
            message = str(e)
            assert wrong_old not in message
            assert NEW_SECRET not in message
            assert "super-secret-plaintext" not in message
        else:
            pytest.fail("RotationAborted를 기대했으나 발생하지 않았습니다.")
