"""
tests/integration/_product_sync_concurrency_runner.py
------------------------------------------------------------------
실제(격리) PostgreSQL 위에서, 서로 다른 DB 커넥션/세션을 쓰는 두 "worker" 스레드가
ProductSyncDispatchService.execute_command()를 barrier/event로 겹치게 실행했을 때
외부 대상 단위 배타 실행이 실제로 보장되는지 검증한다. 채널 호출은 스텁으로
대체한다(실제 네이버/쿠팡 API를 호출하지 않음).

tests/integration/test_product_sync_concurrency_pg.py가 격리된 1회성 docker
postgres 컨테이너를 띄우고, 이 스크립트를 그 postgres와 같은(--internal) 네트워크에
붙은 러너 컨테이너 안에서 실행한다(레포를 읽기전용으로 마운트) - 실제 회전 로직을
검증하는 tests/integration/_pg_rotation_runner.py와 동일한 격리 패턴.

DB 스키마는 Alembic 마이그레이션이 아니라 Base.metadata.create_all()로 직접
만든다 - 이 테스트는 스키마 마이그레이션 경로가 아니라 런타임 동시성 동작만
검증하므로, 매번 새로 뜨는 1회성 스크래치 DB에 현재 모델 그대로 스키마를 만드는
편이 더 정확하고 빠르다.

시나리오(PGCONC_SCENARIO 환경변수로 선택):
  - sibling_inventory: 같은 네이버 원상품(platform_origin_product_id)을 공유하는
    서로 다른 두 매핑(형제 매핑)에 대한 재고 명령 두 개.
  - cross_type_same_target: 같은 매핑(=같은 외부 대상)에 대한 재고 명령과
    판매상태 명령.
  - unrelated_targets_not_serialized: 서로 무관한 두 매핑에 대한 재고 명령 두 개 -
    한쪽이 채널 호출 도중 멈춰 있어도 다른 쪽은 기다리지 않아야 한다.
  - predecessor_unknown_blocks_successor: 선행 명령이 UNKNOWN으로 끝나면, 같은
    대상의 후속(더 최신) 명령은 채널을 호출하지 않고 PENDING을 유지해야 한다.

각 시나리오는 결과를 JSON 한 줄로 표준출력 마지막 줄에 출력한다.
"""

import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

db_host = os.environ["PGCONC_DB_HOST"]
db_user = os.environ["PGCONC_DB_USER"]
db_password = os.environ["PGCONC_DB_PASSWORD"]
db_name = os.environ["PGCONC_DB_NAME"]
scenario = os.environ["PGCONC_SCENARIO"]

os.environ["DATABASE_URL"] = f"postgresql+psycopg://{db_user}:{db_password}@{db_host}:5432/{db_name}"

from config.settings import settings  # noqa: E402
from core.database import SessionLocal, engine  # noqa: E402
from integrations.malls.base_mall_connector import SALE_STATUS_ON_SALE, ProductSyncActionResult  # noqa: E402
from integrations.malls.errors import MarketplaceExternalAPIError  # noqa: E402
from models import Base  # noqa: E402
from models.integration_sync import ExternalCommand, ProductSyncCommandDetail  # noqa: E402
from models.platform import Platform  # noqa: E402
from models.product import Product, ProductOption, ProductPlatformMap  # noqa: E402
from repositories.integration_sync_repository import ExternalCommandRepository  # noqa: E402
from services.product_sync_dispatch_service import (  # noqa: E402
    INVENTORY_UPDATE,
    TARGET_TYPE,
    ProductSyncAlreadyRunningError,
    ProductSyncDispatchService,
)

settings.product_channel_sync_enabled = True

Base.metadata.create_all(engine)


