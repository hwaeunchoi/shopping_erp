"""
tests/unit/test_rotate_postgres_password.py
------------------------------------------------
scripts.rotate_postgres_password 단위 테스트.

실제 PostgreSQL 없이, connect_fn을 가짜 커넥션으로 주입해
rotate_postgres_password()의 검증/실행/보상 롤백 분기 전체를 검증한다.
실 PostgreSQL 대상 통합 검증은 tests/integration/test_rotate_postgres_password_pg.py
(임시 1회성 컨테이너)에서 별도로 다룬다.
"""

import io
from contextlib import redirect_stdout
from urllib.parse import quote

import pytest

from core.crypto import InsecureSecretError
from scripts.rotate_postgres_password import (
    RotationAborted,
    RotationCritical,
    RotationRecovered,
    RotationResult,
    _parse_url,
    main,
    rotate_postgres_password,
)

# 테스트 전용 dummy - 데모 기본값이 아니고 20자 이상.
CURRENT_PASSWORD = "current-postgres-password-for-tests-000000"
NEW_PASSWORD = "new-postgres-password-for-tests-111111111111"

CURRENT_URL = f"postgresql+psycopg://erp_user:{CURRENT_PASSWORD}@db:5432/erp_db"
NEW_URL = f"postgresql+psycopg://erp_user:{NEW_PASSWORD}@db:5432/erp_db"


class FakeCursor:
    def __init__(self, conn: "FakeConnection"):
        self._conn = conn
        self._last_result = None

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, query, *args, **kwargs) -> None:
        if isinstance(query, str) and "current_user" in query:
            self._last_result = self._conn._pop_identity()
            return
        # 그 외 execute는 전부 ALTER ROLE 시도로 취급한다(이 테스트 파일에서
        # 이 함수를 호출하는 경로는 두 곳뿐 - 정방향/보상 ALTER).
        self._conn.alter_calls += 1
        if self._conn.alter_calls in self._conn.raise_on_alter:
            raise RuntimeError("simulated ALTER ROLE failure")

    def fetchone(self):
        return self._last_result


class FakeConnection:
    def __init__(
        self,
        identities=None,
        default_identity=("erp_user", "erp_db"),
        raise_on_alter=(),
        raise_on_commit=(),
        raise_on_rollback=False,
    ):
        self._identities = list(identities) if identities is not None else []
        self._default_identity = default_identity
        self.raise_on_alter = set(raise_on_alter)
        self.raise_on_commit = set(raise_on_commit)
        self.raise_on_rollback = raise_on_rollback
        self.alter_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0
        self.closed = False

    def _pop_identity(self):
        if self._identities:
            return self._identities.pop(0)
        return self._default_identity

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commit_calls += 1
        if self.commit_calls in self.raise_on_commit:
            raise RuntimeError("simulated commit failure")

    def rollback(self) -> None:
        self.rollback_calls += 1
        if self.raise_on_rollback:
            raise RuntimeError("simulated rollback failure")

    def close(self) -> None:
        self.closed = True


class ScriptedConnectFn:
    """connect_fn(url) 호출 순서대로 미리 준비한 FakeConnection(또는 예외)을
    내준다. 리스트의 항목이 Exception 인스턴스면 그 자리에서 raise한다."""

    def __init__(self, items):
        self._items = list(items)
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if not self._items:
            raise AssertionError("connect_fn이 예상보다 더 많이 호출됐습니다.")
        item = self._items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _never_connect(url):
    raise AssertionError("이 테스트에서는 connect_fn이 호출되면 안 됩니다(검증 단계에서 걸러져야 함).")


