"""
services/cs_sync_lock.py
---------------------------
CS 문의 동기화(자동 15분 실행 / 재시작 직후 catch-up / 수동 sync)의 동시 실행 방지.

잠금 범위: (platform_id, source) 단위다 - 같은 플랫폼의 같은 source를 동시에 두 곳에서
외부 호출/DB 쓰기 하지 못하게 한다. 서로 다른 source(예: 콜센터 문의와 상품별 문의)나 서로
다른 플랫폼은 서로를 막지 않는다. 호출자는 사용하려는 source마다 이 잠금을 따로 잡는다.

구현:
- PostgreSQL: 세션 수준 advisory lock(pg_try_advisory_lock(classid, objid), 논블로킹)을
  전용 AUTOCOMMIT 커넥션 하나에 건다 - services/postgres_backup_service.py와 같은 패턴이다.
  데이터 쓰기를 하는 호출자의 세션/트랜잭션과 완전히 분리된 커넥션이라, 호출자가 commit/
  rollback 해도 잠금은 영향받지 않고, 컨텍스트가 끝나면(예외 포함) 반드시 unlock 후
  커넥션을 닫는다(커넥션이 끊겨도 세션 lock은 자동 해제된다).
- 그 외(SQLite 등 - 주로 단위 테스트): advisory lock이 없으므로 같은 프로세스 안의
  threading.Lock 레지스트리로 대체한다. 프로세스 간 보호는 PostgreSQL에서만 보장된다
  (운영 DB가 PostgreSQL이다).

패자(이미 다른 실행이 잠금을 가진 쪽)는 대기·재시도하지 않고 acquired=False를 받는다 -
호출자는 그 source를 "ALREADY_RUNNING"으로 처리하고 외부 호출·DB 쓰기를 하지 않는다.
"""

import logging
import threading
from contextlib import contextmanager
from typing import Iterator, Union

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from core.advisory_locks import cs_sync_lock_key

logger = logging.getLogger(__name__)

# pg_try_advisory_lock(int4, int4) 키: classid = registry가 예약한 CS 영역 기준값 + source 번호, objid = platform_id.
# platforms.id가 PostgreSQL integer(int4)라 platform_id를 그대로 objid에 쓰면 int4 전체 범위(0 ~ 2^31-1)가
# 해시 없이 단사로 들어간다. 다른 기능 영역(백업·외부 명령)과의 분리는 core/advisory_locks.py가 보장한다.
_SOURCE_INDEX: dict[str, int] = {"COUPANG_CALL_CENTER": 0, "COUPANG_PRODUCT_INQUIRY": 1}

_local_locks: dict[tuple[int, int], threading.Lock] = {}
_local_registry_guard = threading.Lock()


def lock_key(platform_id: int, source: str) -> tuple[int, int]:
    """(platform_id, source) -> advisory lock의 (classid, objid). 알 수 없는 source나 int4 범위를 벗어난
    platform_id는 거부한다(잠금 키가 충돌하거나 조용히 무잠금이 되는 일이 없도록)."""
    if source not in _SOURCE_INDEX:
        raise ValueError(f"알 수 없는 CS 동기화 source입니다: {source}")
    return cs_sync_lock_key(platform_id, _SOURCE_INDEX[source])


@contextmanager
def cs_sync_source_lock(platform_id: int, source: str, engine: Union[Engine, Connection]) -> Iterator[bool]:
    """(platform_id, source) 잠금을 논블로킹으로 시도한다. `with ... as acquired:`에서
    acquired가 True일 때만 외부 호출/DB 쓰기를 해야 한다."""
    classid, objid = lock_key(platform_id, source)
    # Session.get_bind()는 Engine 또는 Connection일 수 있다 - 항상 Engine으로 정규화한다.
    eng = engine.engine

    if eng.dialect.name != "postgresql":
        with _local_registry_guard:
            lock = _local_locks.setdefault((classid, objid), threading.Lock())
        acquired = lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()
        return

    conn = eng.connect()
    acquired = False
    try:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        acquired = bool(
            conn.execute(
                text("SELECT pg_try_advisory_lock(:classid, :objid)"), {"classid": classid, "objid": objid}
            ).scalar()
        )
        yield acquired
    finally:
        if acquired:
            try:
                conn.execute(text("SELECT pg_advisory_unlock(:classid, :objid)"), {"classid": classid, "objid": objid})
            except Exception:  # noqa: BLE001
                # unlock 실패 시 이 커넥션을 풀에 돌려보내면 세션 잠금이 살아 있는 채로 재사용돼 잠금이
                # 샌다 - 물리 연결을 폐기(invalidate)해 DB가 세션 잠금을 해제하게 한다.
                logger.warning("CS 동기화 advisory unlock에 실패했습니다 - 커넥션을 폐기해 잠금을 해제합니다.")
                conn.invalidate()
        conn.close()


def is_cs_sync_lock_held(platform_id: int, source: str, engine: Union[Engine, Connection]) -> bool:
    """잠금을 실제로 잡아 보지 않고 "지금 누가 들고 있는가"만 확인한다(잡았다면 즉시 놓는다).
    stale 작업 정리가 "실제 활성 실행"을 건드리지 않도록 판단하는 용도다."""
    with cs_sync_source_lock(platform_id, source, engine) as acquired:
        return not acquired
