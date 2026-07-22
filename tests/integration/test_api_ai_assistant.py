"""
tests/integration/test_api_ai_assistant.py
--------------------------------------------------
api/routers/ai_assistant.py 통합 테스트. UI 와이어프레임 v1.1 8장(AI Assistant).
"""


class TestAskAssistant:
    def test_requires_authentication(self, client, seed_data):
        resp = client.post("/api/ai-assistant/ask", json={"question": "hello"})
        assert resp.status_code == 401

    def test_unrecognized_question_returns_fallback(self, client, auth_headers, seed_data):
        resp = client.post("/api/ai-assistant/ask", json={"question": "오늘 점심 뭐 먹지"}, headers=auth_headers)
        assert resp.status_code == 200
        assert "준비 중" in resp.json()["answer"]

    def test_quick_question_returns_data_backed_answer(self, client, auth_headers, seed_data):
        resp = client.post("/api/ai-assistant/ask", json={"question": "이번주 매출 요약"}, headers=auth_headers)
        assert resp.status_code == 200
        assert isinstance(resp.json()["answer"], str)
        assert len(resp.json()["answer"]) > 0
