"""
tests/unit/test_advisory_lock_registry.py
---------------------------------------------
core/advisory_locks.py - advisory lock 네임스페이스 registry의 구조적 불변식.

"현재 쓰는 target_type 몇 개와 안 겹친다"는 표본 확인이 아니라, 어떤 target_type·자원 id가 와도 기능 영역 사이
키가 같아질 수 없다는 구조를 검증한다(classid 예약 범위가 서로소이고, 외부 명령 영역의 classid는 입력과 무관한
고정값이다). 실제 PostgreSQL 경합은 tests/integration/test_cs_sync_lock_pg.py가 검증한다.
"""

import random
import re
import string
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.advisory_locks import (
    BACKUP_CLASSID,
    CS_SYNC_CLASSID_BASE,
    CS_SYNC_CLASSID_COUNT,
    EXTERNAL_COMMAND_CLASSID,
    INT4_MAX,
    RESERVED_CLASSID_RANGES,
    backup_lock_key,
    cs_sync_lock_key,
    external_command_lock_key,
)
from repositories.integration_sync_repository import ExternalCommandRepository

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# 이 코드베이스가 쓰는 target_type(감사 시점). 아래 구조 테스트는 이 목록과 무관하게 성립한다 - 이 목록은
# "실제 값에서도" 같은 자원은 같은 키, 다른 target_type은 다른 키임을 추가로 확인할 뿐이다.
KNOWN_TARGET_TYPES = [
    "CS_CASE",
    "FULFILLMENT_ORDER_ITEM",
    "FULFILLMENT_INVENTORY",
    "ORDER",
    "PRODUCT_OPTION_PUBLISH_DRAFT",
    "PRODUCT_PLATFORM_MAP",
    "PRODUCT_PUBLISH_DRAFT",
    "SHIPMENT",
]


class TestReservedRanges:
    def test_every_reserved_range_is_valid_signed_int4(self):
        for name, (lo, hi) in RESERVED_CLASSID_RANGES.items():
            assert 0 <= lo <= hi <= INT4_MAX, name

    def test_reserved_ranges_are_pairwise_disjoint(self):
        items = sorted(RESERVED_CLASSID_RANGES.items(), key=lambda kv: kv[1][0])
        for (name_a, (_, hi_a)), (name_b, (lo_b, _)) in zip(items, items[1:], strict=False):
            assert hi_a < lo_b, f"{name_a}와 {name_b}의 classid 범위가 겹칩니다."

    def test_each_domain_constant_lies_inside_its_own_range(self):
        assert RESERVED_CLASSID_RANGES["BACKUP"] == (BACKUP_CLASSID, BACKUP_CLASSID)
        assert RESERVED_CLASSID_RANGES["EXTERNAL_COMMAND"] == (EXTERNAL_COMMAND_CLASSID, EXTERNAL_COMMAND_CLASSID)
        assert RESERVED_CLASSID_RANGES["CS_SYNC"] == (
            CS_SYNC_CLASSID_BASE,
            CS_SYNC_CLASSID_BASE + CS_SYNC_CLASSID_COUNT - 1,
        )


def _domain_of(classid: int) -> str | None:
    for name, (lo, hi) in RESERVED_CLASSID_RANGES.items():
        if lo <= classid <= hi:
            return name
    return None


