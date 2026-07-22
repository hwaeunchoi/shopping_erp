"""
tests/integration/test_api_reports.py
--------------------------------------------
api/routers/reports.py 통합 테스트. SRS FR-REPORT-03/04(월별 경영보고서
조회/엑셀/PDF 다운로드) + report_schedules CRUD.
"""

from datetime import datetime, timedelta, timezone


class TestMonthlyReport:
    def test_get_monthly_report_returns_zeroed_report_for_empty_month(self, client, auth_headers, seed_data):
        resp = client.get("/api/reports/monthly?year=2026&month=1", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["period_key"] == "2026-01"
        assert body["profit_loss"]["order_count"] == 0
        assert body["platform_breakdown"] == []

    def test_requires_authentication(self, client, seed_data):
        resp = client.get("/api/reports/monthly?year=2026&month=1")
        assert resp.status_code == 401

    def test_download_excel_returns_xlsx_content_type(self, client, auth_headers, seed_data):
        resp = client.get("/api/reports/monthly/excel?year=2026&month=1", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        assert "monthly_report_2026-01.xlsx" in resp.headers["content-disposition"]
        assert len(resp.content) > 0

    def test_download_pdf_returns_pdf_content_type(self, client, auth_headers, seed_data):
        resp = client.get("/api/reports/monthly/pdf?year=2026&month=1", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content[:4] == b"%PDF"


class TestReportSchedules:
    def test_create_list_update_delete_schedule(self, client, auth_headers, seed_data):
        next_run = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        create_resp = client.post(
            "/api/reports/schedules",
            json={
                "report_type": "PROFIT_REPORT",
                "frequency": "MONTHLY",
                "output_format": "XLSX",
                "recipient_emails": "a@example.com",
                "next_run_at": next_run,
            },
            headers=auth_headers,
        )
        assert create_resp.status_code == 201
        schedule_id = create_resp.json()["id"]
        assert create_resp.json()["is_enabled"] is True

        list_resp = client.get("/api/reports/schedules", headers=auth_headers)
        assert list_resp.status_code == 200
        assert any(s["id"] == schedule_id for s in list_resp.json())

        update_resp = client.patch(
            f"/api/reports/schedules/{schedule_id}", json={"is_enabled": False}, headers=auth_headers
        )
        assert update_resp.status_code == 200
        assert update_resp.json()["is_enabled"] is False

        delete_resp = client.delete(f"/api/reports/schedules/{schedule_id}", headers=auth_headers)
        assert delete_resp.status_code == 204

        list_after_delete = client.get("/api/reports/schedules", headers=auth_headers)
        assert not any(s["id"] == schedule_id for s in list_after_delete.json())

    def test_update_missing_schedule_returns_404(self, client, auth_headers, seed_data):
        resp = client.patch("/api/reports/schedules/999999", json={"is_enabled": False}, headers=auth_headers)
        assert resp.status_code == 404

    def test_delete_missing_schedule_returns_404(self, client, auth_headers, seed_data):
        resp = client.delete("/api/reports/schedules/999999", headers=auth_headers)
        assert resp.status_code == 404

    def test_requires_authentication(self, client, seed_data):
        resp = client.get("/api/reports/schedules")
        assert resp.status_code == 401
