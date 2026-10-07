"""
tests/unit/test_cs_sync_lock.py
-----------------------------------
services.cs_sync_lock의 생명주기·키 설계 검증. PostgreSQL 쪽 분기는 가짜 엔진(MagicMock)으로 "어떤 SQL을
어떤 순서로 실행하고 실패 시 커넥션을 어떻게 처리하는가"만 검증한다(실제 두 커넥션 경합은
tests/integration/test_cs_sync_lock_pg.py가 격리 PostgreSQL로 검증한다). 합성 데이터만 사용한다.
"""

from contextlib import ExitStack
from unittest.mock import MagicMock

import pytest

from services.cs_inquiry_catchup_service import CHECKPOINT_SOURCE_CODES, SOURCES
from services.cs_sync_lock import _SOURCE_INDEX, cs_sync_source_lock, is_cs_sync_lock_held, lock_key

CC = "COUPANG_CALL_CENTER"
PR = "COUPANG_PRODUCT_INQUIRY"


def _fake_pg_engine(*, lock_result: bool = True, unlock_error: Exception | None = None):
    """PostgreSQL처럼 보이는 가짜 엔진. execute 호출 기록과 invalidate/close 호출을 확인할 수 있다."""
    engine = MagicMock()
    engine.dialect.name = "postgresql"
    engine.engine = engine
    conn = MagicMock()
    conn.execution_options.return_value = conn
    calls: list[str] = []

    def _execute(statement, params=None):
        sql = str(statement)
        calls.append(sql)
        if "pg_advisory_unlock" in sql and unlock_error is not None:
            raise unlock_error
        result = MagicMock()
        result.scalar.return_value = lock_result
        return result

    conn.execute.side_effect = _execute
    engine.connect.return_value = conn
    return engine, conn, calls


class TestLockKeys:
    def test_keys_are_unique_for_every_platform_and_source_in_a_realistic_range(self):
        seen: dict[tuple[int, int], tuple[int, str]] = {}
        for platform_id in range(0, 5001):
            for source in (CC, PR):
                key = lock_key(platform_id, source)
                assert key not in seen, (platform_id, source, seen[key])
                seen[key] = (platform_id, source)

    def test_every_integer_platform_id_maps_without_overflow_or_collision(self):
        max_id = 2**31 - 1
        keys = {lock_key(pid, s) for pid in (0, 1, 16, 17, 134_217_727, 134_217_728, max_id) for s in (CC, PR)}
        assert len(keys) == 14  # 이전 설계(platform_id*16+idx)는 134,217,728부터 int4 범위를 넘었다
        for classid, objid in keys:
            assert 0 <= classid < 2**31 and 0 <= objid < 2**31

    def test_key_is_stable_and_does_not_collide_with_the_backup_lock_namespace(self):
        assert lock_key(7, CC) == lock_key(7, CC)
        backup_classid = 0x424B5550
        assert all(lock_key(7, s)[0] != backup_classid for s in (CC, PR))

    def test_out_of_range_platform_or_unknown_source_is_rejected(self):
        with pytest.raises(ValueError):
            lock_key(2**31, CC)
        with pytest.raises(ValueError):
            lock_key(-1, CC)
        with pytest.raises(ValueError):
            lock_key(1, "NOT_A_SOURCE")


class TestKeyConsistencyAndNamespaces:
    def test_lock_source_numbers_and_checkpoint_codes_cover_exactly_the_same_sources(self):
        assert set(_SOURCE_INDEX) == set(SOURCES) == set(CHECKPOINT_SOURCE_CODES)
        assert len(set(_SOURCE_INDEX.values())) == len(_SOURCE_INDEX)  # 번호 단사

    def test_lock_classids_never_equal_the_other_advisory_lock_namespaces_in_use(self):
        """repositories/integration_sync_repository.acquire_target_lock은 classid=crc32(target_type)&0x7FFFFFFF를
        쓴다(해시라 구조적 분리는 불가능) - 현재 코드베이스가 쓰는 target_type 전부와 백업 잠금 classid가 이 기능의
        classid와 다름을 감사 시점 값으로 고정한다. 새 target_type이 생기면 이 목록에 추가해 확인한다."""
        import zlib

        known_target_types = [
            "CS_CASE",
            "FULFILLMENT_ORDER_ITEM",
            "FULFILLMENT_INVENTORY",
            "ORDER",
            "PRODUCT_OPTION_PUBLISH_DRAFT",
            "PRODUCT_PLATFORM_MAP",
            "PRODUCT_PUBLISH_DRAFT",
            "SHIPMENT",
        ]
        others = {zlib.crc32(t.encode("utf-8")) & 0x7FFFFFFF for t in known_target_types} | {0x424B5550}
        mine = {lock_key(1, s)[0] for s in SOURCES}
        assert mine.isdisjoint(others)

    def test_key_is_injective_in_both_components(self):
        """(classid, objid) -> (source, platform_id)를 복원할 수 있으면 서로 다른 입력이 같은 키를 가질 수 없다."""
        for source in SOURCES:
            for platform_id in (0, 1, 2**30, 2**31 - 1):
                classid, objid = lock_key(platform_id, source)
                assert objid == platform_id
                assert classid - lock_key(0, source)[0] == 0
        assert len({lock_key(5, s)[0] for s in SOURCES}) == len(SOURCES)