class TestEveryKeyStaysInsideItsOwnDomain:
    def test_backup_key(self):
        classid, objid = backup_lock_key()
        assert _domain_of(classid) == "BACKUP" and 0 <= objid <= INT4_MAX

    def test_cs_sync_keys_cover_the_whole_int4_platform_range_inside_the_cs_range(self):
        for platform_id in (0, 1, 2**30, INT4_MAX - 1, INT4_MAX):
            for source_index in (0, 1, CS_SYNC_CLASSID_COUNT - 1):
                classid, objid = cs_sync_lock_key(platform_id, source_index)
                assert _domain_of(classid) == "CS_SYNC"
                assert objid == platform_id  # 해시 없음 - (source, platform)에서 키를 복원할 수 있다(단사)

    def test_cs_sync_rejects_out_of_range_inputs(self):
        for platform_id, source_index in ((-1, 0), (2**31, 0), (1, -1), (1, CS_SYNC_CLASSID_COUNT)):
            with pytest.raises(ValueError):
                cs_sync_lock_key(platform_id, source_index)

    def test_external_command_classid_is_the_fixed_reserved_value_for_any_target_type(self):
        """새로운(미래의) 임의 target_type이 어떤 값이든 classid는 외부 명령 전용 고정값이다 - 다른 영역의
        classid(백업, CS 동기화, 이후 추가될 영역)와 같아질 수 없다."""
        rng = random.Random(20261007)
        alphabet = string.printable + "가나다라마바사아자차카타파하🙂"
        types = [KNOWN_TARGET_TYPES[0], "x", "A" * 500, "한글_대상", "CS", "BKUP", "\x00", "0x43530000", "0x424B5550"]
        types += ["".join(rng.choice(alphabet) for _ in range(rng.randint(1, 40))) for _ in range(3000)]
        for target_type in types:
            for resource_id in (0, 1, 7, 2**31, -5, 2**63 - 1):
                classid, objid = external_command_lock_key(target_type, resource_id)
                assert classid == EXTERNAL_COMMAND_CLASSID
                assert _domain_of(classid) == "EXTERNAL_COMMAND"
                assert 0 <= objid <= INT4_MAX

    def test_external_command_key_is_never_equal_to_a_cs_or_backup_key_even_with_equal_objid(self):
        """objid가 같아도 classid가 다르면 PostgreSQL에서 다른 잠금이다."""
        _, ext_objid = external_command_lock_key("PRODUCT_PLATFORM_MAP", 42)
        ext = external_command_lock_key("PRODUCT_PLATFORM_MAP", 42)
        cs_same_objid = cs_sync_lock_key(ext_objid, 0)
        assert cs_same_objid[1] == ext[1] and cs_same_objid[0] != ext[0]
        assert backup_lock_key()[0] != ext[0]

    def test_empty_or_non_string_target_type_is_rejected(self):
        for bad in ("", None, 5):
            with pytest.raises(ValueError):
                external_command_lock_key(bad, 1)  # type: ignore[arg-type]


class TestExternalCommandObjidPolicy:
    def test_same_resource_always_gets_the_same_key(self):
        assert external_command_lock_key("SHIPMENT", 10) == external_command_lock_key("SHIPMENT", 10)

    def test_key_does_not_depend_on_process_hash_seed(self):
        """결정적 해시(blake2b)라 프로세스/호스트가 달라도 같은 값이다 - 값을 고정해 두어 PYTHONHASHSEED 같은
        런타임 요인에 의존하는 구현(내장 hash())로 바뀌면 실패한다. 서로 다른 api/scheduler 프로세스가 같은
        자원에 같은 잠금을 잡아야 한다."""
        assert external_command_lock_key("SHIPMENT", 5) == (EXTERNAL_COMMAND_CLASSID, 1843433029)

    def test_known_target_types_get_distinct_keys_for_the_same_id_and_distinct_ids_get_distinct_keys(self):
        seen: dict[tuple[int, int], tuple[str, int]] = {}
        for target_type in KNOWN_TARGET_TYPES:
            for resource_id in range(1, 201):
                key = external_command_lock_key(target_type, resource_id)
                assert key not in seen, f"{(target_type, resource_id)}와 {seen[key]}의 키가 충돌합니다."
                seen[key] = (target_type, resource_id)

    def test_int_like_resource_id_is_normalised(self):
        assert external_command_lock_key("ORDER", True) == external_command_lock_key("ORDER", 1)


def _fake_postgres_session():
    session = MagicMock()
    session.get_bind.return_value = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    return session


