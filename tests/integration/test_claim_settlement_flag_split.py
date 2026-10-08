"""
tests/integration/test_claim_settlement_flag_split.py
---------------------------------------------------------
클레임(취소·반품·교환) 수집과 정산 수집의 활성화 플래그 분리 계약.

- 기본값은 둘 다 False이고, 예전 환경변수 CLAIMS_SETTLEMENT_SYNC_ENABLED는 어느 쪽도 켜지 못한다.
- 한 플래그를 켜도 다른 기능은 세션·커넥터(credential 복호화)·HTTP·DB 쓰기가 전혀 없다(자동 job과 수동 API 양쪽).
"""

import pytest

from config.settings import Settings, settings
from scheduler.jobs import claim_sync_job, settlement_sync_job

LEGACY = "CLAIMS_SETTLEMENT_SYNC_ENABLED"


class TestFlagDefaultsAndEnvMapping:
    def test_defaults_are_both_false(self):
        parsed = Settings(_env_file=None)  # type: ignore[call-arg]
        assert parsed.claim_sync_enabled is False
        assert parsed.settlement_sync_enabled is False

    def test_legacy_variable_does_not_enable_either_feature(self, monkeypatch):
        monkeypatch.setenv(LEGACY, "true")
        parsed = Settings(_env_file=None)  # type: ignore[call-arg]
        assert parsed.claim_sync_enabled is False
        assert parsed.settlement_sync_enabled is False
        assert not hasattr(parsed, "claims_settlement_sync_enabled")

    def test_legacy_variable_is_ignored_even_when_present_at_import_time(self):
        """Settings 모듈을 "새 프로세스"에서 import하며(클래스 정의 시점에 환경이 이미 있다) 예전 변수가 어느 플래그도
        켜지 못하는지 확인한다 - 정의 시점에 환경을 읽는 구현도 잡아낸다."""
        import os
        import subprocess
        import sys
        from pathlib import Path

        repo = Path(__file__).resolve().parent.parent.parent
        env = {k: v for k, v in os.environ.items() if k not in ("CLAIM_SYNC_ENABLED", "SETTLEMENT_SYNC_ENABLED")}
        env[LEGACY] = "true"
        code = (
            "from config.settings import Settings; s = Settings(_env_file=None); "
            "print(s.claim_sync_enabled, s.settlement_sync_enabled)"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=repo, env=env, capture_output=True, text=True, timeout=60
        )
        assert out.returncode == 0
        assert out.stdout.strip() == "False False"

    def test_each_new_variable_enables_only_its_own_feature(self, monkeypatch):
        monkeypatch.setenv("CLAIM_SYNC_ENABLED", "true")
        only_claim = Settings(_env_file=None)  # type: ignore[call-arg]
        assert (only_claim.claim_sync_enabled, only_claim.settlement_sync_enabled) == (True, False)
        monkeypatch.delenv("CLAIM_SYNC_ENABLED")
        monkeypatch.setenv("SETTLEMENT_SYNC_ENABLED", "true")
        only_settlement = Settings(_env_file=None)  # type: ignore[call-arg]
        assert (only_settlement.claim_sync_enabled, only_settlement.settlement_sync_enabled) == (False, True)


def _forbid(name):
    def _raise(*args, **kwargs):
        raise AssertionError(f"기능이 꺼져 있는데 {name}이(가) 호출되었습니다.")

    return _raise


class TestJobsAreIndependent:
    def test_settlement_job_does_nothing_when_only_claims_are_enabled(self, monkeypatch):
        monkeypatch.setattr(settings, "claim_sync_enabled", True)
        monkeypatch.setattr(settings, "settlement_sync_enabled", False)
        monkeypatch.setattr(settlement_sync_job, "session_scope", _forbid("session_scope"))
        monkeypatch.setattr(settlement_sync_job, "get_mall_connector", _forbid("get_mall_connector"))
        assert settlement_sync_job.run() == {"skipped_disabled": {"skipped": "disabled"}}

    def test_claim_job_does_nothing_when_only_settlement_is_enabled(self, monkeypatch):
        monkeypatch.setattr(settings, "claim_sync_enabled", False)
        monkeypatch.setattr(settings, "settlement_sync_enabled", True)
        monkeypatch.setattr(claim_sync_job, "session_scope", _forbid("session_scope"))
        monkeypatch.setattr(claim_sync_job, "get_mall_connector", _forbid("get_mall_connector"))
        assert claim_sync_job.run() == {"skipped_disabled": {"skipped": "disabled"}}


class TestManualApisAreIndependent:
    def _post_claims(self, client, auth_headers, seed_data):
        return client.post(
            "/api/orders/sync-claims",
            json={
                "platform_id": seed_data["platform_id"],
                "warehouse_id": seed_data["warehouse_id"],
                "start_date": "2026-01-01",
                "end_date": "2026-01-31",
            },
            headers=auth_headers,
        )

    def _post_settlements(self, client, auth_headers, seed_data):
        return client.post(
            "/api/settlements/sync",
            json={"platform_id": seed_data["platform_id"], "start_date": "2026-01-01", "end_date": "2026-01-31"},
            headers=auth_headers,
        )

    def test_claims_api_is_blocked_when_only_settlement_is_enabled(self, client, auth_headers, seed_data, monkeypatch):
        monkeypatch.setattr(settings, "claim_sync_enabled", False)
        monkeypatch.setattr(settings, "settlement_sync_enabled", True)
        monkeypatch.setattr("api.routers.orders.get_mall_connector", _forbid("get_mall_connector"))
        resp = self._post_claims(client, auth_headers, seed_data)
        assert resp.status_code == 501
        assert resp.json()["returns"]["reason_code"] == "FEATURE_DISABLED"

    def test_settlement_api_is_blocked_when_only_claims_are_enabled(self, client, auth_headers, seed_data, monkeypatch):
        monkeypatch.setattr(settings, "claim_sync_enabled", True)
        monkeypatch.setattr(settings, "settlement_sync_enabled", False)
        monkeypatch.setattr("api.routers.settlements.get_mall_connector", _forbid("get_mall_connector"))
        resp = self._post_settlements(client, auth_headers, seed_data)
        assert resp.status_code == 501
        assert resp.json()["settlements"]["reason_code"] == "FEATURE_DISABLED"


@pytest.mark.parametrize("flag", ["claim_sync_enabled", "settlement_sync_enabled"])
def test_flag_attribute_exists_and_is_a_bool(flag):
    assert isinstance(getattr(Settings(_env_file=None), flag), bool)  # type: ignore[call-arg]