class ConcurrencyStubConnector:
    """update_inventory()/update_sale_status()를 실제로 호출하지 않고, 호출
    시점(시작/끝)과 동시 진입 횟수만 스레드 안전하게 기록한다. gate에 등록된
    (kind, platform_option_id)로 불리면, 그 호출은 started 이벤트를 세팅한 뒤
    release 이벤트가 set되거나 timeout까지 실제로 블록한다(barrier 역할) -
    "선행 명령의 외부 호출을 대기시킨 상태에서 후속 worker 실행"을 재현한다."""

    supports_inventory_update = True
    supports_sale_status_update = True

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.call_log: list[dict] = []
        self.gate: dict[tuple, dict] = {}
        self.fail_for: dict[tuple, Exception] = {}

    def _enter(self, kind: str, platform_option_id: str) -> float:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        key = (kind, platform_option_id)
        gate = self.gate.get(key)
        start_ts = time.monotonic()
        if gate is not None:
            gate["started_event"].set()
            gate["release_event"].wait(timeout=gate.get("timeout", 10))
        return start_ts

    def _exit(self, kind: str, platform_option_id: str, start_ts: float) -> None:
        end_ts = time.monotonic()
        with self._lock:
            self.active -= 1
            self.call_log.append(
                {
                    "kind": kind,
                    "id": platform_option_id,
                    "start": start_ts,
                    "end": end_ts,
                    "thread": threading.current_thread().name,
                }
            )

    def update_inventory(self, platform_option_id, quantity, platform_origin_product_id=None):
        start_ts = self._enter("inventory", platform_option_id)
        try:
            key = ("inventory", platform_option_id)
            if key in self.fail_for:
                raise self.fail_for[key]
            return ProductSyncActionResult(accepted=True, platform_result_code="SUCCESS")
        finally:
            self._exit("inventory", platform_option_id, start_ts)

    def update_sale_status(self, platform_option_id, target_status, platform_origin_product_id=None):
        start_ts = self._enter("status", platform_option_id)
        try:
            key = ("status", platform_option_id)
            if key in self.fail_for:
                raise self.fail_for[key]
            return ProductSyncActionResult(accepted=True, platform_result_code="SUCCESS")
        finally:
            self._exit("status", platform_option_id, start_ts)


def _seed_mapping(session, *, platform_id, sku, platform_option_id, platform_origin_product_id=None):
    product = Product(name=f"동시성테스트-{sku}", category="테스트", base_price=10000, status="ACTIVE")
    session.add(product)
    session.flush()
    option = ProductOption(product_id=product.id, sku_code=sku, is_active=True)
    session.add(option)
    session.flush()
    mapping = ProductPlatformMap(
        product_option_id=option.id,
        platform_id=platform_id,
        platform_option_id=platform_option_id,
        platform_origin_product_id=platform_origin_product_id,
    )
    session.add(mapping)
    session.flush()
    return mapping


def _seed_pending_inventory_command(session, *, mapping, platform, target_quantity: int) -> int:
    """enqueue_inventory_update()를 거치지 않고 PENDING ExternalCommand를 직접
    만든다 - 그 메서드는 "같은 외부 대상·같은 명령종류의 아직 실행 전 명령"을
    의도적으로 CANCELLED 처리하므로(형제 매핑이 같은 외부 대상을 공유하면 서로를
    취소한다 - 이미 단위테스트로 검증된 별개의 정책), 이 테스트가 검증하려는
    "같은 외부 대상을 가리키는 서로 다른 명령 행 두 개가 동시에 실행 후보로 남아
    있는 상황"을 enqueue 경로로는 재현할 수 없다. execute_command()의 실행 시점
    배타성만 분리해서 검증하기 위해 그 상태를 직접 구성한다."""
    command = ExternalCommand(
        idempotency_key=f"TEST:{uuid.uuid4().hex}",
        command_type=INVENTORY_UPDATE,
        platform_id=platform.id,
        platform_code=platform.code,
        target_type=TARGET_TYPE,
        target_id=mapping.id,
        status="PENDING",
        trace_id=uuid.uuid4().hex,
    )
    session.add(command)
    session.flush()
    session.add(
        ProductSyncCommandDetail(
            command_id=command.id, product_platform_map_id=mapping.id, target_quantity=target_quantity
        )
    )
    session.flush()
    return command.id


