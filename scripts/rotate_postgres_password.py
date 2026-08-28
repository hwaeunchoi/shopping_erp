"""
scripts/rotate_postgres_password.py
------------------------------------------
PostgreSQL 접속 role(현재 URL로 실제 연결한 role 자신 - CLI로 임의 role명을
받지 않는다)의 비밀번호를 회전한다. 기본은 dry-run이며 실제 변경은
--execute가 있어야만 수행한다.

왜 "원자적"이라고 부르지 않는가
--------------------------------
scripts/rotate_credential_key.py(DB 행 재암호화)와 달리, 이 도구는 완전한
단일 트랜잭션 원자성을 가질 수 없다. ALTER ROLE ... WITH PASSWORD는 그
자체로 하나의 트랜잭션(성공하면 그 즉시 그 role의 유효 password가 바뀐다)
이고, "새 password가 실제로 통하는지"는 정의상 별도의 새 연결로만 확인할
수 있기 때문이다 - 같은 트랜잭션 안에서 "커밋 후 재검증"을 할 방법이 없다.
그래서 이 도구는:
  1) 기존 연결(conn)을 계속 열어둔 채로 ALTER ROLE + commit
  2) 신규 URL/신규 password로 "새 연결"을 열어 SELECT 1 / current_user /
     current_database()로 검증
  3) 검증 실패 시 - 아직 살아있는 1)의 conn(구 password로 이미 인증된
     세션이므로 role의 password가 바뀌어도 이 세션 자체는 안 끊긴다)을 그대로
     재사용해 구 password로 되돌리는 "보상 롤백(compensating rollback)"을
     수행한다.
라는 "검증 후 보상 롤백" 방식으로 구현한다. 즉 ALTER ROLE commit 시점과
새 연결 검증 시점 사이에는 "role의 실제 password는 이미 새 값인데 아직
아무도 그걸로 접속을 확인하지 못한" 아주 짧은 비원자 구간이 존재한다.

이 짧은 구간에 프로세스가 죽으면?
  이 도구가 실행되기 전에 이미 (요청받은 운영 절차상) 새 password가 담긴
  Secret 파일(예: erp_production_next_<timestamp>.env)이 저장소 밖에
  안전하게 보존되어 있어야 한다. 그 파일의 POSTGRES_PASSWORD/DATABASE_URL이
  바로 이 도구가 실제로 적용한 값과 동일하므로, 프로세스가 죽어도 "그 Secret
  파일을 그대로 새 운영 .env로 반영"하면 복구된다(DEPLOYMENT.md 8절 참고).
  이 도구 자체가 그 Secret 파일을 새로 만들거나 수정하지는 않는다.

권한/대상 제한
----------------
role 이름을 CLI 인자로 받지 않는다. current URL로 실제 연결한 뒤
current_user/current_database()를 DB에 직접 물어보고, URL이 주장하는
값과 일치하는지 확인한 다음, "그 확인된 current_user 자신"만 ALTER
ROLE 대상으로 삼는다 - 즉 URL 문자열이 뭐라고 주장하든, 실제로 그 자격
증명으로 로그인해서 얻은 role 외에는 절대 변경 대상이 될 수 없다.
identifier(role명)는 psycopg.sql.Identifier로, password는
psycopg.sql.Literal로 구성한다 - PostgreSQL의 ALTER ROLE ... PASSWORD
절 문법은 일반 파라미터 바인딩(플레이스홀더)을 지원하지 않으므로(DDL이라
리터럴만 허용), 값이 SQL 문자열에 그대로 이어붙여지지 않도록 psycopg가
제공하는 안전한 리터럴/식별자 조합(내부적으로 올바른 이스케이프/인용을
거침 - 파라미터 바인딩과 동등한 안전성)을 쓴다.

실행 예:
    python scripts/rotate_postgres_password.py \\
        --current-url-env OLD_DATABASE_URL \\
        --new-url-env NEW_DATABASE_URL \\
        --new-password-env NEW_POSTGRES_PASSWORD
    python scripts/rotate_postgres_password.py \\
        --current-url-env OLD_DATABASE_URL \\
        --new-url-env NEW_DATABASE_URL \\
        --new-password-env NEW_POSTGRES_PASSWORD --execute

종료 코드:
    0 = dry-run 통과(변경 없음) 또는 execute 성공(새 password로 전환 완료)
    1 = 변경 시도 전 안전 중단(입력 검증 실패/연결 실패/대상 불일치) - DB 무변경
    2 = execute를 시도했으나 실패했고, 기존 password로 안전하게 복구 완료
    3 = CRITICAL - 복구(보상 롤백)조차 실패, 즉시 수동 개입 필요
"""

