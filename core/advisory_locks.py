"""
core/advisory_locks.py
--------------------------
PostgreSQL advisory lock 네임스페이스의 단일 registry.

advisory lock은 같은 DB 인스턴스 안에서 모든 기능이 하나의 키 공간(classid, objid)을 공유하고, 세션 수준
(pg_try_advisory_lock)과 트랜잭션 수준(pg_advisory_xact_lock)도 같은 키 공간이다. 그래서 기능 영역마다
classid를 이 모듈에 "예약"하고, 영역 안의 개별 자원은 objid로만 구분한다. 키를 만드는 코드는 전부 이 모듈의
함수/상수를 쓴다(다른 모듈에서 classid를 직접 계산하거나 숫자 리터럴로 만들지 않는다 - 테스트가 감시한다).

예약 영역(classid 범위, 모두 서로소이고 signed int4 안):
- 백업(BACKUP):                  0x424B5550 ("BKUP"), objid=0 하나 - 세션 수준
- CS 문의 동기화(CS_SYNC):        0x43530000 ~ 0x4353FFFF ("CS" + source 번호), objid=platform_id - 세션 수준
- 외부 명령 대상(EXTERNAL_COMMAND): 0x4558434D ("EXCM") 하나 고정, objid=hash(target_type, 자원 id) - 트랜잭션 수준

외부 명령 영역의 classid는 target_type 문자열에서 만들지 않는다(예전에는 crc32(target_type)을 classid로 써서
미래의 target_type이 다른 영역의 classid와 같아질 수 있었다). target_type과 자원 id는 objid 하나의 해시로
합친다. objid는 31비트라 서로 다른 (target_type, id)가 같은 objid를 가질 수 있다 - 이 경우 서로 상관없는 두
자원이 불필요하게 직렬화될 뿐(보수적 상호 배제)이고, 같은 자원이 다른 키를 얻는 일은 없다(결정적 해시, 프로세스·
호스트와 무관). 다른 기능 영역과는 classid가 달라 objid가 같아도 절대 충돌하지 않는다.
"""

import hashlib

INT4_MAX = 2**31 - 1

BACKUP_CLASSID = 0x424B5550
BACKUP_OBJID = 0

CS_SYNC_CLASSID_BASE = 0x43530000
CS_SYNC_CLASSID_COUNT = 0x10000  # source 번호 0 ~ 65535

EXTERNAL_COMMAND_CLASSID = 0x4558434D

# 이름 -> (classid 범위 시작, 끝; 양끝 포함). 새 기능 영역은 여기에 추가한다(서로소·int4 여부는 테스트가 검증).
RESERVED_CLASSID_RANGES: dict[str, tuple[int, int]] = {
    "BACKUP": (BACKUP_CLASSID, BACKUP_CLASSID),
    "CS_SYNC": (CS_SYNC_CLASSID_BASE, CS_SYNC_CLASSID_BASE + CS_SYNC_CLASSID_COUNT - 1),
    "EXTERNAL_COMMAND": (EXTERNAL_COMMAND_CLASSID, EXTERNAL_COMMAND_CLASSID),
}


def backup_lock_key() -> tuple[int, int]:
    return BACKUP_CLASSID, BACKUP_OBJID


def cs_sync_lock_key(platform_id: int, source_index: int) -> tuple[int, int]:
    """(platform_id, source 번호) -> (classid, objid). 두 값 모두 범위 검사 후 그대로 쓴다(해시 없음, 단사)."""
    if not 0 <= source_index < CS_SYNC_CLASSID_COUNT:
        raise ValueError("CS 동기화 source 번호가 예약된 classid 범위를 벗어났습니다.")
    if not 0 <= platform_id <= INT4_MAX:
        raise ValueError("platform_id가 잠금 키 범위(int4)를 벗어났습니다.")
    return CS_SYNC_CLASSID_BASE + source_index, platform_id


def external_command_lock_key(target_type: str, resource_id: int) -> tuple[int, int]:
    """외부 명령 대상 (target_type, 자원 id) -> (고정 classid, 31비트 결정적 해시 objid)."""
    if not isinstance(target_type, str) or not target_type:
        raise ValueError("target_type은 비어 있지 않은 문자열이어야 합니다.")
    digest = hashlib.blake2b(f"{target_type}\x00{int(resource_id)}".encode("utf-8"), digest_size=4).digest()
    return EXTERNAL_COMMAND_CLASSID, int.from_bytes(digest, "big") & INT4_MAX
