"""
tests/integration/test_api_platforms.py
------------------------------------------------
api/routers/platforms.py 통합 테스트: 활성 플랫폼 목록(GET /api/platforms, 기존
동작 회귀)과 capability matrix(GET /api/platforms/capability-matrix, 상용 ERP
확장 4단계 - 11번가 연동 준비) 계약을 확인한다.

capability matrix의 핵심 계약: 공식 API 계약이 검증된 채널(integrations.malls.
SUPPORTED_CONNECTORS 소속)만 capabilities가 실제 dict로 채워지고, 그 외(11번가 등
아직 실 연동이 없는 채널)는 "개별 기능을 점검해 전부 미지원으로 나왔다"가 아니라
"애초에 확인된 게 없다"를 뜻하는 null이어야 한다 - 이 구분이 화면에서 지원 여부를
잘못 전달하지 않는 유일한 안전장치다.
"""

from datetime import datetime, timezone

from models.platform import Platform


def _add_elevenst_platform(api_session_factory) -> int:
    db = api_session_factory()
    try:
        platform = Platform(
            code="elevenst",
            name="11번가",
            connector_class="ElevenstConnector",
            settlement_cycle_days=14,
            is_active=False,
        )
        db.add(platform)
        db.commit()
        return platform.id
    finally:
        db.close()


def _seed_integration_status(api_session_factory, code: str, *, success: bool) -> None:
    from models.extra import IntegrationStatus

    db = api_session_factory()
    try:
        now = datetime.now(timezone.utc)
        if success:
            db.add(
                IntegrationStatus(
                    integration_type="MALL", integration_code=code, status="NORMAL", last_success_at=now, updated_at=now
                )
            )
        else:
            db.add(
                IntegrationStatus(
                    integration_type="MALL",
                    integration_code=code,
                    status="ERROR",
                    last_error_at=now,
                    last_error_message="AUTH_FAILED",
                    updated_at=now,
                )
            )
        db.commit()
    finally:
        db.close()


class TestListPlatforms:
    def test_only_returns_active_platforms(self, client, auth_headers, seed_data, api_session_factory):
        _add_elevenst_platform(api_session_factory)

        resp = client.get("/api/platforms", headers=auth_headers)

        assert resp.status_code == 200
        codes = [p["code"] for p in resp.json()]
        assert "coupang" in codes
        assert "elevenst" not in codes

    def test_requires_authentication(self, client):
        resp = client.get("/api/platforms")
        assert resp.status_code == 401


class TestCapabilityMatrix:
    def test_includes_inactive_platforms(self, client, auth_headers, seed_data, api_session_factory):
        _add_elevenst_platform(api_session_factory)

        resp = client.get("/api/platforms/capability-matrix", headers=auth_headers)

        assert resp.status_code == 200
        by_code = {p["code"]: p for p in resp.json()}
        assert "elevenst" in by_code
        assert by_code["elevenst"]["is_active"] is False

    def test_verified_connector_has_capability_dict(self, client, auth_headers, seed_data):
        resp = client.get("/api/platforms/capability-matrix", headers=auth_headers)

        assert resp.status_code == 200
        by_code = {p["code"]: p for p in resp.json()}
        coupang = by_code["coupang"]
        assert coupang["official_contract_verified"] is True
        assert coupang["capabilities"] is not None
        assert coupang["capabilities"]["product_create"] is True
        assert coupang["capabilities"]["product_info_update"] is False

    def test_unverified_connector_has_null_capabilities_not_all_false(
        self, client, auth_headers, seed_data, api_session_factory
    ):
        _add_elevenst_platform(api_session_factory)

        resp = client.get("/api/platforms/capability-matrix", headers=auth_headers)

        by_code = {p["code"]: p for p in resp.json()}
        elevenst = by_code["elevenst"]
        assert elevenst["official_contract_verified"] is False
        assert elevenst["capabilities"] is None

    def test_unknown_connector_class_is_treated_as_unverified(
        self, client, auth_headers, seed_data, api_session_factory
    ):
        db = api_session_factory()
        try:
            db.add(
                Platform(
                    code="typo_platform",
                    name="오타 플랫폼",
                    connector_class="DoesNotExistConnector",
                    settlement_cycle_days=7,
                    is_active=False,
                )
            )
            db.commit()
        finally:
            db.close()

        resp = client.get("/api/platforms/capability-matrix", headers=auth_headers)

        by_code = {p["code"]: p for p in resp.json()}
        assert by_code["typo_platform"]["official_contract_verified"] is False
        assert by_code["typo_platform"]["capabilities"] is None

    def test_reports_last_success_and_error_from_integration_status(
        self, client, auth_headers, seed_data, api_session_factory
    ):
        _seed_integration_status(api_session_factory, "coupang", success=True)

        resp = client.get("/api/platforms/capability-matrix", headers=auth_headers)

        by_code = {p["code"]: p for p in resp.json()}
        assert by_code["coupang"]["last_success_at"] is not None
        assert by_code["coupang"]["last_error_at"] is None

    def test_error_message_is_returned_but_never_fabricated(self, client, auth_headers, seed_data, api_session_factory):
        _seed_integration_status(api_session_factory, "coupang", success=False)

        resp = client.get("/api/platforms/capability-matrix", headers=auth_headers)

        by_code = {p["code"]: p for p in resp.json()}
        assert by_code["coupang"]["last_error_message"] == "AUTH_FAILED"
        assert by_code["coupang"]["last_success_at"] is None

    def test_platform_with_no_sync_history_reports_none(self, client, auth_headers, seed_data, api_session_factory):
        _add_elevenst_platform(api_session_factory)

        resp = client.get("/api/platforms/capability-matrix", headers=auth_headers)

        by_code = {p["code"]: p for p in resp.json()}
        assert by_code["elevenst"]["last_success_at"] is None
        assert by_code["elevenst"]["last_error_at"] is None
        assert by_code["elevenst"]["last_error_message"] is None

    def test_requires_authentication(self, client):
        resp = client.get("/api/platforms/capability-matrix")
        assert resp.status_code == 401


class TestElevenstStaysInactiveByDefault:
    """DEFAULT_PLATFORMS의 elevenst is_active=False가 그대로 유지되는지(회귀) -
    scripts/init_db.py를 직접 파싱해 이 라운드에서 실수로 활성화하지 않았는지
    확인한다."""

    def test_default_platforms_seed_keeps_elevenst_inactive(self):
        from scripts.init_db import DEFAULT_PLATFORMS

        by_code = {code: is_active for code, _, _, _, is_active in DEFAULT_PLATFORMS}
        assert by_code["elevenst"] is False
        assert by_code["esm"] is False
        assert by_code["kakao_shopping"] is False
