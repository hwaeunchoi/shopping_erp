"""
services/settlement_sync_service.py
------------------------------------------
정산 회차 요약(Settlement)/주문단위 상세(SettlementDetail) 수집 + 대사(reconciliation).
상용 ERP 확장(2단계).

capability 인지 + 기능별 격리(SAVEPOINT)는 services.claim_sync_service.ClaimSyncService
와 같은 설계 원칙이다:
  - supports_settlement_sync/supports_settlement_detail_sync가 True인 기능만 호출한다
    (미지원은 UNSUPPORTED로 구분 - "결과 0건"과 혼동하지 않는다).
  - 각각 SAVEPOINT 안에서 처리하고, 한 기능의 실패(인증/외부/DB/예상밖)는 그 기능만
    롤백하고 다음 기능은 계속한다.

금액은 전부 Decimal로 다룬다 - models.settlement의 금액 컬럼은 Numeric이고 Python
쪽 타입도 Decimal이다(float 연산 없음). 미제공 금액을 0으로 추정하지 않는다(커넥터가
None을 주면 그대로 None으로 두거나, 필수로 확인된 필드에만 0을 기본값으로 쓴다 -
integrations.malls.coupang_connector._to_decimal 참고).

dedup:
  - Settlement: (platform_id, settlement_cycle, settlement_type) 조합으로 upsert.
  - SettlementDetail: 채널이 별도 라인 ID를 주지 않아 (settlement_id, order_id,
    order_item_id, sale_type, recognition_date) 자연키로 upsert한다.

대사(reconciliation) - 자동으로 추정해 덮어쓰지 않고 SettlementDiscrepancy로 남긴다:
  - NO_MATCHING_ORDER: 상세 라인의 주문번호를 아직 수집하지 못함(order_collect_job이
    나중에 수집하면 다음 재수집 때 자동으로 다시 시도된다 - 별도 승격 절차는 없다,
    claim_sync_service의 ClaimUnmatched와 달리 재수집 자체가 재시도 역할을 한다).
  - NO_MATCHING_SETTLEMENT: 상세 라인이 속할 정산 회차(같은 settled_date/scheduled_date)
    를 찾지 못함.
  - AMOUNT_MISMATCH: 정산 회차의 상세 합계와 회차 요약(settled_amount)이 허용오차
    (AMOUNT_TOLERANCE)를 넘어 다름 - 주문 매출과 정산 입금을 섞어 집계하지 않기
    위해 반드시 상세 라인(SettlementDetail)을 근거로 재계산한다.
  - 같은 미해소 불일치는 재수집 때마다 새로 만들지 않는다(SettlementDiscrepancyRepository.
    get_unresolved_for로 확인).
"""

import logging
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Optional

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from integrations.malls.errors import (
    MarketplaceCapabilityUnsupportedError,
    MarketplaceCredentialMissingError,
    MarketplaceExternalAPIError,
)
from models.settlement import Settlement, SettlementDetail, SettlementDiscrepancy
from repositories.order_repository import OrderRepository
from repositories.settlement_repository import SettlementDiscrepancyRepository, SettlementRepository

logger = logging.getLogger(__name__)

# 반올림 오차 허용치(원) - 이보다 큰 차이만 AMOUNT_MISMATCH로 남긴다.
AMOUNT_TOLERANCE = Decimal("1")