class TestUrlParsingAndValidation:
    def test_parses_standard_url(self):
        url = _parse_url(CURRENT_URL, "current")
        assert url.username == "erp_user"
        assert url.host == "db"
        assert url.port == 5432
        assert url.database == "erp_db"
        assert url.password == CURRENT_PASSWORD

    def test_percent_encoded_password_decodes_correctly(self):
        raw_password = "abc@def#123456789012345"
        encoded = quote(raw_password, safe="")
        url_string = f"postgresql+psycopg://erp_user:{encoded}@db:5432/erp_db"

        url = _parse_url(url_string, "current")

        assert url.password == raw_password

    def test_mismatched_user_blocks_before_any_connection(self):
        other_user_url = f"postgresql+psycopg://other_user:{NEW_PASSWORD}@db:5432/erp_db"
        with pytest.raises(RotationAborted):
            rotate_postgres_password(
                CURRENT_URL, other_user_url, NEW_PASSWORD, execute=False, connect_fn=_never_connect
            )

    def test_mismatched_host_blocks_before_any_connection(self):
        other_host_url = f"postgresql+psycopg://erp_user:{NEW_PASSWORD}@other-host:5432/erp_db"
        with pytest.raises(RotationAborted):
            rotate_postgres_password(
                CURRENT_URL, other_host_url, NEW_PASSWORD, execute=False, connect_fn=_never_connect
            )

    def test_mismatched_port_blocks_before_any_connection(self):
        other_port_url = f"postgresql+psycopg://erp_user:{NEW_PASSWORD}@db:5433/erp_db"
        with pytest.raises(RotationAborted):
            rotate_postgres_password(
                CURRENT_URL, other_port_url, NEW_PASSWORD, execute=False, connect_fn=_never_connect
            )

    def test_mismatched_database_blocks_before_any_connection(self):
        other_db_url = f"postgresql+psycopg://erp_user:{NEW_PASSWORD}@db:5432/other_db"
        with pytest.raises(RotationAborted):
            rotate_postgres_password(CURRENT_URL, other_db_url, NEW_PASSWORD, execute=False, connect_fn=_never_connect)

    def test_mismatched_driver_blocks_before_any_connection(self):
        other_driver_url = f"postgresql://erp_user:{NEW_PASSWORD}@db:5432/erp_db"
        with pytest.raises(RotationAborted):
            rotate_postgres_password(
                CURRENT_URL, other_driver_url, NEW_PASSWORD, execute=False, connect_fn=_never_connect
            )

    def test_new_url_password_mismatch_with_env_blocks(self):
        different_password = "totally-different-new-password-mismatch-0000"
        with pytest.raises(RotationAborted):
            rotate_postgres_password(CURRENT_URL, NEW_URL, different_password, execute=False, connect_fn=_never_connect)

    def test_demo_default_new_password_blocks(self):
        demo_url = "postgresql+psycopg://erp_user:CHANGE_ME_IN_PRODUCTION@db:5432/erp_db"
        with pytest.raises(InsecureSecretError):
            rotate_postgres_password(
                CURRENT_URL, demo_url, "CHANGE_ME_IN_PRODUCTION", execute=False, connect_fn=_never_connect
            )

    def test_too_short_new_password_blocks(self):
        short_url = "postgresql+psycopg://erp_user:short@db:5432/erp_db"
        with pytest.raises(InsecureSecretError):
            rotate_postgres_password(CURRENT_URL, short_url, "short", execute=False, connect_fn=_never_connect)

    def test_same_old_new_password_blocks(self):
        same_url = f"postgresql+psycopg://erp_user:{CURRENT_PASSWORD}@db:5432/erp_db"
        with pytest.raises(RotationAborted):
            rotate_postgres_password(CURRENT_URL, same_url, CURRENT_PASSWORD, execute=False, connect_fn=_never_connect)


class TestDryRun:
    def test_dry_run_makes_zero_alter_calls(self):
        conn = FakeConnection()
        connect_fn = ScriptedConnectFn([conn])

        result = rotate_postgres_password(CURRENT_URL, NEW_URL, NEW_PASSWORD, execute=False, connect_fn=connect_fn)

        assert result == RotationResult(executed=False, role="erp_user", database="erp_db")
        assert conn.alter_calls == 0
        assert conn.commit_calls == 0
        assert conn.closed is True

    def test_dry_run_connection_identity_mismatch_blocks(self):
        conn = FakeConnection(default_identity=("someone_else", "erp_db"))
        connect_fn = ScriptedConnectFn([conn])

        with pytest.raises(RotationAborted):
            rotate_postgres_password(CURRENT_URL, NEW_URL, NEW_PASSWORD, execute=False, connect_fn=connect_fn)

        assert conn.alter_calls == 0


class TestExecuteHappyPath:
    def test_execute_success_full_flow(self):
        current_conn = FakeConnection()
        new_conn = FakeConnection()
        connect_fn = ScriptedConnectFn([current_conn, new_conn])

        result = rotate_postgres_password(CURRENT_URL, NEW_URL, NEW_PASSWORD, execute=True, connect_fn=connect_fn)

        assert result == RotationResult(executed=True, role="erp_user", database="erp_db")
        assert current_conn.alter_calls == 1
        assert current_conn.commit_calls == 1
        assert current_conn.rollback_calls == 0
        assert new_conn.closed is True
        assert current_conn.closed is True
        assert len(connect_fn.calls) == 2


