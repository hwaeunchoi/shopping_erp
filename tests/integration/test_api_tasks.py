"""
tests/integration/test_api_tasks.py
------------------------------------------
api/routers/tasks.py 통합 테스트. UI 와이어프레임 v1.1 3장(대시보드
빠른실행 버튼 5종) 대응.

ORDER_COLLECT/AD_COLLECT/BACKUP/FULL_SYNC는 scheduler/jobs/*.run()이
core.database.session_scope()(실제 앱 엔진에 바인딩)를 사용하므로, 테스트의
격리된 인메모리 DB를 건드리지 않도록 해당 run() 함수들을 스텁으로
monkeypatch한다. REPORT_GENERATE만 요청의 세션(api_session_factory)을 그대로
사용하므로 실제 로직으로 검증한다.
"""

from unittest.mock import patch

from core.security import hash_password
from models.user import Role, User


def _make_limited_user(api_session_factory, permission_codes: list[str]) -> dict:
    """DASHBOARD_VIEW 등 일부 권한만 가진 사용자를 만들고 로그인 정보를 반환한다."""
    from models.user import Permission, RolePermission

    db = api_session_factory()
    try:
        role = Role(name="LimitedRole")
        db.add(role)
        db.flush()
        for code in permission_codes:
            perm = db.query(Permission).filter_by(code=code).one_or_none()
            if perm is None:
                perm = Permission(code=code, name=code)
                db.add(perm)
                db.flush()
            db.add(RolePermission(role_id=role.id, permission_id=perm.id))
        user = User(
            username="limited",
            password_hash=hash_password("Pw123456!"),
            name="제한사용자",
            role_id=role.id,
            is_active=True,
        )
        db.add(user)
        db.commit()
        return {"username": "limited", "password": "Pw123456!"}
    finally:
        db.close()


class TestTriggerTask:
    def test_requires_authentication(self, client, seed_data):
        resp = client.post("/api/tasks/trigger", json={"task_type": "ORDER_COLLECT"})
        assert resp.status_code == 401

    def test_rejects_unknown_task_type(self, client, auth_headers, seed_data):
        resp = client.post("/api/tasks/trigger", json={"task_type": "NOT_A_TASK"}, headers=auth_headers)
        assert resp.status_code == 422

    def test_admin_without_matching_permission_gets_403(self, client, api_session_factory, seed_data):
        creds = _make_limited_user(api_session_factory, ["DASHBOARD_VIEW"])
        login_resp = client.post("/api/auth/login", data=creds)
        headers = {"Authorization": f"Bearer {login_resp.json()['access_token']}"}

        resp = client.post("/api/tasks/trigger", json={"task_type": "ORDER_COLLECT"}, headers=headers)
        assert resp.status_code == 403

    def test_order_collect_records_manual_history_on_success(
        self, client, auth_headers, seed_data, api_session_factory
    ):
        with patch("api.routers.tasks.order_collect_job.run", return_value={"coupang": {"created": 3}}):
            resp = client.post("/api/tasks/trigger", json={"task_type": "ORDER_COLLECT"}, headers=auth_headers)

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "SUCCESS"
        assert "coupang" in body["result_summary"]

        from models.extra import TaskExecutionHistory

        db = api_session_factory()
        try:
            history = db.query(TaskExecutionHistory).filter_by(id=body["id"]).one()
            assert history.trigger_type == "MANUAL"
            assert history.triggered_by == seed_data["admin_id"]
            assert history.task_type == "ORDER_COLLECT"
        finally:
            db.close()

    def test_job_failure_records_failed_history_and_returns_500(self, client, auth_headers, seed_data):
        with patch("api.routers.tasks.ad_collect_job.run", side_effect=RuntimeError("연동 실패")):
            resp = client.post("/api/tasks/trigger", json={"task_type": "AD_COLLECT"}, headers=auth_headers)

        assert resp.status_code == 500

    def test_full_sync_runs_all_sub_jobs(self, client, auth_headers, seed_data):
        with (
            patch("api.routers.tasks.order_collect_job.run", return_value={}) as order_run,
            patch("api.routers.tasks.ad_collect_job.run", return_value={}) as ad_run,
            patch("api.routers.tasks.settlement_sync_job.run", return_value={}) as settlement_run,
            patch("api.routers.tasks.customer_stats_job.run", return_value=0) as customer_run,
        ):
            resp = client.post("/api/tasks/trigger", json={"task_type": "FULL_SYNC"}, headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json()["status"] == "SUCCESS"
        order_run.assert_called_once()
        ad_run.assert_called_once()
        settlement_run.assert_called_once()
        customer_run.assert_called_once()

    def test_report_generate_runs_real_report_service(self, client, auth_headers, seed_data):
        resp = client.post("/api/tasks/trigger", json={"task_type": "REPORT_GENERATE"}, headers=auth_headers)

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "SUCCESS"
        assert "period_key" in body["result_summary"]
