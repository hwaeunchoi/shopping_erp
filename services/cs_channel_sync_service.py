"""
services/cs_channel_sync_service.py
--------------------------------------
상용 ERP 확장(5단계, B묶음) - 채널 CS(고객문의) 조회 동기화.

공식 계약이 확인된 것만 구현한다: 현재는 쿠팡 콜센터 문의(callCenterInquiries)와
상품별 문의(onlineInquiries) 조회를 지원한다(integrations.malls.coupang_connector
모듈 상단 주석 참고 - 네이버는 응답 스키마/답변 바디 모두 미확인이라 커넥터
자체에 구현이 없다).

**답변(쓰기) 전송은 이 서비스에 없다** - 조회 전용이다. 쿠팡 콜센터 답변
API(POST .../callCenterInquiries/{id}/replies)는 요청 바디 필드 존재는
확인되지만 parentAnswerId의 "신규 답변(transfer 아님)" 케이스 값 의미를
문서로 확정할 수 없어 이번 단계에서 구현하지 않는다(docs/
COMMERCIAL_ERP_ROADMAP.md 5-B단계 절 참고). CS 케이스는 답변 초안
(reply_draft) 저장까지만 지원하고, 화면에는 "미지원(UNSUPPORTED)"으로 표시한다.

**소스 구분**: 콜센터 문의와 상품별 문의는 서로 다른 API·응답 스키마를 쓰는
별개 capability다(`supports_inquiry_sync`/`supports_product_inquiry_sync`).
`_INQUIRY_SOURCES`가 소스별로 (capability 속성명, fetch 메서드명, external_source
값, 신규 케이스 기본 inquiry_type)을 담은 설정 테이블이다 - `sync_inquiries()`는
소스 하나를 명시적으로 지정해 실행하고(기본값은 하위호환을 위해 콜센터 문의),
`sync_all_inquiries()`는 두 소스를 모두 순회해 합산한 결과를 돌려준다
(scheduler/jobs/cs_inquiry_sync_job.py, api/routers/cs_cases.py가 이걸 쓴다).

동기화 원칙:
- **멱등 저장**: (platform_id, external_source, external_inquiry_id) 유니크
  제약(models.cs_case.CsCase.__table_args__)이 재수집 시 중복 케이스 생성을 DB
  레벨에서도 막는다 - 이 서비스는 그 전에 get_by_external()로 먼저 조회해 있으면
  갱신, 없으면 생성한다. external_source를 포함하는 이유는 두 문의 API의
  inquiryId가 서로 다른 독립 ID 공간일 수 있어서다(models.cs_case.CsCase
  클래스 docstring 참고 - 이를 포함하지 않았을 때 실제로 문의 오인 버그가
  있었다).
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
COUPANG_PRODUCT_INQUIRY_SOURCE = "COUPANG_PRODUCT_INQUIRY"

# 소스별 설정: (capability 속성명, fetch 메서드명, 신규 케이스 기본 inquiry_type).
# inquiry_type="PRODUCT"는 models.cs_case.CS_CASE_INQUIRY_TYPES에 이미 존재하는
# 값이라 enum 변경이 필요 없다 - 콜센터 문의는 세분화 근거가 없어 기존 그대로 ETC.
_INQUIRY_SOURCES: dict[str, tuple[str, str, str]] = {
    COUPANG_CALL_CENTER_SOURCE: ("supports_inquiry_sync", "fetch_inquiries", "ETC"),
    COUPANG_PRODUCT_INQUIRY_SOURCE: ("supports_product_inquiry_sync", "fetch_product_inquiries", "PRODUCT"),
}


class CsChannelSyncService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.case_repo = CsCaseRepository(session)
        self.history_repo = CsCaseHistoryRepository(session)
        self.order_repo = OrderRepository(session)

    def sync_inquiries(
        self,
        connector: Any,
        platform_id: int,
        start_date: date,
        end_date: date,
        source: str = COUPANG_CALL_CENTER_SOURCE,
    ) -> dict[str, Any]:
        """source에 해당하는 capability가 False면 커넥터를 호출하지 않고
        UNSUPPORTED를 반환한다(capability 인지 - "지원하지만 결과 0건"과 구분).
        source 기본값은 하위호환을 위해 기존 콜센터 문의로 유지한다 - 두 소스를
        모두 동기화하려면 sync_all_inquiries()를 쓴다."""
        if not settings.cs_inquiry_sync_enabled:
            return {"status": "DISABLED", "created": 0, "updated": 0, "failed": 0}

        capability_attr, fetch_method_name, default_inquiry_type = _INQUIRY_SOURCES[source]
        if not getattr(connector, capability_attr, False):
            return {"status": "UNSUPPORTED", "created": 0, "updated": 0, "failed": 0}

        try:
            raw_items = getattr(connector, fetch_method_name)(start_date, end_date)
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
                was_created = self._upsert_one(platform_id, source, default_inquiry_type, raw)
                savepoint.commit()
                if was_created:
                    created += 1
                else:
                    updated += 1
            except SQLAlchemyError:
                savepoint.rollback()
                failed += 1
                logger.warning("CS 문의 동기화 중 항목 하나 실패 - platform_id=%s, source=%s", platform_id, source)

        if failed and not (created or updated):
            status = "FAILED"
        elif failed:
            status = "PARTIAL_SUCCESS"
        else:
            status = "SUCCESS"
        return {"status": status, "created": created, "updated": updated, "failed": failed}

    def sync_all_inquiries(self, connector: Any, platform_id: int, start_date: date, end_date: date) -> dict[str, Any]:
        """공식 계약이 확인된 모든 문의 소스(현재: 콜센터 문의 + 상품별 문의)를
        차례로 동기화하고 결과를 합산한다. 소스 하나가 UNSUPPORTED여도 다른
        소스는 계속 진행한다(둘 다 UNSUPPORTED면 전체도 UNSUPPORTED). 반환값의
        `by_source`에는 소스별 원본 결과를 그대로 남겨(디버깅/화면 참고용) 어느
        소스가 실패했는지 구분할 수 있게 한다."""
        if not settings.cs_inquiry_sync_enabled:
            return {"status": "DISABLED", "created": 0, "updated": 0, "failed": 0, "by_source": {}}

        by_source: dict[str, dict[str, Any]] = {}
        total_created = 0
        total_updated = 0
        total_failed = 0
        reason_codes: list[str] = []
        for source in _INQUIRY_SOURCES:
            result = self.sync_inquiries(connector, platform_id, start_date, end_date, source=source)
            by_source[source] = result
            total_created += result.get("created", 0)
            total_updated += result.get("updated", 0)
            total_failed += result.get("failed", 0)
            if result.get("status") == "FAILED" and result.get("reason_code"):
                reason_codes.append(result["reason_code"])

        statuses = {r["status"] for r in by_source.values()}
        if statuses == {"UNSUPPORTED"}:
            overall = "UNSUPPORTED"
        elif statuses <= {"SUCCESS", "UNSUPPORTED"} and total_failed == 0:
            overall = "SUCCESS"
        elif statuses & {"SUCCESS", "PARTIAL_SUCCESS"}:
            # 최소 한 소스는 뭔가 처리했다 - 나머지가 FAILED/UNSUPPORTED여도 전체
            # 성공을 완전 실패로 위장하지 않는다.
            overall = "PARTIAL_SUCCESS"
        else:
            overall = "FAILED"

        combined: dict[str, Any] = {
            "status": overall,
            "created": total_created,
            "updated": total_updated,
            "failed": total_failed,
            "by_source": by_source,
        }
        if reason_codes:
            combined["reason_code"] = ",".join(reason_codes)
        return combined

    def _upsert_one(self, platform_id: int, source: str, default_inquiry_type: str, raw: dict[str, Any]) -> bool:
        """새로 만들었으면 True, 기존 케이스를 갱신했으면 False를 반환한다."""
        external_id = raw["platform_inquiry_id"]
        existing = self.case_repo.get_by_external(platform_id, source, external_id)

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
            external_source=source,
            external_raw_status=raw.get("raw_status"),
            order_id=order.id if order else None,
            # 채널 문의를 세분화된 문의유형으로 임의 분류할 근거가 없는 소스(콜센터
            # 문의)는 ETC로 시작한다 - 상품별 문의는 이미 확인된 값(PRODUCT)이 있어
            # 그대로 채운다. 담당자가 상세를 확인한 뒤 필요하면 직접 재분류한다
            # (이번 단계에는 재분류 API가 없다 - 향후 확장 여지).
            inquiry_type=default_inquiry_type,
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
                note=f"source={source}",
            )
        )
        return True