import argparse
import contextlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402
from sqlalchemy.engine import URL, make_url  # noqa: E402

from core.crypto import InsecureSecretError, validate_production_secret  # noqa: E402

ConnectFn = Callable[[URL], "psycopg.Connection"]


class RotationAborted(Exception):
    """변경 시도 전에 안전하게 중단됐다 - DB에는 어떤 변경도 없다.
    메시지에 password/URL 원문을 절대 담지 않는다."""


class RotationRecovered(Exception):
    """execute를 시도했으나 신규 password 검증에 실패했고, 기존 password로
    성공적으로 복구(보상 롤백)됐다. 롤백 자체는 성공했으므로 CRITICAL은 아니다."""


class RotationCritical(Exception):
    """신규 password 검증 실패 후 복구(보상 롤백) 시도 자체가 실패했다.
    role의 실제 password 상태가 불확실하므로 즉시 수동 개입이 필요하다."""


@dataclass
class RotationResult:
    executed: bool
    role: Optional[str] = None
    database: Optional[str] = None


def _parse_url(raw: str, label: str) -> URL:
    try:
        return make_url(raw)
    except Exception as e:
        raise RotationAborted(f"{label} URL 파싱에 실패했습니다.") from e


def _connect(url: URL, connect_timeout: int = 5) -> "psycopg.Connection":
    """SQLAlchemy URL 객체에서 접속 정보를 뽑아 psycopg로 직접 연결한다.
    URL 원문 문자열을 그대로 드라이버에 넘기지 않고 구성요소(host/port/
    user/password/dbname)만 넘겨, 로그·예외에 완성된 URL 문자열이 섞여
    나올 여지를 없앤다."""
    try:
        return psycopg.connect(
            host=url.host,
            port=url.port,
            user=url.username,
            password=url.password,
            dbname=url.database,
            connect_timeout=connect_timeout,
        )
    except Exception as e:
        raise RotationAborted(f"DB 연결에 실패했습니다({url.host}:{url.port}/{url.database}).") from e


def _verify_connected_identity(
    conn: "psycopg.Connection", expected_user: Optional[str], expected_database: Optional[str], label: str
) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT current_user, current_database()")
        row = cur.fetchone()
    if row is None:
        raise RotationAborted(f"{label} 연결에서 current_user/current_database() 조회 결과가 없습니다.")
    actual_user, actual_database = row[0], row[1]
    if actual_user != expected_user or actual_database != expected_database:
        raise RotationAborted(f"{label} 연결의 실제 접속 대상이 URL이 주장하는 값과 일치하지 않습니다.")


def _validate_inputs(current_url: URL, new_url: URL, new_password: str) -> None:
    if current_url.username != new_url.username:
        raise RotationAborted("current/new URL의 DB user가 서로 다릅니다.")
    if current_url.host != new_url.host:
        raise RotationAborted("current/new URL의 host가 서로 다릅니다.")
    if current_url.port != new_url.port:
        raise RotationAborted("current/new URL의 port가 서로 다릅니다.")
    if current_url.database != new_url.database:
        raise RotationAborted("current/new URL의 database가 서로 다릅니다.")
    if current_url.drivername != new_url.drivername:
        raise RotationAborted("current/new URL의 driver가 서로 다릅니다(의도치 않은 driver 변경 가능성).")
    if new_url.password != new_password:
        raise RotationAborted("new URL에 포함된 password가 --new-password-env 값과 일치하지 않습니다.")

    validate_production_secret("new PostgreSQL password", new_password)
    if new_password == current_url.password:
        raise RotationAborted("새 password가 기존 password와 동일합니다.")