def _run_worker(command_id: int, connector, out: dict, key: str) -> None:
    session = SessionLocal()
    try:
        svc = ProductSyncDispatchService(session, connector_factory=lambda *a, **k: connector)
        try:
            outcome = svc.execute_command(command_id)
            session.commit()
            out[key] = {"status": outcome.command.status, "already_processed": outcome.already_processed, "error": None}
        except ProductSyncAlreadyRunningError as e:
            session.rollback()
            out[key] = {"status": "ALREADY_RUNNING", "already_processed": None, "error": str(e)}
        except Exception as e:  # noqa: BLE001 - 실행 결과(FAILED/RETRY_WAIT/UNKNOWN)는 이미 세션에 반영돼 있다.
            session.commit()
            cmd = ExternalCommandRepository(session).get_by_id(command_id)
            out[key] = {
                "status": cmd.status if cmd else None,
                "already_processed": None,
                "error": f"{type(e).__name__}: {e}",
            }
    finally:
        session.close()


def scenario_sibling_inventory() -> dict:
    setup = SessionLocal()
    platform = Platform(
        code="naver_smartstore",
        name="네이버",
        connector_class="NaverSmartstoreConnector",
        settlement_cycle_days=15,
        is_active=True,
    )
    setup.add(platform)
    setup.flush()
    origin_id = f"ORIGIN-SIB-{uuid.uuid4().hex[:8]}"
    map_a = _seed_mapping(
        setup,
        platform_id=platform.id,
        sku=f"SIB-A-{uuid.uuid4().hex[:6]}",
        platform_option_id=f"SIB-EXT-A-{uuid.uuid4().hex[:6]}",
        platform_origin_product_id=origin_id,
    )
    map_b = _seed_mapping(
        setup,
        platform_id=platform.id,
        sku=f"SIB-B-{uuid.uuid4().hex[:6]}",
        platform_option_id=f"SIB-EXT-B-{uuid.uuid4().hex[:6]}",
        platform_origin_product_id=origin_id,
    )
    # command_b는 아직 만들지 않는다 - command_a와 동시에 미리 만들어 두면
    # exists_newer_command_for_target()이 (실제 동시성과 무관하게, 항상) command_a를
    # "더 낡은 같은 종류의 명령"으로 판단해 결정론적으로 CANCELLED 처리한다(이미
    # 단위테스트로 검증된 별개의 정책 - services.product_sync_dispatch_service의
    # "오래된 명령이 최신 목표값을 덮어쓰지 않도록" 참고). 이 테스트가 검증하려는
    # 것은 그 정책이 아니라 "형제 매핑의 서로 다른 명령 행이 실제로 동시에 채널을
    # 호출하지 않는가"이므로, command_a가 이미 실행(채널 호출 도중) 중일 때 뒤늦게
    # command_b가 생겨 그 뒤를 잇는 실제 운영 순서를 그대로 재현한다.
    command_a_id = _seed_pending_inventory_command(setup, mapping=map_a, platform=platform, target_quantity=11)
    setup.commit()
    platform_option_a = map_a.platform_option_id
    setup.close()

    connector = ConcurrencyStubConnector()
    started_a = threading.Event()
    release_a = threading.Event()
    connector.gate[("inventory", platform_option_a)] = {
        "started_event": started_a,
        "release_event": release_a,
        "timeout": 10,
    }

    out: dict = {}
    t1 = threading.Thread(target=_run_worker, args=(command_a_id, connector, out, "a"), name="worker-A")
    t1.start()
    started_ok = started_a.wait(timeout=5)

    seed2 = SessionLocal()
    command_b_id = _seed_pending_inventory_command(seed2, mapping=map_b, platform=platform, target_quantity=22)
    seed2.commit()
    seed2.close()

    t2 = threading.Thread(target=_run_worker, args=(command_b_id, connector, out, "b"), name="worker-B")
    t2.start()
    # worker-B는 형제 매핑(같은 외부 대상)이라 advisory lock에서 대기해야 한다 -
    # worker-A가 아직 release되지 않은 시점에 worker-B가 벌써 끝나 있으면 직렬화 실패.
    t2.join(timeout=1.5)
    b_finished_before_a_released = not t2.is_alive()
    release_a.set()
    t1.join(timeout=10)
    t2.join(timeout=10)

    return {
        "scenario": "sibling_inventory",
        "worker_a_started": bool(started_ok),
        "b_finished_before_a_released": bool(b_finished_before_a_released),
        "max_active": connector.max_active,
        "call_log": connector.call_log,
        "a": out.get("a"),
        "b": out.get("b"),
    }


