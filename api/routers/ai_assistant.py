"""
api/routers/ai_assistant.py
--------------------------------
UI v1.1 8장 ERP AI Assistant. 전용 권한 없이 로그인한 사용자면 사용할 수
있다(모든 화면에서 동일한 위치에 고정 노출되는 패널이라 화면별 권한과
분리한다).
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.deps import get_current_user, get_db
from services.ai_assistant_service import AIAssistantService

router = APIRouter(prefix="/api/ai-assistant", tags=["ai-assistant"], dependencies=[Depends(get_current_user)])


class AskIn(BaseModel):
    question: str


class AskOut(BaseModel):
    answer: str


@router.post(
    "/ask",
    response_model=AskOut,
    summary="AI Assistant 질문",
    description="1차 범위에서는 실제 AI 모델을 호출하지 않고, 기존 집계 데이터를 요약한 규칙 기반 응답을 "
    "반환한다(이번주 매출 요약/저ROAS 캠페인 찾기/반품 급증 원인 3종은 실데이터 기반, 그 외는 준비 중 안내).",
)
def ask(payload: AskIn, db: Session = Depends(get_db)) -> AskOut:
    answer = AIAssistantService(db).answer(payload.question)
    return AskOut(answer=answer)
