"""
services/exchange_return_service.py
----------------------------------------
교환(Exchange)/반품(Return)/취소(Cancellation) 등록/조회/상태 변경 업무로직.
SRS FR-ORD-03/04, UI 와이어프레임 "교환/반품/취소 관리" 화면 대응.

세 리소스는 스키마 상 독립된 테이블이지만(models/order.py) 요청 -> 승인/
처리 -> 완료(또는 거절)라는 흐름을 공유한다. 다만 완료 시 orders.status에
동기화할 값이 서로 달라(교환=EXCHANGED, 반품=REFUNDED, 취소=CANCELED)
하나의 클래스로 합치지 않고 리소스별로 나눴다.

주문 상태를 "성공적으로 완료" 상태에 도달했을 때만 동기화한다(거절/중간
상태에서는 orders.status를 바꾸지 않는다) - OrderSyncService.apply_status_change()를
재사용해 이력 기록/재고 반영 로직이 sync_orders() 경로와 갈라지지 않게 한다.
"""

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from models.order import Cancellation, Exchange, Return
from repositories.order_repository import CancellationRepository, ExchangeRepository, OrderRepository, ReturnRepository
from services.order_sync_service import OrderSyncService

VALID_EXCHANGE_STATUSES = {"REQUESTED", "APPROVED", "SHIPPED", "COMPLETED", "REJECTED"}
VALID_RETURN_STATUSES = {"REQUESTED", "APPROVED", "RECEIVED", "REFUNDED", "REJECTED"}
VALID_CANCELLATION_STATUSES = {"REQUESTED", "COMPLETED"}


class InvalidStatusError(Exception):
    """허용되지 않는 status 값으로 변경하려 할 때 발생한다."""


class ExchangeService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.order_repo = OrderRepository(session)
        self.exchange_repo = ExchangeRepository(session)
        self.order_sync_service = OrderSyncService(session)

    def create(self, order_id: int, order_item_id: Optional[int], reason: Optional[str]) -> Exchange:
        exchange = Exchange(
            order_id=order_id,
            order_item_id=order_item_id,
            reason=reason,
            status="REQUESTED",
            requested_at=datetime.now(timezone.utc),
        )
        return self.exchange_repo.add(exchange)

    def change_status(self, exchange: Exchange, new_status: str, warehouse_id: Optional[int] = None) -> Exchange:
        if new_status not in VALID_EXCHANGE_STATUSES:
            raise InvalidStatusError(f"허용되지 않는 교환 상태입니다: {new_status}")

        exchange.status = new_status
        if new_status in ("COMPLETED", "REJECTED") and exchange.completed_at is None:
            exchange.completed_at = datetime.now(timezone.utc)

        if new_status == "COMPLETED":
            order = self.order_repo.get_by_id(exchange.order_id)
            if order is not None and order.status != "EXCHANGED":
                self.order_sync_service.apply_status_change(order, "EXCHANGED", warehouse_id)

        self.session.flush()
        return exchange

    def get(self, exchange_id: int) -> Optional[Exchange]:
        return self.exchange_repo.get_by_id(exchange_id)

    def list(
        self,
        status: Optional[str] = None,
        order_id: Optional[int] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        search: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Exchange], int]:
        offset = (page - 1) * page_size
        items = self.exchange_repo.list_filtered(
            status=status,
            order_id=order_id,
            start_date=start_date,
            end_date=end_date,
            search=search,
            limit=page_size,
            offset=offset,
        )
        total = self.exchange_repo.count_filtered(
            status=status, order_id=order_id, start_date=start_date, end_date=end_date, search=search
        )
        return items, total


class ReturnService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.order_repo = OrderRepository(session)
        self.return_repo = ReturnRepository(session)
        self.order_sync_service = OrderSyncService(session)

    def create(
        self, order_id: int, order_item_id: Optional[int], reason: Optional[str], refund_amount: Optional[float]
    ) -> Return:
        ret = Return(
            order_id=order_id,
            order_item_id=order_item_id,
            reason=reason,
            refund_amount=refund_amount,
            status="REQUESTED",
            requested_at=datetime.now(timezone.utc),
        )
        return self.return_repo.add(ret)

    def change_status(self, ret: Return, new_status: str, warehouse_id: Optional[int] = None) -> Return:
        """warehouse_id는 REFUNDED로 바뀔 때 orders.status 동기화에 사용한다.

        정책 5: RECEIVED는 회수 도착 기록일 뿐 재고를 바꾸지 않는다. 반품 재고는
        검수를 통과해야 판매가능 재고가 되므로, 재고 반영은 검수 시점에
        InventoryService.inspect_return()이 단독으로 담당한다(SSoT).
        """
        if new_status not in VALID_RETURN_STATUSES:
            raise InvalidStatusError(f"허용되지 않는 반품 상태입니다: {new_status}")

        ret.status = new_status
        if new_status in ("REFUNDED", "REJECTED") and ret.completed_at is None:
            ret.completed_at = datetime.now(timezone.utc)

        if new_status == "REFUNDED":
            order = self.order_repo.get_by_id(ret.order_id)
            if order is not None and order.status != "REFUNDED":
                self.order_sync_service.apply_status_change(order, "REFUNDED", warehouse_id)

        self.session.flush()
        return ret

    def get(self, return_id: int) -> Optional[Return]:
        return self.return_repo.get_by_id(return_id)

    def list(
        self,
        status: Optional[str] = None,
        order_id: Optional[int] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        search: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Return], int]:
        offset = (page - 1) * page_size
        items = self.return_repo.list_filtered(
            status=status,
            order_id=order_id,
            start_date=start_date,
            end_date=end_date,
            search=search,
            limit=page_size,
            offset=offset,
        )
        total = self.return_repo.count_filtered(
            status=status, order_id=order_id, start_date=start_date, end_date=end_date, search=search
        )
        return items, total