def scenario_cross_type_same_target() -> dict:
    setup = SessionLocal()
    platform = Platform(
        code="naver_smartstore",
        name="네이버",
        connector_class="NaverSmartstoreConnector",
        settlement_cycle_days=15,
        is_active=True,
    )
    setup.add(platform)
    setup.flush()
    mapping = _seed_mapping(
        setup,
        platform_id=platform.id,
        sku=f"CROSS-{uuid.uuid4().hex[:6]}",
        platform_option_id=f"CROSS-EXT-{uuid.uuid4().hex[:6]}",
    )
    svc = ProductSyncDispatchService(setup)
    outcome_inv = svc.enqueue_inventory_update(mapping.id, 7)
    outcome_status = svc.enqueue_sale_status_update(mapping.id, SALE_STATUS_ON_SALE)
    setup.commit()
    command_inv_id, command_status_id = outcome_inv.command.id, outcome_status.command.id
    platform_option_id = mapping.platform_option_id
    setup.close()

    connector = ConcurrencyStubConnector()
    started_inv = threading.Event()
    release_inv = threading.Event()
    connector.gate[("inventory", platform_option_id)] = {
        "started_event": started_inv,
        "release_event": release_inv,
        "timeout": 10,
    }

    out: dict = {}
    t1 = threading.Thread(target=_run_worker, args=(command_inv_id, connector, out, "inv"), name="worker-inv")
    t1.start()
    started_ok = started_inv.wait(timeout=5)

    t2 = threading.Thread(target=_run_worker, args=(command_status_id, connector, out, "status"), name="worker-status")
    t2.start()
    t2.join(timeout=1.5)
    status_finished_before_inv_released = not t2.is_alive()
    release_inv.set()
    t1.join(timeout=10)
    t2.join(timeout=10)

    return {
        "scenario": "cross_type_same_target",
        "worker_inv_started": bool(started_ok),
        "status_finished_before_inv_released": bool(status_finished_before_inv_released),
        "max_active": connector.max_active,
        "call_log": connector.call_log,
        "inv": out.get("inv"),
        "status": out.get("status"),
    }