class TestExecuteFailureAndRecovery:
    def test_alter_failure_aborts_with_rollback_zero_commit(self):
        conn = FakeConnection(raise_on_alter={1})
        connect_fn = ScriptedConnectFn([conn])

        with pytest.raises(RotationAborted):
            rotate_postgres_password(CURRENT_URL, NEW_URL, NEW_PASSWORD, execute=True, connect_fn=connect_fn)

        assert conn.commit_calls == 0
        assert conn.rollback_calls == 1
        assert len(connect_fn.calls) == 1  # 신규 연결 시도까지 가지 않음

    def test_commit_failure_aborts_with_rollback(self):
        conn = FakeConnection(raise_on_commit={1})
        connect_fn = ScriptedConnectFn([conn])

        with pytest.raises(RotationAborted):
            rotate_postgres_password(CURRENT_URL, NEW_URL, NEW_PASSWORD, execute=True, connect_fn=connect_fn)

        assert conn.alter_calls == 1
        assert conn.rollback_calls == 1
        assert len(connect_fn.calls) == 1

    def test_new_connection_failure_recovers_to_old_password(self):
        current_conn = FakeConnection()
        recheck_conn = FakeConnection()
        connect_fn = ScriptedConnectFn([current_conn, RuntimeError("simulated connection refused"), recheck_conn])

        with pytest.raises(RotationRecovered):
            rotate_postgres_password(CURRENT_URL, NEW_URL, NEW_PASSWORD, execute=True, connect_fn=connect_fn)

        assert current_conn.alter_calls == 2  # 정방향 1회 + 보상 롤백 1회
        assert current_conn.commit_calls == 2
        assert len(connect_fn.calls) == 3
        assert recheck_conn.closed is True

    def test_new_connection_identity_mismatch_recovers(self):
        current_conn = FakeConnection()
        new_conn = FakeConnection(default_identity=("someone_else", "erp_db"))
        recheck_conn = FakeConnection()
        connect_fn = ScriptedConnectFn([current_conn, new_conn, recheck_conn])

        with pytest.raises(RotationRecovered):
            rotate_postgres_password(CURRENT_URL, NEW_URL, NEW_PASSWORD, execute=True, connect_fn=connect_fn)

        assert current_conn.alter_calls == 2
        assert current_conn.commit_calls == 2

    def test_recovery_alter_failure_is_critical(self):
        current_conn = FakeConnection(raise_on_alter={2})  # 1회차(정방향)는 성공, 2회차(보상)에서 실패
        connect_fn = ScriptedConnectFn([current_conn, RuntimeError("simulated connection refused")])

        with pytest.raises(RotationCritical):
            rotate_postgres_password(CURRENT_URL, NEW_URL, NEW_PASSWORD, execute=True, connect_fn=connect_fn)

    def test_recovery_recheck_connection_failure_is_critical(self):
        current_conn = FakeConnection()
        connect_fn = ScriptedConnectFn(
            [current_conn, RuntimeError("simulated connection refused"), RuntimeError("recheck connection failed")]
        )

        with pytest.raises(RotationCritical):
            rotate_postgres_password(CURRENT_URL, NEW_URL, NEW_PASSWORD, execute=True, connect_fn=connect_fn)

        # 보상 ALTER/commit 자체는 성공했어야 한다(재확인 연결만 실패).
        assert current_conn.alter_calls == 2
        assert current_conn.commit_calls == 2


class TestNoSecretLeakage:
    def test_exception_messages_never_contain_password(self):
        conn = FakeConnection(raise_on_alter={1})
        connect_fn = ScriptedConnectFn([conn])

        try:
            rotate_postgres_password(CURRENT_URL, NEW_URL, NEW_PASSWORD, execute=True, connect_fn=connect_fn)
        except RotationAborted as e:
            message = str(e)
            assert CURRENT_PASSWORD not in message
            assert NEW_PASSWORD not in message
            assert CURRENT_URL not in message
            assert NEW_URL not in message
        else:
            pytest.fail("RotationAborted를 기대했으나 발생하지 않았습니다.")

    def test_main_stdout_never_contains_password_or_url(self, monkeypatch):
        import scripts.rotate_postgres_password as rotate_mod

        def fake_rotate(current_url_raw, new_url_raw, new_password, execute, connect_fn=None):
            assert current_url_raw == CURRENT_URL
            assert new_url_raw == NEW_URL
            assert new_password == NEW_PASSWORD
            return RotationResult(executed=False, role="erp_user", database="erp_db")

        monkeypatch.setattr(rotate_mod, "rotate_postgres_password", fake_rotate)
        monkeypatch.setenv("TEST_PG_CURRENT_URL", CURRENT_URL)
        monkeypatch.setenv("TEST_PG_NEW_URL", NEW_URL)
        monkeypatch.setenv("TEST_PG_NEW_PASSWORD", NEW_PASSWORD)
        monkeypatch.setattr(
            "sys.argv",
            [
                "rotate_postgres_password.py",
                "--current-url-env",
                "TEST_PG_CURRENT_URL",
                "--new-url-env",
                "TEST_PG_NEW_URL",
                "--new-password-env",
                "TEST_PG_NEW_PASSWORD",
            ],
        )

        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = rotate_mod.main()
        output = buf.getvalue()

        assert exit_code == 0
        assert CURRENT_PASSWORD not in output
        assert NEW_PASSWORD not in output
        assert CURRENT_URL not in output
        assert NEW_URL not in output

    def test_main_missing_env_var_aborts_without_secret_leakage(self, monkeypatch):
        monkeypatch.setattr(
            "sys.argv",
            [
                "rotate_postgres_password.py",
                "--current-url-env",
                "DOES_NOT_EXIST_CURRENT",
                "--new-url-env",
                "DOES_NOT_EXIST_NEW",
                "--new-password-env",
                "DOES_NOT_EXIST_PASSWORD",
            ],
        )
        monkeypatch.delenv("DOES_NOT_EXIST_CURRENT", raising=False)
        monkeypatch.delenv("DOES_NOT_EXIST_NEW", raising=False)
        monkeypatch.delenv("DOES_NOT_EXIST_PASSWORD", raising=False)

        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = main()

        assert exit_code == 1