class CancellationService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.order_repo = OrderRepository(session)
        self.cancellation_repo = CancellationRepository(session)
        self.order_sync_service = OrderSyncService(session)

    def create(self, order_id: int, reason: Optional[str], refund_amount: Optional[float]) -> Cancellation:
        cancellation = Cancellation(
            order_id=order_id,
            reason=reason,
            refund_amount=refund_amount,
            status="REQUESTED",
            requested_at=datetime.now(timezone.utc),
        )
        return self.cancellation_repo.add(cancellation)

    def change_status(
        self, cancellation: Cancellation, new_status: str, warehouse_id: Optional[int] = None
    ) -> Cancellation:
        if new_status not in VALID_CANCELLATION_STATUSES:
            raise InvalidStatusError(f"허용되지 않는 취소 상태입니다: {new_status}")

        cancellation.status = new_status
        if new_status == "COMPLETED":
            if cancellation.completed_at is None:
                cancellation.completed_at = datetime.now(timezone.utc)
            order = self.order_repo.get_by_id(cancellation.order_id)
            if order is not None and order.status != "CANCELED":
                self.order_sync_service.apply_status_change(order, "CANCELED", warehouse_id)

        self.session.flush()
        return cancellation

    def get(self, cancellation_id: int) -> Optional[Cancellation]:
        return self.cancellation_repo.get_by_id(cancellation_id)

    def list(
        self,
        status: Optional[str] = None,
        order_id: Optional[int] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        search: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Cancellation], int]:
        offset = (page - 1) * page_size
        items = self.cancellation_repo.list_filtered(
            status=status,
            order_id=order_id,
            start_date=start_date,
            end_date=end_date,
            search=search,
            limit=page_size,
            offset=offset,
        )
        total = self.cancellation_repo.count_filtered(
            status=status, order_id=order_id, start_date=start_date, end_date=end_date, search=search
        )
        return items, total


class OrderRateService:
    """SRS FR-ORD-04: 교환율/반품율/취소율을 계산한다.

    비율 = 기간 내 교환(또는 반품/취소) 신청 건수 / 기간 내 전체 주문건수 * 100.
    분모가 0이면(해당 기간에 주문이 없으면) 0으로 반환한다.
    """

    def __init__(self, session: Session) -> None:
        self.order_repo = OrderRepository(session)
        self.exchange_repo = ExchangeRepository(session)
        self.return_repo = ReturnRepository(session)
        self.cancellation_repo = CancellationRepository(session)

    def calculate_rates(self, start_date: Optional[date] = None, end_date: Optional[date] = None) -> dict:
        order_count = self.order_repo.count_filtered(start_date=start_date, end_date=end_date)
        exchange_count = self.exchange_repo.count_filtered(start_date=start_date, end_date=end_date)
        return_count = self.return_repo.count_filtered(start_date=start_date, end_date=end_date)
        cancellation_count = self.cancellation_repo.count_filtered(start_date=start_date, end_date=end_date)

        def _rate(count: int) -> float:
            return round(count / order_count * 100, 2) if order_count > 0 else 0.0

        return {
            "order_count": order_count,
            "exchange_count": exchange_count,
            "exchange_rate": _rate(exchange_count),
            "return_count": return_count,
            "return_rate": _rate(return_count),
            "cancellation_count": cancellation_count,
            "cancellation_rate": _rate(cancellation_count),
        }

    def pending_alerts(self) -> dict:
        """SRS FR-DASH-01: 대시보드 경고 카드용 현재 시점 스냅샷(기간과 무관).

        미배송 = orders.status가 NEW 또는 PREPARING인 건수, 교환/반품/취소
        대기 = 각각 status가 REQUESTED(아직 승인/처리 전)인 신청 건수.
        """
        unshipped_count = self.order_repo.count_filtered(status="NEW") + self.order_repo.count_filtered(
            status="PREPARING"
        )
        return {
            "unshipped_count": unshipped_count,
            "delayed_unshipped_count": self.order_repo.count_delayed_unshipped(),
            "exchange_pending_count": self.exchange_repo.count_filtered(status="REQUESTED"),
            "return_pending_count": self.return_repo.count_filtered(status="REQUESTED"),
            "cancellation_pending_count": self.cancellation_repo.count_filtered(status="REQUESTED"),
        }
