"""
scheduler/jobs/alert_evaluation_job.py
---------------------------------------------
UI v1.0 알림센터: 활성화된 alert_rules를 현재 지표와 비교해 조건을
만족하면 notifications를 생성한다. services.AlertEvaluationService에
실제 판정 로직을 위임한다.
"""

from core.database import session_scope
from services.alert_evaluation_service import AlertEvaluationService


def run() -> dict[str, int]:
    with session_scope() as db:
        created_ids = AlertEvaluationService(db).evaluate_all()
        return {"created_notifications": len(created_ids)}