class TestRepositoryUsesTheRegistryKey:
    def test_acquire_target_lock_executes_the_registry_key_with_the_smallest_id(self):
        session = _fake_postgres_session()
        ExternalCommandRepository(session).acquire_target_lock("PRODUCT_PLATFORM_MAP", [9, 3, 5])
        stmt, params = session.execute.call_args.args
        assert "pg_advisory_xact_lock" in str(stmt)
        expected = external_command_lock_key("PRODUCT_PLATFORM_MAP", 3)
        assert (params["classid"], params["objid"]) == expected

    def test_sibling_mappings_starting_from_any_member_lock_the_same_key(self):
        keys = set()
        for ids in ([3, 5, 9], [9, 5, 3], [5, 3, 9]):
            session = _fake_postgres_session()
            ExternalCommandRepository(session).acquire_target_lock("PRODUCT_PLATFORM_MAP", ids)
            _, params = session.execute.call_args.args
            keys.add((params["classid"], params["objid"]))
        assert len(keys) == 1

    def test_scalar_id_and_empty_ids_and_non_postgres_dialect(self):
        session = _fake_postgres_session()
        ExternalCommandRepository(session).acquire_target_lock("ORDER", 11)
        assert session.execute.call_args.args[1]["objid"] == external_command_lock_key("ORDER", 11)[1]

        empty = _fake_postgres_session()
        ExternalCommandRepository(empty).acquire_target_lock("ORDER", [])
        empty.execute.assert_not_called()

        sqlite = MagicMock()
        sqlite.get_bind.return_value = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
        ExternalCommandRepository(sqlite).acquire_target_lock("ORDER", 1)
        sqlite.execute.assert_not_called()


SOURCE_DIRS = ["api", "config", "core", "integrations", "models", "repositories", "scheduler", "scripts", "services"]
LOCK_SQL = re.compile(r"pg_(?:try_)?advisory_(?:xact_)?(?:lock|unlock)")
# advisory lock SQL을 실제로 실행하는 모듈 - 새 사용처가 생기면 registry를 쓰도록 이 목록에 추가해 검토한다.
LOCK_SQL_ALLOWLIST = {
    "repositories/integration_sync_repository.py": "external_command_lock_key",
    "services/cs_sync_lock.py": "cs_sync_lock_key",
    "services/postgres_backup_service.py": "backup_lock_key",
}


def _python_sources():
    for directory in SOURCE_DIRS:
        for path in (REPO_ROOT / directory).rglob("*.py"):
            if "venv" in path.parts or "__pycache__" in path.parts:
                continue
            yield path


class TestNoLockKeyIsBuiltOutsideTheRegistry:
    def test_only_the_known_modules_execute_advisory_lock_sql_and_each_imports_its_registry_function(self):
        executing: dict[str, str] = {}
        for path in _python_sources():
            text = path.read_text(encoding="utf-8")
            # 문서/주석에서 함수 이름을 언급하는 경우와 구분하기 위해 SQL 문자열(text("SELECT ...")) 호출만 센다.
            if re.search(r'text\(\s*"[^"]*' + LOCK_SQL.pattern, text):
                executing[path.relative_to(REPO_ROOT).as_posix()] = text
        assert set(executing) == set(LOCK_SQL_ALLOWLIST)
        for rel, text in executing.items():
            assert "from core.advisory_locks import" in text and LOCK_SQL_ALLOWLIST[rel] in text, rel

    def test_no_lock_module_derives_a_classid_from_a_string_hash_or_a_numeric_literal(self):
        for rel in LOCK_SQL_ALLOWLIST:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            assert "crc32" not in text, rel
            assert not re.search(r"classid\s*=\s*0x", text), rel
            assert "_ADVISORY_CLASSID_BASE" not in text, rel


class TestModulesUseTheRegistryValues:
    def test_backup_service_constants_are_the_registry_backup_key(self):
        from services import postgres_backup_service as svc

        assert backup_lock_key() == (svc._ADVISORY_LOCK_CLASSID, svc._ADVISORY_LOCK_OBJID)

    def test_cs_sync_lock_keys_come_from_the_registry_cs_domain(self):
        from services.cs_sync_lock import _SOURCE_INDEX, lock_key

        for source, index in _SOURCE_INDEX.items():
            assert lock_key(9, source) == cs_sync_lock_key(9, index)