def rotate_postgres_password(
    current_url_raw: str, new_url_raw: str, new_password: str, execute: bool, connect_fn: ConnectFn = _connect
) -> RotationResult:
    """current_url_raw로 실제 연결해 대상을 확인한 뒤, execute=True면 그
    연결로 인증된 role 자신의 password를 new_password로 바꾸고 새 연결로
    검증한다. 검증 실패 시 기존 password로 보상 롤백을 시도한다.

    connect_fn을 주입 가능하게 분리해, 실제 psycopg 연결 없이도(단위
    테스트에서 가짜 연결을 주입해) 이 함수의 분기 로직 전체를 검증할 수
    있게 했다."""
    current_url = _parse_url(current_url_raw, "current")
    new_url = _parse_url(new_url_raw, "new")
    _validate_inputs(current_url, new_url, new_password)

    conn = connect_fn(current_url)
    try:
        _verify_connected_identity(conn, current_url.username, current_url.database, "current")
        if current_url.username is None:
            raise RotationAborted("current URL에 DB user가 없습니다.")
        role = current_url.username
        database = current_url.database

        if not execute:
            return RotationResult(executed=False, role=role, database=database)

        try:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("ALTER ROLE {} WITH PASSWORD {}").format(sql.Identifier(role), sql.Literal(new_password))
                )
            conn.commit()
        except Exception as e:
            with contextlib.suppress(Exception):
                conn.rollback()
            raise RotationAborted("ALTER ROLE 또는 commit이 실패했습니다 - 기존 password가 유지됩니다.") from e

        # --- 여기부터 role의 실제 password는 이미 new_password로 반영됐다.
        # 아래 검증이 끝나기 전까지는 "커밋됐지만 아직 확인되지 않은" 비원자 구간이다.
        try:
            verify_conn = connect_fn(new_url)
            try:
                _verify_connected_identity(verify_conn, new_url.username, new_url.database, "new")
            finally:
                verify_conn.close()
        except Exception as verify_exc:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        sql.SQL("ALTER ROLE {} WITH PASSWORD {}").format(
                            sql.Identifier(role), sql.Literal(current_url.password)
                        )
                    )
                conn.commit()
            except Exception as rollback_exc:
                raise RotationCritical(
                    "신규 password 검증 실패 + 기존 password로의 복구(ALTER/commit)도 실패했습니다 - "
                    "role의 실제 password 상태가 불확실합니다. 즉시 수동 개입이 필요합니다."
                ) from rollback_exc

            try:
                recheck_conn = connect_fn(current_url)
                try:
                    _verify_connected_identity(recheck_conn, current_url.username, current_url.database, "recovered")
                finally:
                    recheck_conn.close()
            except Exception as recheck_exc:
                raise RotationCritical(
                    "복구 ALTER/commit은 성공했지만 기존 password로의 재연결 검증이 실패했습니다 - "
                    "즉시 수동 개입이 필요합니다."
                ) from recheck_exc

            raise RotationRecovered(
                "신규 password 검증에 실패해 기존 password로 안전하게 복구했습니다(변경 없음)."
            ) from verify_exc

        return RotationResult(executed=True, role=role, database=database)
    finally:
        conn.close()


def _read_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise InsecureSecretError(f"환경변수 {name}가 설정되어 있지 않습니다.")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="PostgreSQL role password 회전(기본 dry-run)")
    parser.add_argument("--current-url-env", required=True, help="현재 DATABASE_URL 값이 담긴 환경변수 이름")
    parser.add_argument("--new-url-env", required=True, help="새 DATABASE_URL 값이 담긴 환경변수 이름")
    parser.add_argument("--new-password-env", required=True, help="새 POSTGRES_PASSWORD 값이 담긴 환경변수 이름")
    parser.add_argument("--execute", action="store_true", help="실제 회전 수행(미지정 시 dry-run)")
    args = parser.parse_args()

    try:
        current_url_raw = _read_env(args.current_url_env)
        new_url_raw = _read_env(args.new_url_env)
        new_password = _read_env(args.new_password_env)
    except InsecureSecretError as e:
        print(f"[중단] {e}")
        return 1

    try:
        result = rotate_postgres_password(current_url_raw, new_url_raw, new_password, execute=args.execute)
    except RotationCritical as e:
        print(f"[CRITICAL] {e}")
        return 3
    except RotationRecovered as e:
        print(f"[복구완료] {e}")
        return 2
    except (RotationAborted, InsecureSecretError) as e:
        print(f"[중단] {e}")
        return 1

    if not result.executed:
        print(f"[DRY-RUN] role={result.role} database={result.database} - 검증 통과, 변경 0건.")
        return 0

    print(f"[완료] role={result.role} database={result.database} - password 회전 및 신규 연결 검증 완료.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
