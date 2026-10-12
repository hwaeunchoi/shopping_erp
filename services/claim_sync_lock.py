"""
services/claim_sync_lock.py
-------------------------------
클레임(취소·반품·교환) 수집의 동시 실행 방지 - 플랫폼별 PostgreSQL 세션 advisory 잠금.

scheduler의 claim_sync job(20분 주기)과 수동 POST /api/orders/sync-claims가 같은 (platform_id, CLAIM_SYNC) 잠금을
공유한다. 겹치면 하나만 외부 호출·DB 쓰기를 하고 패자는 대기·재시도 없이 ALREADY_RUNNING으로 끝난다 - 그래서 같은 기간을
두 번 조회하는 중복 요청, 요청 예산 2배 소비, 취소 후보 cursor 이중 처리가 생기지 않는다.

잠금 키는 core/advisory_locks.py registry의 claim_sync_lock_key()가 만든다(고정 classid + platform_id). 구현(전용
AUTOCOMMIT 커넥션, unlock 실패 시 커넥션 폐기, SQLite 대체 잠금의 한계)은 services/cs_sync_lock.session_advisory_lock과
같다 - SQLite 대체 잠금은 같은 프로세스 안에서만 유효하고 프로세스 간 보호는 PostgreSQL에서만 보장된다.
"""

from contextlib import contextmanager
from typing import Iterator, Union

from sqlalchemy.engine import Connection, Engine

from core.advisory_locks import claim_sync_lock_key
from services.cs_sync_lock import session_advisory_lock


@contextmanager
def claim_sync_platform_lock(platform_id: int, engine: Union[Engine, Connection]) -> Iterator[bool]:
    """플랫폼 1개의 클레임 수집 잠금을 논블로킹으로 시도한다. acquired가 True일 때만 수집을 진행한다."""
    classid, objid = claim_sync_lock_key(platform_id)
    with session_advisory_lock(classid, objid, engine) as acquired:
        yield acquired