class SettlementSyncService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settlement_repo = SettlementRepository(session)
        self.discrepancy_repo = SettlementDiscrepancyRepository(session)
        self.order_repo = OrderRepository(session)
        # sync_settlement_details() 호출 하나에서 상세가 반영된 정산 회차 id 모음 -
        # _recheck_touched_settlements()가 이 회차들만 금액을 재확인한다.
        self._touched_settlement_ids: set[int] = set()

    def sync_settlements(self, connector: Any, platform_id: int, start_date: date, end_date: date) -> dict[str, Any]:
        """정산 회차 요약(Settlement)을 수집한다."""
        return self._sync_feature(
            supported=getattr(connector, "supports_settlement_sync", False),
            fetch=lambda: connector.fetch_settlements(start_date, end_date),
            persist=lambda raw: self._persist_settlement(platform_id, raw),
            feature="settlements",
        )

    def sync_settlement_details(
        self, connector: Any, platform_id: int, start_date: date, end_date: date
    ) -> dict[str, Any]:
        """정산 상세(주문단위)를 수집하고 정산 회차/주문과 대사한다."""
        result = self._sync_feature(
            supported=getattr(connector, "supports_settlement_detail_sync", False),
            fetch=lambda: connector.fetch_settlement_details(start_date, end_date),
            persist=lambda raw: self._persist_settlement_detail(platform_id, raw),
            feature="settlement_details",
        )
        if result["status"] == "SUCCESS":
            self._recheck_touched_settlements(platform_id)
        return result

    def _sync_feature(
        self, supported: bool, fetch: Callable[[], list], persist: Callable[[dict], bool], feature: str
    ) -> dict[str, Any]:
        if not supported:
            return _feature_result("UNSUPPORTED", 0, "CAPABILITY_UNSUPPORTED", False)

        savepoint = self.session.begin_nested()
        try:
            count = 0
            for raw in fetch():
                if persist(raw):
                    count += 1
            self.session.flush()
            savepoint.commit()
            return _feature_result("SUCCESS", count, None, None)
        except MarketplaceCredentialMissingError:
            savepoint.rollback()
            return _feature_result("FAILED", 0, "CREDENTIAL_MISSING", False)
        except MarketplaceExternalAPIError as e:
            savepoint.rollback()
            return _feature_result("FAILED", 0, e.reason_code, bool(e.retryable))
        except MarketplaceCapabilityUnsupportedError:
            savepoint.rollback()
            return _feature_result("UNSUPPORTED", 0, "CAPABILITY_UNSUPPORTED", False)
        except SQLAlchemyError:
            savepoint.rollback()
            logger.warning("정산 DB 반영 실패: feature=%s, trace=%s", feature, uuid.uuid4().hex[:8])
            return _feature_result("FAILED", 0, "DB_WRITE_FAILED", False)
        except Exception:  # noqa: BLE001 - 예상 밖 예외도 기능별 격리(내부 전문 미노출)
            savepoint.rollback()
            logger.warning("정산 처리 예상 밖 오류: feature=%s, trace=%s", feature, uuid.uuid4().hex[:8])
            return _feature_result("FAILED", 0, "INTERNAL_ERROR", False)

    def _persist_settlement(self, platform_id: int, raw: dict[str, Any]) -> bool:
        existing = self.settlement_repo.get_by_platform_and_cycle(
            platform_id, raw["settlement_cycle"], raw.get("settlement_type")
        )
        expected = raw["expected_amount"]
        settled = raw["settled_amount"]
        if existing is not None:
            existing.scheduled_date = raw["scheduled_date"]
            existing.settled_date = raw["settled_date"]
            existing.expected_amount = expected
            existing.settled_amount = settled
            existing.unsettled_amount = expected - settled
            existing.status = raw["status"]
            self._touched_settlement_ids.add(existing.id)
            return True
        created = self.settlement_repo.add(
            Settlement(
                platform_id=platform_id,
                settlement_cycle=raw["settlement_cycle"],
                settlement_type=raw.get("settlement_type"),
                scheduled_date=raw["scheduled_date"],
                settled_date=raw["settled_date"],
                expected_amount=expected,
                settled_amount=settled,
                unsettled_amount=expected - settled,
                discrepancy_amount=Decimal("0"),
                status=raw["status"],
                created_at=datetime.now(timezone.utc),
            )
        )
        self._touched_settlement_ids.add(created.id)
        return True

    def _persist_settlement_detail(self, platform_id: int, raw: dict[str, Any]) -> bool:
        order_no = raw.get("platform_order_no")
        order = self.order_repo.get_by_platform_order_no(platform_id, order_no) if order_no else None
        if order is None:
            self._record_discrepancy(
                platform_id,
                reason="NO_MATCHING_ORDER",
                order_id=None,
                settlement_id=None,
                detail_summary=f"order_no={_clip(order_no, 100)}",
            )
            return False

        target_date = raw.get("settled_date") or raw.get("recognition_date")
        settlement = self.settlement_repo.get_by_platform_and_date(platform_id, target_date) if target_date else None
        if settlement is None:
            self._record_discrepancy(
                platform_id,
                reason="NO_MATCHING_SETTLEMENT",
                order_id=order.id,
                settlement_id=None,
                detail_summary=f"order_id={order.id}, date={target_date}",
            )
            return False

        order_item_id = None
        poin = raw.get("platform_order_item_no")
        if poin:
            item = self.order_repo.get_item_by_platform_order_item_no(order.id, poin)
            order_item_id = item.id if item is not None else None

        existing = self.settlement_repo.get_detail_by_natural_key(
            settlement.id, order.id, order_item_id, raw.get("sale_type"), raw.get("recognition_date")
        )
        gross = raw["gross_amount"]
        fee = raw["fee_amount"]
        net = raw["net_amount"]
        if existing is not None:
            existing.gross_amount = gross
            existing.fee_amount = fee
            existing.net_amount = net
        else:
            self.settlement_repo.add_detail(
                SettlementDetail(
                    settlement_id=settlement.id,
                    order_id=order.id,
                    order_item_id=order_item_id,
                    gross_amount=gross,
                    fee_amount=fee,
                    net_amount=net,
                    sale_type=raw.get("sale_type"),
                    recognition_date=raw.get("recognition_date"),
                )
            )
        self._touched_settlement_ids.add(settlement.id)
        return True

    def _record_discrepancy(
        self, platform_id: int, reason: str, order_id: Optional[int], settlement_id: Optional[int], detail_summary: str
    ) -> None:
        existing = self.discrepancy_repo.get_unresolved_for(
            platform_id, reason, order_id=order_id, settlement_id=settlement_id
        )
        if existing is not None:
            return
        self.discrepancy_repo.add(
            SettlementDiscrepancy(
                platform_id=platform_id,
                settlement_id=settlement_id,
                order_id=order_id,
                reason=reason,
                detail_summary=_clip(detail_summary, 500),
                detected_at=datetime.now(timezone.utc),
            )
        )

    def _recheck_touched_settlements(self, platform_id: int) -> None:
        """이번 sync_settlement_details() 호출에서 상세가 반영된 정산 회차만 금액을
        재확인한다(전체 회차를 매번 다시 계산하지 않기 위함)."""
        for settlement_id in self._touched_settlement_ids:
            self.check_amount_mismatch(settlement_id)

    def check_amount_mismatch(self, settlement_id: int) -> Optional[SettlementDiscrepancy]:
        """정산 회차의 상세 합계와 회차 요약(settled_amount)을 비교해 허용오차를 넘으면
        AMOUNT_MISMATCH를 남긴다(이미 같은 회차로 미해소 불일치가 있으면 갱신)."""
        settlement = self.settlement_repo.get_by_id(settlement_id)
        if settlement is None:
            return None
        details = self.settlement_repo.list_details(settlement_id)
        detail_sum = sum((Decimal(d.net_amount) for d in details), Decimal("0"))
        diff = detail_sum - Decimal(settlement.settled_amount)
        existing = self.discrepancy_repo.get_unresolved_for(
            settlement.platform_id, "AMOUNT_MISMATCH", settlement_id=settlement_id
        )
        if abs(diff) <= AMOUNT_TOLERANCE:
            # 더 이상 불일치가 아니다 - 기존에 남아있던 미해소 기록은 그대로 두되(자동
            # 해소하지 않는다, 운영자가 확인하도록) 새로 만들지는 않는다.
            return existing
        if existing is not None:
            existing.expected_amount = settlement.settled_amount
            existing.actual_amount = detail_sum
            existing.diff_amount = diff
            return existing
        return self.discrepancy_repo.add(
            SettlementDiscrepancy(
                platform_id=settlement.platform_id,
                settlement_id=settlement_id,
                reason="AMOUNT_MISMATCH",
                expected_amount=settlement.settled_amount,
                actual_amount=detail_sum,
                diff_amount=diff,
                detail_summary=_clip(f"settlement_id={settlement_id}", 500),
                detected_at=datetime.now(timezone.utc),
            )
        )


def _feature_result(status: str, count: int, reason_code: Optional[str], retryable: Optional[bool]) -> dict[str, Any]:
    return {"status": status, "count": count, "reason_code": reason_code, "retryable": retryable}


def _clip(value: Any, length: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text[:length] if len(text) > length else text
