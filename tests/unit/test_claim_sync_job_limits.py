"""
tests/unit/test_claim_sync_job_limits.py
---------------------------------------------
claim_sync job의 호출량 계약: 플래그 OFF면 후보 조회 DB 쿼리·세션·HTTP가 0건, 주기는 설정(기본 20분)을 따르고,
켜져 있으면 목록 조회와 취소 단건 조회가 같은 요청 예산을 공유하며 설정의 조회기간·상한을 쓴다.
"""

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from config.settings import settings
from integrations.malls.errors import RequestBudget
from models.order import Order
from scheduler import scheduler as scheduler_module
from scheduler.jobs import claim_sync_job


class TestSchedulerInterval:
    def test_claim_sync_runs_every_twenty_minutes_by_default(self):
        job = {j.id: j for j in scheduler_module.build_scheduler().get_jobs()}["claim_sync"]
        assert job.trigger.interval == timedelta(minutes=20)
        assert job.max_instances == 1

    def test_interval_follows_the_setting(self, monkeypatch):
        monkeypatch.setattr(settings, "claim_sync_interval_minutes", 30)
        job = {j.id: j for j in scheduler_module.build_scheduler().get_jobs()}["claim_sync"]
        assert job.trigger.interval == timedelta(minutes=30)


class TestDisabledDoesNothing:
    def test_disabled_job_opens_no_session_and_runs_no_candidate_query(self, monkeypatch):
        monkeypatch.setattr(settings, "claim_sync_enabled", False)

        def _boom(*args, **kwargs):
            raise AssertionError("플래그가 꺼져 있는데 DB/커넥터가 사용되었습니다.")

        monkeypatch.setattr(claim_sync_job, "session_scope", _boom)
        monkeypatch.setattr(claim_sync_job, "get_mall_connector", _boom)
        monkeypatch.setattr("repositories.order_repository.OrderRepository.list_cancellation_lookup_candidates", _boom)

        assert claim_sync_job.run() == {"skipped_disabled": {"skipped": "disabled"}}


class _Connector:
    supports_cancellation_sync = False
    supports_return_sync = True
    supports_exchange_sync = True
    supports_cancellation_lookup_by_order = True

    def __init__(self):
        self.windows: list[tuple[str, date, date]] = []
        self.budgets: list[RequestBudget] = []
        self.lookup_calls = 0

    def estimate_claim_requests(self, feature, start, end, max_pages, max_retries):
        return None

    def fetch_returns(self, s, e, **kw):
        self.windows.append(("returns", s, e))
        self.budgets.append(kw["request_budget"])
        kw["request_budget"].consume("coupang")
        return []

    def fetch_exchanges(self, s, e, **kw):
        self.windows.append(("exchanges", s, e))
        self.budgets.append(kw["request_budget"])
        kw["request_budget"].consume("coupang")
        return []

    def fetch_cancellation_status(self, platform_order_no, since, **kw):
        self.lookup_calls += 1
        self.budgets.append(kw["request_budget"])
        kw["request_budget"].consume("coupang")
        return None


class TestEnabledJobUsesOneSharedBudget:
    def _run(self, monkeypatch, db_session, platform, connector):
        @contextmanager
        def _scope():
            yield db_session

        monkeypatch.setattr(settings, "claim_sync_enabled", True)
        monkeypatch.setattr(claim_sync_job, "session_scope", _scope)
        monkeypatch.setattr(claim_sync_job, "get_mall_connector", lambda *a, **k: connector)
        monkeypatch.setattr("repositories.platform_repository.PlatformRepository.list_active", lambda self: [platform])
        return claim_sync_job.run()

    def test_window_batch_and_budget_come_from_settings_and_are_shared(self, monkeypatch, db_session, platform):
        for i in range(30):
            db_session.add(
                Order(
                    platform_id=platform.id,
                    platform_order_no=f"JOB-{i:03d}",
                    status="NEW",
                    order_date=datetime.now(timezone.utc),
                    total_amount=1000,
                    discount_amount=0,
                )
            )
        db_session.flush()
        monkeypatch.setattr(settings, "claim_sync_window_days", 2)
        monkeypatch.setattr(settings, "claim_cancel_detail_max_orders_per_run", 5)
        conn = _Connector()

        results = self._run(monkeypatch, db_session, platform, conn)

        result = results[platform.code]
        assert [w[0] for w in conn.windows] == ["returns", "exchanges"]
        assert all((end - start).days == 2 for _, start, end in conn.windows)
        assert conn.lookup_calls == 5  # 설정의 실행당 상한
        assert len({id(b) for b in conn.budgets}) == 1  # 목록 조회와 취소 조회가 한 예산을 공유
        assert result["requests_used"] == 2 + 5 == conn.budgets[0].used
        assert conn.budgets[0].max_requests == settings.claim_sync_max_requests_per_run

    def test_a_small_budget_caps_the_whole_run_including_lookups(self, monkeypatch, db_session, platform):
        for i in range(10):
            db_session.add(
                Order(
                    platform_id=platform.id,
                    platform_order_no=f"CAP-{i:03d}",
                    status="NEW",
                    order_date=datetime.now(timezone.utc),
                    total_amount=1000,
                    discount_amount=0,
                )
            )
        db_session.flush()
        monkeypatch.setattr(settings, "claim_sync_max_requests_per_run", 6)
        monkeypatch.setattr(settings, "claim_cancel_detail_max_orders_per_run", 20)
        conn = _Connector()

        results = self._run(monkeypatch, db_session, platform, conn)

        assert results[platform.code]["requests_used"] <= 6
        assert conn.lookup_calls <= 6 - 2
