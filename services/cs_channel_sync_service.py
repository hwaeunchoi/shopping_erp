"""
services/cs_channel_sync_service.py
--------------------------------------
상용 ERP 확장(5단계, B묶음) - 채널 CS(고객문의) 조회 동기화.

공식 계약이 확인된 것만 구현한다: 현재는 쿠팡 콜센터 문의(callCenterInquiries)
조회만 지원한다(integrations.malls.coupang_connector 모듈 상단 주석 참고 -
상품별 문의(onlineInquiries)는 조회 계약은 확인되나 이번 단계 범위 밖, 네이버는
응답 스키마/답변 바디 모두 미확인이라 커넥터 자체에 구현이 없다).

**답변(쓰기) 전송은 이 서비스에 없다** - 조회 전용이다. 쿠팡 콜센터 답변
API(POST .../callCenterInquiries/{id}/replies)는 요청 바디 필드 존재는
확인되지만 parentAnswerId의 "신규 답변(transfer 아님)" 케이스 값 의미를
문서로 확정할 수 없어 이번 단계에서 구현하지 않는다(docs/
COMMERCIAL_ERP_ROADMAP.md 5-B단계 절 참고). CS 케이스는 답변 초안
(reply_draft) 저장까지만 지원하고, 화면에는 "미지원(UNSUPPORTED)"으로 표시한다.

동기화 원칙:
- **멱등 저장**: (platform_id, external_inquiry_id) 유니크 제약(models.cs_case.
  CsCase.__table_args__)이 재수집 시 중복 케이스 생성을 DB 레벨에서도 막는다 -
  이 서비스는 그 전에 get_by_external()로 먼저 조회해 있으면 갱신, 없으면
  생성한다.
- **로컬 데이터 보존**: 담당자(assignee_id)/우선순위/상태(status)/태그/내부
  메모/답변초안은 재수집으로 절대 덮어쓰지 않는다 - 채널이 준 원본 상태
  (external_raw_status)와 마지막 고객 메시지 시각만 갱신한다.
- **알 수 없는 외부 상태를 임의 변환하지 않음**: external_raw_status는 채널
  원본 값을 그대로 보존만 한다 - CsCase.status(내부 CS 워크플로우 상태)로
  매핑하지 않는다(둘은 완전히 다른 상태체계다 - services/cs_case_service.py
  모듈 docstring 참고).
- **부분 실패가 전체 성공으로 위장되지 않음**: 항목 하나(_upsert_one)를
  SAVEPOINT(session.begin_nested())로 감싸 그 항목만 실패해도 롤백하고 나머지는
  계속 처리한다. 반환하는 status는 실패가 하나라도 있으면 PARTIAL_SUCCESS다
  (전부 실패하면 FAILED, 커넥터 자체 조회가 실패하면 결과가 아예 없다는 것을
  명확히 구분한다).
- **기능 플래그 OFF에서는 외부 요청 0건**: settings.cs_inquiry_sync_enabled가
  False면 sync_inquiries()는 connector를 만들거나 호출하지 않고 즉시
  반환한다(scheduler/jobs/cs_inquiry_sync_job.py가 이 플래그를 확인하지만,
  서비스 스스로도 방어적으로 한 번 더 확인한다 - 직접 호출에 대한 2차 방어).
"""

import logging
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from config.settings import settings
from integrations.malls.errors import (
    MarketplaceCapabilityUnsupportedError,
    MarketplaceCredentialMissingError,
    MarketplaceExternalAPIError,
)
from models.cs_case import CsCase, CsCaseHistory
from repositories.cs_case_repository import CsCaseHistoryRepository, CsCaseRepository
from repositories.order_repository import OrderRepository


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


logger = logging.getLogger(__name__)

COUPANG_CALL_CENTER_SOURCE = "COUPANG_CALL_CENTER"