def scenario_unrelated_targets_not_serialized() -> dict:
    setup = SessionLocal()
    platform = Platform(
        code="coupang", name="쿠팡", connector_class="CoupangConnector", settlement_cycle_days=15, is_active=True
    )
    setup.add(platform)
    setup.flush()
    map_x = _seed_mapping(
        setup,
        platform_id=platform.id,
        sku=f"UNREL-X-{uuid.uuid4().hex[:6]}",
        platform_option_id=f"UNREL-EXT-X-{uuid.uuid4().hex[:6]}",
    )
    map_y = _seed_mapping(
        setup,
        platform_id=platform.id,
        sku=f"UNREL-Y-{uuid.uuid4().hex[:6]}",
        platform_option_id=f"UNREL-EXT-Y-{uuid.uuid4().hex[:6]}",
    )
    svc = ProductSyncDispatchService(setup)
    outcome_x = svc.enqueue_inventory_update(map_x.id, 1)
    outcome_y = svc.enqueue_inventory_update(map_y.id, 2)
    setup.commit()
    command_x_id, command_y_id = outcome_x.command.id, outcome_y.command.id
    platform_option_x = map_x.platform_option_id
    setup.close()

    connector = ConcurrencyStubConnector()
    started_x = threading.Event()
    release_x = threading.Event()
    connector.gate[("inventory", platform_option_x)] = {
        "started_event": started_x,
        "release_event": release_x,
        "timeout": 10,
    }

    out: dict = {}
    t1 = threading.Thread(target=_run_worker, args=(command_x_id, connector, out, "x"), name="worker-X")
    t1.start()
    started_ok = started_x.wait(timeout=5)

    t2 = threading.Thread(target=_run_worker, args=(command_y_id, connector, out, "y"), name="worker-Y")
    t2.start()
    # 무관한 대상이므로 X가 release되기 훨씬 전에 Y가 끝나야 한다(전역 직렬화 없음).
    t2.join(timeout=3)
    y_alive_after_wait = t2.is_alive()

    release_x.set()
    t1.join(timeout=10)
    t2.join(timeout=10)

    return {
        "scenario": "unrelated_targets_not_serialized",
        "worker_x_started": bool(started_ok),
        "y_finished_without_waiting_for_x": bool(not y_alive_after_wait),
        "max_active": connector.max_active,
        "call_log": connector.call_log,
        "x": out.get("x"),
        "y": out.get("y"),
    }


def scenario_predecessor_unknown_blocks_successor() -> dict:
    setup = SessionLocal()
    platform = Platform(
        code="naver_smartstore",
        name="네이버",
        connector_class="NaverSmartstoreConnector",
        settlement_cycle_days=15,
        is_active=True,
    )
    setup.add(platform)
    setup.flush()
    mapping = _seed_mapping(
        setup,
        platform_id=platform.id,
        sku=f"UNK-{uuid.uuid4().hex[:6]}",
        platform_option_id=f"UNK-EXT-{uuid.uuid4().hex[:6]}",
    )
    svc = ProductSyncDispatchService(setup)
    outcome1 = svc.enqueue_inventory_update(mapping.id, 3)
    setup.commit()
    command1_id = outcome1.command.id
    platform_option_id = mapping.platform_option_id
    setup.close()

    connector = ConcurrencyStubConnector()
    connector.fail_for[("inventory", platform_option_id)] = MarketplaceExternalAPIError("naver", "TIMEOUT", True)

    out1: dict = {}
    _run_worker(command1_id, connector, out1, "first")

    # 첫 명령이 UNKNOWN으로 끝난 뒤, 같은 대상에 더 최신 목표값으로 두 번째 명령을 접수.
    setup2 = SessionLocal()
    svc2 = ProductSyncDispatchService(setup2)
    outcome2 = svc2.enqueue_inventory_update(mapping.id, 9)
    setup2.commit()
    command2_id = outcome2.command.id
    setup2.close()

    calls_before = len(connector.call_log)
    out2: dict = {}
    _run_worker(command2_id, connector, out2, "second")
    calls_after = len(connector.call_log)

    return {
        "scenario": "predecessor_unknown_blocks_successor",
        "first": out1.get("first"),
        "second": out2.get("second"),
        "connector_calls_before_second_attempt": calls_before,
        "connector_calls_after_second_attempt": calls_after,
    }


SCENARIOS = {
    "sibling_inventory": scenario_sibling_inventory,
    "cross_type_same_target": scenario_cross_type_same_target,
    "unrelated_targets_not_serialized": scenario_unrelated_targets_not_serialized,
    "predecessor_unknown_blocks_successor": scenario_predecessor_unknown_blocks_successor,
}


def main() -> None:
    fn = SCENARIOS.get(scenario)
    if fn is None:
        raise ValueError(f"알 수 없는 시나리오: {scenario}")
    result = fn()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
