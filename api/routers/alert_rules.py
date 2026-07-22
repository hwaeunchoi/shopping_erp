"""
api/routers/alert_rules.py
-------------------------------
UI v1.0 알림센터: 사용자 정의 알림 규칙(alert_rules) CRUD. 환경설정 화면의
일부이므로 SETTINGS_MANAGE 권한으로 보호한다.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.deps import get_current_user, get_db, require_permission
from models.system import AlertRule
from models.user import User
from repositories.system_repository import AlertRuleRepository

router = APIRouter(
    prefix="/api/alert-rules", tags=["alert-rules"], dependencies=[Depends(require_permission("SETTINGS_MANAGE"))]
)


class AlertRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    metric: str
    operator: str
    threshold_value: Optional[float]
    scope_type: Optional[str]
    scope_id: Optional[int]
    check_frequency: str
    is_enabled: bool
    created_by: Optional[int]
    created_at: datetime


class AlertRuleCreate(BaseModel):
    name: str
    metric: str
    operator: str
    threshold_value: Optional[float] = None
    scope_type: Optional[str] = None
    scope_id: Optional[int] = None
    check_frequency: str = "HOURLY"
    is_enabled: bool = True


class AlertRuleUpdate(BaseModel):
    name: Optional[str] = None
    operator: Optional[str] = None
    threshold_value: Optional[float] = None
    check_frequency: Optional[str] = None
    is_enabled: Optional[bool] = None


@router.get("", response_model=list[AlertRuleOut], summary="알림 규칙 목록 조회")
def list_alert_rules(db: Session = Depends(get_db)) -> list:
    return AlertRuleRepository(db).list_all(limit=200)


@router.post("", response_model=AlertRuleOut, status_code=status.HTTP_201_CREATED, summary="알림 규칙 등록")
def create_alert_rule(
    payload: AlertRuleCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> AlertRule:
    rule = AlertRule(**payload.model_dump(), created_by=current_user.id, created_at=datetime.now(timezone.utc))
    AlertRuleRepository(db).add(rule)
    db.commit()
    return rule


@router.patch(
    "/{rule_id}",
    response_model=AlertRuleOut,
    summary="알림 규칙 수정",
    responses={404: {"description": "알림 규칙을 찾을 수 없습니다."}},
)
def update_alert_rule(rule_id: int, payload: AlertRuleUpdate, db: Session = Depends(get_db)) -> AlertRule:
    repo = AlertRuleRepository(db)
    rule = repo.get_by_id(rule_id)
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="알림 규칙을 찾을 수 없습니다.")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(rule, field, value)
    db.commit()
    return rule


@router.delete(
    "/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="알림 규칙 삭제",
    responses={404: {"description": "알림 규칙을 찾을 수 없습니다."}},
)
def delete_alert_rule(rule_id: int, db: Session = Depends(get_db)) -> None:
    repo = AlertRuleRepository(db)
    rule = repo.get_by_id(rule_id)
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="알림 규칙을 찾을 수 없습니다.")
    repo.delete(rule)
    db.commit()
