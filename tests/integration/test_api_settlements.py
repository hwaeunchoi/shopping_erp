"""
tests/integration/test_api_settlements.py
------------------------------------------------
api/routers/settlements.py 통합 테스트: 목록/상세 조회(기존), 미매칭/차액 조회 및
정산 수집 트리거(상용 ERP 확장 2단계 - capability/기본 OFF/RBAC).
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from config.settings import settings


def _create_settlement(api_session_factory, platform_id: int, cycle: str = "2026-01-01~2026-01-31") -> int:
    from models.settlement import Settlement

    db = api_session_factory()
    try:
        settlement = Settlement(
            platform_id=platform_id,
            settlement_cycle=cycle,
            expected_amount=Decimal("100000"),
            settled_amount=Decimal("95000"),
            unsettled_amount=Decimal("5000"),
            discrepancy_amount=Decimal("0"),
            status="COMPLETED",
            created_at=datetime.now(timezone.utc),
        )
        db.add(settlement)
        db.commit()
        return settlement.id
    finally:
        db.close()


def _create_discrepancy(api_session_factory, platform_id: int, reason: str = "NO_MATCHING_ORDER") -> int:
    from models.settlement import SettlementDiscrepancy

    db = api_session_factory()
    try:
        disc = SettlementDiscrepancy(platform_id=platform_id, reason=reason, detected_at=datetime.now(timezone.utc))
        db.add(disc)
        db.commit()
        return disc.id
    finally:
        db.close()


class StubSettlementConnector:
    def __init__(self, *, supports=("settlements", "details"), errors=None):
        self.supports_settlement_sync = "settlements" in supports
        self.supports_settlement_detail_sync = "details" in supports
        self._errors = errors or {}

    def fetch_settlements(self, s, e):
        if "settlements" in self._errors:
            raise self._errors["settlements"]
        return []

    def fetch_settlement_details(self, s, e):
        if "details" in self._errors:
            raise self._errors["details"]
        return []


class TestListAndDetails:
    def test_list_settlements_by_platform(self, client, auth_headers, api_session_factory, seed_data):
        _create_settlement(api_session_factory, seed_data["platform_id"])

        resp = client.get(f"/api/settlements?platform_id={seed_data['platform_id']}", headers=auth_headers)

        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["settlement_cycle"] == "2026-01-01~2026-01-31"

    def test_list_details_empty_for_missing_settlement(self, client, auth_headers, seed_data):
        resp = client.get("/api/settlements/999999/details", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_requires_authentication(self, client, seed_data):
        resp = client.get("/api/settlements")
        assert resp.status_code == 401


class TestDiscrepancies:
    def test_lists_unresolved_discrepancies(self, client, auth_headers, api_session_factory, seed_data):
        _create_discrepancy(api_session_factory, seed_data["platform_id"], reason="NO_MATCHING_ORDER")

        resp = client.get(
            f"/api/settlements/discrepancies?platform_id={seed_data['platform_id']}", headers=auth_headers
        )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["reason"] == "NO_MATCHING_ORDER"
        assert body[0]["resolved_at"] is None


class TestSyncSettlements:
    """POST /sync - settings.claims_settlement_sync_enabled 기본 False이므로 이 클래스는
    켜 두고, TestSyncDisabledByDefault만 자체적으로 다시 꺼서 기본 차단을 검증한다."""

    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setattr(settings, "claims_settlement_sync_enabled", True)

    def _post(self, client, auth_headers, seed_data):
        return client.post(
            "/api/settlements/sync",
            json={"platform_id": seed_data["platform_id"], "start_date": "2026-01-01", "end_date": "2026-01-31"},
            headers=auth_headers,
        )

    def test_all_unsupported_returns_501(self, client, auth_headers, seed_data, monkeypatch):
        monkeypatch.setattr(
            "api.routers.settlements.get_mall_connector", lambda *a, **k: StubSettlementConnector(supports=())
        )
        resp = self._post(client, auth_headers, seed_data)
        assert resp.status_code == 501
        body = resp.json()
        assert body["overall_status"] == "UNSUPPORTED"

    def test_success_returns_200(self, client, auth_headers, seed_data, monkeypatch):
        monkeypatch.setattr("api.routers.settlements.get_mall_connector", lambda *a, **k: StubSettlementConnector())
        resp = self._post(client, auth_headers, seed_data)
        assert resp.status_code == 200
        assert resp.json()["overall_status"] == "SUCCESS"

    def test_missing_platform_returns_404(self, client, auth_headers):
        resp = client.post(
            "/api/settlements/sync",
            json={"platform_id": 999999, "start_date": "2026-01-01", "end_date": "2026-01-31"},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_credential_missing_returns_409(self, client, auth_headers, seed_data, monkeypatch):
        from integrations.malls.errors import MarketplaceCredentialMissingError

        # details는 미지원(UNSUPPORTED)으로 두어 overall이 PARTIAL이 아닌 FAILED가
        # 되도록 한다 - settlements만 FAILED고 details가 SUCCESS면 overall=PARTIAL이라
        # _settlement_sync_http_status가 200을 반환하기 때문(두 기능 모두 실패해야 409).
        monkeypatch.setattr(
            "api.routers.settlements.get_mall_connector",
            lambda *a, **k: StubSettlementConnector(
                supports=("settlements",), errors={"settlements": MarketplaceCredentialMissingError("x")}
            ),
        )
        resp = self._post(client, auth_headers, seed_data)
        assert resp.status_code == 409


class TestSyncDisabledByDefault:
    def test_sync_returns_501_without_touching_connector(self, client, auth_headers, seed_data, monkeypatch):
        monkeypatch.setattr(settings, "claims_settlement_sync_enabled", False)
        called = {"n": 0}

        def _fail_if_called(*a, **k):
            called["n"] += 1
            raise AssertionError("기능이 OFF인데 get_mall_connector가 호출되었습니다.")

        monkeypatch.setattr("api.routers.settlements.get_mall_connector", _fail_if_called)

        resp = client.post(
            "/api/settlements/sync",
            json={"platform_id": seed_data["platform_id"], "start_date": "2026-01-01", "end_date": "2026-01-31"},
            headers=auth_headers,
        )

        assert resp.status_code == 501
        assert resp.json()["settlements"]["reason_code"] == "FEATURE_DISABLED"
        assert called["n"] == 0
