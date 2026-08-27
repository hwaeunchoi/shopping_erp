"""
tests/unit/test_security.py
------------------------------
JWT_SECRET_KEY 회전이 인증에 미치는 영향을 검증한다.

core.security.create_access_token/decode_access_token은 매 호출마다
config.settings.settings.jwt_secret_key를 즉시 읽는다(앱 재시작 없이도 이
프로세스 내에서 settings 속성을 바꾸면 즉시 반영됨) - 그래서 "회전 전 키로
발급한 토큰은 회전 후 거부되고, 회전 후 키로 발급한 토큰은 통과한다"를
monkeypatch로 재현할 수 있다. 운영에서는 프로세스 재시작이 곧 이 키 교체에
해당한다.

비밀번호 해시(bcrypt)는 JWT 서명 키와 완전히 독립된 별도 메커니즘(로그인 시
credential 검증에만 쓰임)이므로, JWT 키 회전이 저장된 password_hash 값을
바꾸지 않는다는 것도 함께 확인한다.
"""

import pytest

from config.settings import settings
from core.security import create_access_token, decode_access_token, hash_password, verify_password

# 테스트 전용 dummy secret - 데모 기본값이 아니고 20자 이상.
_OLD_JWT_SECRET = "old-jwt-secret-for-rotation-test-000000"
_NEW_JWT_SECRET = "new-jwt-secret-for-rotation-test-111111"


def _make_user(db_session, username="admin"):
    from models.user import Role, User

    role = Role(name=f"role-{username}")
    db_session.add(role)
    db_session.flush()
    user = User(
        username=username, password_hash=hash_password("ChangeMe!123"), name="관리자", role_id=role.id, is_active=True
    )
    db_session.add(user)
    db_session.flush()
    return user


class TestJwtKeyRotation:
    def test_token_issued_with_old_key_is_rejected_after_rotation(self, monkeypatch):
        monkeypatch.setattr(settings, "jwt_secret_key", _OLD_JWT_SECRET)
        old_token = create_access_token(subject="admin")
        assert decode_access_token(old_token) is not None

        monkeypatch.setattr(settings, "jwt_secret_key", _NEW_JWT_SECRET)
        assert decode_access_token(old_token) is None

    def test_token_issued_with_new_key_after_rotation_is_accepted(self, monkeypatch):
        monkeypatch.setattr(settings, "jwt_secret_key", _OLD_JWT_SECRET)
        create_access_token(subject="admin")  # 회전 전 발급(사용하지 않음, 순서 확인용)

        monkeypatch.setattr(settings, "jwt_secret_key", _NEW_JWT_SECRET)
        new_token = create_access_token(subject="admin")
        payload = decode_access_token(new_token)

        assert payload is not None
        assert payload["sub"] == "admin"

    def test_jwt_rotation_does_not_change_stored_password_hash(self, db_session, monkeypatch):
        user = _make_user(db_session, "admin")
        original_hash = user.password_hash

        monkeypatch.setattr(settings, "jwt_secret_key", _OLD_JWT_SECRET)
        create_access_token(subject=user.username)
        monkeypatch.setattr(settings, "jwt_secret_key", _NEW_JWT_SECRET)
        create_access_token(subject=user.username)

        assert user.password_hash == original_hash
        assert verify_password("ChangeMe!123", user.password_hash) is True


class TestSchedulerStartupFailClosed:
    """scheduler/scheduler.py::main()은 validate_startup_secrets()를 잡 등록보다
    먼저 호출한다(core.crypto와 api/main.py 것과 완전히 동일한 함수) - Secret이
    안전하지 않으면 build_scheduler()/scheduler.start()(블로킹)에 도달하기
    전에 예외를 던지고 끝나야 한다."""

    def test_invalid_secret_raises_before_building_scheduler(self, monkeypatch):
        import scheduler.scheduler as scheduler_mod
        from core.crypto import InsecureSecretError

        monkeypatch.setattr(scheduler_mod.settings, "jwt_secret_key", "CHANGE_ME_IN_PRODUCTION")
        monkeypatch.setattr(scheduler_mod.settings, "credential_encryption_key", _NEW_JWT_SECRET)

        called = {"build_scheduler": False}
        monkeypatch.setattr(scheduler_mod, "build_scheduler", lambda: called.__setitem__("build_scheduler", True))

        with pytest.raises(InsecureSecretError):
            scheduler_mod.main()

        assert called["build_scheduler"] is False

    def test_valid_secrets_reach_build_scheduler(self, monkeypatch):
        import scheduler.scheduler as scheduler_mod

        monkeypatch.setattr(scheduler_mod.settings, "jwt_secret_key", _OLD_JWT_SECRET)
        monkeypatch.setattr(scheduler_mod.settings, "credential_encryption_key", _NEW_JWT_SECRET)

        called = {"build_scheduler": False}

        class _FakeScheduler:
            def start(self):
                pass

        def _fake_build_scheduler():
            called["build_scheduler"] = True
            return _FakeScheduler()

        monkeypatch.setattr(scheduler_mod, "build_scheduler", _fake_build_scheduler)
        monkeypatch.setattr(scheduler_mod, "setup_logging", lambda: None)

        scheduler_mod.main()  # _FakeScheduler.start()는 즉시 반환하므로 블로킹되지 않는다.

        assert called["build_scheduler"] is True