class TestLocalFallbackLifecycle:
    def test_is_exclusive_then_released_repeatedly_without_leaking(self, engine):
        for _ in range(3):
            with cs_sync_source_lock(1, CC, engine) as first:
                assert first is True
                with cs_sync_source_lock(1, CC, engine) as second:
                    assert second is False
            with cs_sync_source_lock(1, CC, engine) as after:
                assert after is True

    def test_released_when_the_body_raises(self, engine):
        with pytest.raises(RuntimeError), cs_sync_source_lock(2, CC, engine) as acquired:
            assert acquired is True
            raise RuntimeError("synthetic")
        with cs_sync_source_lock(2, CC, engine) as after:
            assert after is True

    def test_released_on_base_exception_such_as_cancellation_or_exit(self, engine):
        with pytest.raises(KeyboardInterrupt), cs_sync_source_lock(3, PR, engine):
            raise KeyboardInterrupt
        with cs_sync_source_lock(3, PR, engine) as after:
            assert after is True

    def test_is_held_reports_true_only_while_someone_holds_it_and_leaves_no_residue(self, engine):
        assert is_cs_sync_lock_held(4, CC, engine) is False
        with cs_sync_source_lock(4, CC, engine):
            assert is_cs_sync_lock_held(4, CC, engine) is True
        assert is_cs_sync_lock_held(4, CC, engine) is False

    def test_failed_second_acquisition_releases_the_first_when_stacked(self, engine):
        """전체-source 수동 sync처럼 두 잠금을 순서대로 잡다가 두 번째가 실패해도 첫 번째는 해제된다."""
        # 다른 실행이 PR 잠금을 들고 있다
        with cs_sync_source_lock(5, PR, engine), ExitStack() as stack:
            acquired = [stack.enter_context(cs_sync_source_lock(5, s, engine)) for s in (CC, PR)]
            assert acquired == [True, False]
        with cs_sync_source_lock(5, CC, engine) as cc_free:
            assert cc_free is True  # 스택이 닫히며 첫 번째(CC) 잠금도 해제됐다
        with cs_sync_source_lock(5, PR, engine) as pr_free:
            assert pr_free is True

    def test_fixed_acquisition_order_never_blocks_so_there_is_no_deadlock(self, engine):
        """두 실행이 같은 두 잠금을 서로 반대로 쥐려 해도 논블로킹이라 영원히 기다리지 않는다."""
        with (
            cs_sync_source_lock(6, CC, engine) as a_cc,
            cs_sync_source_lock(6, PR, engine) as b_pr,
            cs_sync_source_lock(6, PR, engine) as a_pr_attempt,
            cs_sync_source_lock(6, CC, engine) as b_cc_attempt,
        ):
            assert (a_cc, b_pr, a_pr_attempt, b_cc_attempt) == (True, True, False, False)


class TestPostgresPathLifecycle:
    def test_normal_path_tries_lock_then_unlocks_then_closes_without_invalidating(self):
        engine, conn, calls = _fake_pg_engine()
        with cs_sync_source_lock(1, CC, engine) as acquired:
            assert acquired is True
        assert any("pg_try_advisory_lock" in c for c in calls)
        assert any("pg_advisory_unlock" in c for c in calls)
        conn.invalidate.assert_not_called()
        conn.close.assert_called_once()

    def test_not_acquired_does_not_unlock_but_still_closes(self):
        engine, conn, calls = _fake_pg_engine(lock_result=False)
        with cs_sync_source_lock(1, CC, engine) as acquired:
            assert acquired is False
        assert not any("pg_advisory_unlock" in c for c in calls)
        conn.close.assert_called_once()

    def test_exception_in_body_still_unlocks_and_closes(self):
        engine, conn, calls = _fake_pg_engine()
        with pytest.raises(RuntimeError), cs_sync_source_lock(1, CC, engine):
            raise RuntimeError("synthetic")
        assert any("pg_advisory_unlock" in c for c in calls)
        conn.close.assert_called_once()

    def test_unlock_failure_discards_the_connection_instead_of_returning_it_to_the_pool(self):
        """unlock이 실패한 커넥션을 풀에 돌려보내면 세션 잠금이 살아 있는 채 재사용돼 잠금이 샌다."""
        engine, conn, _ = _fake_pg_engine(unlock_error=RuntimeError("connection lost"))
        with cs_sync_source_lock(1, CC, engine) as acquired:
            assert acquired is True
        conn.invalidate.assert_called_once()
        conn.close.assert_called_once()

    def test_connection_is_opened_with_autocommit_isolation(self):
        engine, conn, _ = _fake_pg_engine()
        with cs_sync_source_lock(1, CC, engine):
            pass
        conn.execution_options.assert_called_once_with(isolation_level="AUTOCOMMIT")