class CsChannelSyncService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.case_repo = CsCaseRepository(session)
        self.history_repo = CsCaseHistoryRepository(session)
        self.order_repo = OrderRepository(session)

    def sync_inquiries(self, connector: Any, platform_id: int, start_date: date, end_date: date) -> dict[str, Any]:
        """connector.supports_inquiry_sync가 False면 커넥터를 호출하지 않고
        UNSUPPORTED를 반환한다(capability 인지 - "지원하지만 결과 0건"과 구분)."""
        if not settings.cs_inquiry_sync_enabled:
            return {"status": "DISABLED", "created": 0, "updated": 0, "failed": 0}
        if not getattr(connector, "supports_inquiry_sync", False):
            return {"status": "UNSUPPORTED", "created": 0, "updated": 0, "failed": 0}

        try:
            raw_items = connector.fetch_inquiries(start_date, end_date)
        except MarketplaceCapabilityUnsupportedError:
            return {"status": "UNSUPPORTED", "created": 0, "updated": 0, "failed": 0}
        except MarketplaceCredentialMissingError:
            return {"status": "FAILED", "created": 0, "updated": 0, "failed": 0, "reason_code": "CREDENTIAL_MISSING"}
        except MarketplaceExternalAPIError as e:
            return {"status": "FAILED", "created": 0, "updated": 0, "failed": 0, "reason_code": e.reason_code}

        created = 0
        updated = 0
        failed = 0
        for raw in raw_items:
            savepoint = self.session.begin_nested()
            try:
                was_created = self._upsert_one(platform_id, raw)
                savepoint.commit()
                if was_created:
                    created += 1
                else:
                    updated += 1
            except SQLAlchemyError:
                savepoint.rollback()
                failed += 1
                logger.warning("CS 문의 동기화 중 항목 하나 실패 - platform_id=%s", platform_id)

        if failed and not (created or updated):
            status = "FAILED"
        elif failed:
            status = "PARTIAL_SUCCESS"
        else:
            status = "SUCCESS"
        return {"status": status, "created": created, "updated": updated, "failed": failed}

    def _upsert_one(self, platform_id: int, raw: dict[str, Any]) -> bool:
        """새로 만들었으면 True, 기존 케이스를 갱신했으면 False를 반환한다."""
        external_id = raw["platform_inquiry_id"]
        existing = self.case_repo.get_by_external(platform_id, external_id)

        order = None
        platform_order_no = raw.get("platform_order_no")
        if platform_order_no:
            order = self.order_repo.get_by_platform_order_no(platform_id, platform_order_no)

        if existing is not None:
            existing.external_raw_status = raw.get("raw_status")
            inquiry_at = raw.get("inquiry_at")
            if inquiry_at is not None and (
                existing.last_customer_message_at is None or inquiry_at > existing.last_customer_message_at
            ):
                existing.last_customer_message_at = inquiry_at
                # 고객 쪽에서 새 내용이 온 것으로 보이는 경우에만 본문을 갱신한다 -
                # 담당자/태그/메모/답변초안 등 로컬 필드는 절대 건드리지 않는다.
                existing.customer_message = (raw.get("content") or existing.customer_message)[:4000]
            self.session.flush()
            return False

        case = CsCase(
            platform_id=platform_id,
            external_inquiry_id=external_id,
            external_source=COUPANG_CALL_CENTER_SOURCE,
            external_raw_status=raw.get("raw_status"),
            order_id=order.id if order else None,
            # 채널 문의를 세분화된 문의유형으로 임의 분류할 근거가 없어 ETC로 시작한다 -
            # 담당자가 상세를 확인한 뒤 필요하면 직접 재분류한다(이번 단계에는 재분류
            # API가 없다 - 향후 확장 여지).
            inquiry_type="ETC",
            priority="NORMAL",
            status="OPEN",
            customer_message=(raw.get("content") or "")[:4000],
            last_customer_message_at=raw.get("inquiry_at"),
        )
        self.case_repo.add(case)
        self.history_repo.add(
            CsCaseHistory(
                case_id=case.id,
                action="CREATED",
                from_value=None,
                to_value="OPEN",
                changed_by=None,
                changed_at=_now(),
                note=f"source={COUPANG_CALL_CENTER_SOURCE}",
            )
        )
        return True
