"""
tests/integration/_pg_rotation_runner.py
------------------------------------------------
tests/integration/test_rotate_postgres_password_pg.py가 도커 "러너"
컨테이너 안에서 실행시키는 스크립트(파일명이 test_로 시작하지 않으므로
pytest가 이 파일 자체를 테스트로 수집하지 않는다).

환경변수(PGROT_*)로 시나리오와 접속 정보를 받아 실제
scripts.rotate_postgres_password 로직을 "진짜" PostgreSQL(같은 docker
network에 떠 있는 임시 컨테이너, PGROT_DB_HOST)에 대해 실행하고, 결과를
한 줄 JSON(상태값만 - password/URL 원문은 절대 담지 않음)으로 표준출력에
남긴다. 이 JSON을 호스트 쪽 pytest가 파싱해 검증한다.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sqlalchemy.engine import make_url  # noqa: E402

from scripts.rotate_postgres_password import (  # noqa: E402
    RotationAborted,
    RotationCritical,
    RotationRecovered,
    _connect,
    rotate_postgres_password,
)


def _url(password: str) -> str:
    host = os.environ["PGROT_DB_HOST"]
    user = os.environ["PGROT_DB_USER"]
    db = os.environ["PGROT_DB_NAME"]
    return f"postgresql+psycopg://{user}:{password}@{host}:5432/{db}"


def _can_connect(url_raw: str) -> bool:
    try:
        conn = _connect(make_url(url_raw))
        conn.close()
        return True
    except Exception:
        return False


def _forced_failure_connect_fn(fail_on_call: int):
    """connect_fn(url) 호출 순서 중 fail_on_call번째 호출만 실패시키고
    나머지는 실제 _connect를 그대로 쓴다 - ALTER ROLE/보상 롤백 SQL 자체는
    진짜 PostgreSQL에 대해 실행되게 하면서, "신규 연결 검증"만 인위적으로
    깨서 보상 롤백 경로를 실제 DB로 검증하기 위함이다."""
    state = {"n": 0}

    def _fn(url):
        state["n"] += 1
        if state["n"] == fail_on_call:
            raise RuntimeError("integration-test-forced-verification-failure")
        return _connect(url)

    return _fn


def main() -> int:
    scenario = os.environ["PGROT_SCENARIO"]
    old_password = os.environ["PGROT_OLD_PASSWORD"]
    new_password = os.environ["PGROT_NEW_PASSWORD"]

    current_url = _url(old_password)
    new_url = _url(new_password)

    out: dict = {"scenario": scenario}

    if scenario == "dry_run_then_reject_new":
        result = rotate_postgres_password(current_url, new_url, new_password, execute=False)
        out["dry_run_executed"] = result.executed
        out["new_password_works_before_execute"] = _can_connect(new_url)
        out["old_password_still_works_after_dry_run"] = _can_connect(current_url)

    elif scenario == "execute_success":
        result = rotate_postgres_password(current_url, new_url, new_password, execute=True)
        out["execute_executed"] = result.executed
        out["new_password_connects_after_execute"] = _can_connect(new_url)
        out["old_password_still_connects_after_execute"] = _can_connect(current_url)

    elif scenario == "recovery_on_verification_failure":
        forced_fn = _forced_failure_connect_fn(fail_on_call=2)
        raised = None
        try:
            rotate_postgres_password(current_url, new_url, new_password, execute=True, connect_fn=forced_fn)
        except RotationRecovered:
            raised = "RotationRecovered"
        except RotationCritical:
            raised = "RotationCritical"
        except RotationAborted:
            raised = "RotationAborted"
        out["raised"] = raised
        out["old_password_connects_after_recovery"] = _can_connect(current_url)
        out["new_password_connects_after_recovery"] = _can_connect(new_url)

    elif scenario == "mismatched_database_zero_change":
        other_db_url = current_url.rsplit("/", 1)[0] + "/other_database_name"
        raised = None
        try:
            rotate_postgres_password(current_url, other_db_url, new_password, execute=True)
        except RotationAborted:
            raised = "RotationAborted"
        except Exception as e:  # noqa: BLE001
            raised = type(e).__name__
        out["raised"] = raised
        out["old_password_still_works"] = _can_connect(current_url)

    elif scenario == "rerun_after_success_is_safe":
        # 호출 측(host pytest)이 이 시나리오를 부르기 전에 이미
        # execute_success 시나리오를 먼저 실행해 role의 실제 password가
        # new_password로 바뀐 상태다. 여기서는 "이미 무효가 된 old_password"
        # 로 다시 실행했을 때 안전하게 막히는지만 본다.
        raised = None
        try:
            rotate_postgres_password(current_url, new_url, new_password, execute=True)
        except RotationAborted:
            raised = "RotationAborted"
        except Exception as e:  # noqa: BLE001
            raised = type(e).__name__
        out["raised"] = raised

    else:
        raise SystemExit(f"unknown scenario: {scenario}")

    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
