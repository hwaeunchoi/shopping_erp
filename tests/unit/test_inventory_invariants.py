"""
tests/unit/test_inventory_invariants.py
--------------------------------------------
단계 B 완료 기준 T1~T8.

B의 완료는 "기능이 동작한다"가 아니라 "불변조건이 깨지지 않는다"로 판정한다.
따라서 이 파일은 정상 동작보다 **위반이 확실히 거부되는지**를 검증한다.
"""

import pytest

from models.inventory import Inventory, InventoryStatus, InventoryTransaction
from services.inventory_service import (
    InsufficientStockError,
    InvalidTransitionError,
    InventoryInvariantError,
    InventoryService,
)


class TestT1ReservedNotExceedSellable:
    """T1 - reserved_stock은 sellable_stock을 넘을 수 없다 (I1)."""

    def test_reserve_within_sellable_succeeds(self, db_session, inventory_row):
        result = InventoryService(db_session).reserve(inventory_row.product_option_id, inventory_row.warehouse_id, 100)
        assert result.reserved_stock == 100
        assert result.reserved_stock <= result.sellable_stock

    def test_reserve_over_sellable_is_rejected(self, db_session, inventory_row):
        service = InventoryService(db_session)

        with pytest.raises(InventoryInvariantError):
            service.reserve(inventory_row.product_option_id, inventory_row.warehouse_id, 101)

        db_session.refresh(inventory_row)
        assert inventory_row.reserved_stock == 0  # 변경되지 않았다


class TestT2NoNegativeStock:
    """T2 - 어떤 재고 칸도 음수가 될 수 없다 (I6)."""

    def test_transition_more_than_available_is_rejected(self, db_session, inventory_row):
        service = InventoryService(db_session)

        with pytest.raises(InsufficientStockError):
            service.transition_stock(
                inventory=inventory_row,
                from_status=InventoryStatus.SELLABLE,
                to_status=InventoryStatus.DEFECTIVE,
                quantity=101,  # 보유 100
                reason="한도 초과 이동",
            )

        db_session.refresh(inventory_row)
        assert inventory_row.sellable_stock == 100
        assert inventory_row.defective_stock == 0

    def test_defective_cannot_go_negative(self, db_session, inventory_row):
        service = InventoryService(db_session)

        with pytest.raises(InsufficientStockError):
            service.transition_stock(
                inventory=inventory_row,
                from_status=InventoryStatus.DEFECTIVE,
                to_status=InventoryStatus.DISPOSED,
                quantity=1,  # 불량 재고 0
                reason="없는 불량 재고 폐기",
            )

    def test_zero_or_negative_quantity_is_rejected(self, db_session, inventory_row):
        service = InventoryService(db_session)

        with pytest.raises(ValueError):
            service.transition_stock(
                inventory=inventory_row,
                from_status=None,
                to_status=InventoryStatus.SELLABLE,
                quantity=0,
                reason="0 수량",
            )


class TestT3ForbiddenTransitionBlocked:
    """T3 - 전이 행렬에 없는 조합은 거부된다."""

    def test_same_status_transition_is_rejected(self, db_session, inventory_row):
        with pytest.raises(InvalidTransitionError):
            InventoryService(db_session).transition_stock(
                inventory=inventory_row,
                from_status=InventoryStatus.SELLABLE,
                to_status=InventoryStatus.SELLABLE,
                quantity=1,
                reason="자기 자신으로 전이",
            )

    def test_outside_to_outside_is_rejected(self, db_session, inventory_row):
        with pytest.raises(InvalidTransitionError):
            InventoryService(db_session).transition_stock(
                inventory=inventory_row, from_status=None, to_status=None, quantity=1, reason="재고와 무관한 이동"
            )


class TestT4ShipmentDecreasesBoth:
    """T4 - 출고 시 sellable과 reserved가 함께 감소한다 (I2)."""

    def test_shipment_decreases_sellable_and_reserved_together(self, db_session, inventory_row):
        service = InventoryService(db_session)
        service.reserve(inventory_row.product_option_id, inventory_row.warehouse_id, 10)

        service.deduct_on_shipment(inventory_row.product_option_id, inventory_row.warehouse_id, 10, reference_id=1)

        db_session.refresh(inventory_row)
        assert inventory_row.sellable_stock == 90  # 100 - 10
        assert inventory_row.reserved_stock == 0  # 10 - 10 (함께 감소)
        assert inventory_row.reserved_stock <= inventory_row.sellable_stock  # I1 유지

    def test_full_stock_shipment_keeps_invariant(self, db_session, inventory_row):
        """전량 예약된 재고를 전량 출고해도 I1이 깨지지 않는다."""
        service = InventoryService(db_session)
        service.reserve(inventory_row.product_option_id, inventory_row.warehouse_id, 100)

        service.deduct_on_shipment(inventory_row.product_option_id, inventory_row.warehouse_id, 100)

        db_session.refresh(inventory_row)
        assert inventory_row.sellable_stock == 0
        assert inventory_row.reserved_stock == 0


class TestT5InspectPassIncreasesSellable:
    """T5 - 검수 합격은 sellable_stock을 증가시킨다 (I4)."""

    def test_inspection_pass_increases_sellable(self, db_session, inventory_row):
        result = InventoryService(db_session).inspect_return(
            inventory_row.product_option_id,
            inventory_row.warehouse_id,
            quantity=3,
            result="PASS",
            reason="반품 검수 양품",
            reference_id=77,
        )

        assert result.inventory.sellable_stock == 103
        assert result.inventory.defective_stock == 0
        assert result.transaction.type == "INSPECT"
        assert result.transaction.to_status == InventoryStatus.SELLABLE


class TestT6InspectFailIncreasesDefective:
    """T6 - 검수 불합격은 defective_stock을 증가시킨다 (I5)."""

    def test_inspection_defective_increases_defective_not_sellable(self, db_session, inventory_row):
        result = InventoryService(db_session).inspect_return(
            inventory_row.product_option_id,
            inventory_row.warehouse_id,
            quantity=2,
            result="DEFECTIVE",
            reason="반품 검수 불량",
            reference_id=78,
        )

        assert result.inventory.sellable_stock == 100  # 판매가능은 늘지 않는다
        assert result.inventory.defective_stock == 2
        assert result.transaction.to_status == InventoryStatus.DEFECTIVE

    def test_inspection_disposed_increases_neither(self, db_session, inventory_row):
        """폐기 판정은 어느 칸도 늘리지 않고 이벤트만 남긴다."""
        result = InventoryService(db_session).inspect_return(
            inventory_row.product_option_id,
            inventory_row.warehouse_id,
            quantity=1,
            result="DISPOSED",
            reason="반품 검수 폐기",
        )

        assert result.inventory.sellable_stock == 100
        assert result.inventory.defective_stock == 0
        assert result.transaction.type == "DISPOSE"

    def test_invalid_inspection_result_is_rejected(self, db_session, inventory_row):
        with pytest.raises(ValueError):
            InventoryService(db_session).inspect_return(
                inventory_row.product_option_id, inventory_row.warehouse_id, 1, "UNKNOWN", "잘못된 결과"
            )


class TestT7DisposedIsTerminal:
    """T7 - DISPOSED에서는 어떤 상태로도 이동할 수 없다."""

    @pytest.mark.parametrize(
        "to_status", [InventoryStatus.SELLABLE, InventoryStatus.DEFECTIVE, InventoryStatus.DISPOSED, None]
    )
    def test_no_transition_out_of_disposed(self, db_session, inventory_row, to_status):
        with pytest.raises(InvalidTransitionError):
            InventoryService(db_session).transition_stock(
                inventory=inventory_row,
                from_status=InventoryStatus.DISPOSED,
                to_status=to_status,
                quantity=1,
                reason="폐기 재고 되돌리기 시도",
            )


class TestT8RollbackOnFailure:
    """T8 - 실패 시 inventory 변경도, 이력 기록도 남지 않는다 (원자성)."""

    def test_t8a_validation_failure_changes_nothing(self, db_session, inventory_row):
        """검증 실패 - 변경 전에 거부되므로 아무것도 바뀌지 않는다."""
        before_sellable = inventory_row.sellable_stock
        before_defective = inventory_row.defective_stock
        before_tx = db_session.query(InventoryTransaction).count()

        with pytest.raises(InvalidTransitionError):
            InventoryService(db_session).transition_stock(
                inventory=inventory_row,
                from_status=InventoryStatus.DISPOSED,
                to_status=InventoryStatus.SELLABLE,
                quantity=5,
                reason="금지된 전이",
            )

        db_session.refresh(inventory_row)
        assert inventory_row.sellable_stock == before_sellable
        assert inventory_row.defective_stock == before_defective
        assert db_session.query(InventoryTransaction).count() == before_tx

    def test_t8b_failure_during_write_rolls_back(self, db_session, inventory_row, monkeypatch):
        """변경 도중 실패 - 롤백으로 재고와 이력이 모두 원상복구된다.

        inventory를 이미 변경한 뒤 이력 기록 단계에서 오류를 일으켜, 부분 변경이
        실제로 되돌려지는지를 본다. 이것이 원자성의 진짜 증명이다.

        transition_stock()은 commit하지 않으므로(트랜잭션 경계는 호출자 소유),
        호출자에 해당하는 SAVEPOINT를 테스트가 직접 열고 되돌린다.
        """
        inventory_id = inventory_row.id
        before_sellable = inventory_row.sellable_stock
        before_tx = db_session.query(InventoryTransaction).count()

        service = InventoryService(db_session)
        original_add = db_session.add

        def fail_on_transaction(obj, *args, **kwargs):
            if isinstance(obj, InventoryTransaction):
                raise RuntimeError("이력 기록 단계 강제 실패")
            return original_add(obj, *args, **kwargs)

        monkeypatch.setattr(db_session, "add", fail_on_transaction)

        # begin_nested()가 호출자의 트랜잭션 경계 역할을 한다(SAVEPOINT).
        with pytest.raises(RuntimeError), db_session.begin_nested():
            service.transition_stock(
                inventory=inventory_row,
                from_status=InventoryStatus.SELLABLE,
                to_status=InventoryStatus.DEFECTIVE,
                quantity=5,
                reason="쓰기 도중 실패",
            )

        monkeypatch.undo()
        db_session.expire_all()

        reloaded = db_session.get(Inventory, inventory_id)
        assert reloaded is not None
        assert reloaded.sellable_stock == before_sellable  # 변경 없음
        assert reloaded.defective_stock == 0
        assert db_session.query(InventoryTransaction).count() == before_tx  # 이력 없음


class TestP0EngineIsTheOnlyPath:
    """P0 회귀 방지 - 모든 재고 변경이 엔진(transition_stock)을 통과하는지 검증한다.

    TD-1/TD-2: manual_adjust()와 receive_purchase_order()가 엔진을 우회하던 시절에는
    (1) I1 검증이 없어 reserved > sellable을 만들 수 있었고,
    (2) 이력의 from/to_status가 비어 원장으로 재고를 재구성할 수 없었다.
    """

    def test_manual_adjust_cannot_break_i1(self, db_session, inventory_row):
        """TD-1 회귀: 예약분보다 적게 남기는 수동 조정은 거부된다."""
        service = InventoryService(db_session)
        service.reserve(inventory_row.product_option_id, inventory_row.warehouse_id, 100)

        with pytest.raises(InventoryInvariantError):
            service.manual_adjust(inventory_row.product_option_id, inventory_row.warehouse_id, -50, memo="실사 감모")

        db_session.refresh(inventory_row)
        assert inventory_row.sellable_stock == 100  # 변경되지 않았다
        assert inventory_row.reserved_stock <= inventory_row.sellable_stock

    def test_manual_adjust_within_free_stock_succeeds(self, db_session, inventory_row):
        """예약분을 침범하지 않는 감모는 정상 처리된다(과잉 차단이 아님을 확인)."""
        service = InventoryService(db_session)
        service.reserve(inventory_row.product_option_id, inventory_row.warehouse_id, 40)

        result = service.manual_adjust(inventory_row.product_option_id, inventory_row.warehouse_id, -50)

        assert result.sellable_stock == 50
        assert result.reserved_stock == 40

    def test_manual_adjust_zero_delta_is_rejected(self, db_session, inventory_row):
        with pytest.raises(ValueError):
            InventoryService(db_session).manual_adjust(inventory_row.product_option_id, inventory_row.warehouse_id, 0)

    @pytest.mark.parametrize(
        ("delta", "expected_from", "expected_to"),
        [(20, None, InventoryStatus.SELLABLE), (-20, InventoryStatus.SELLABLE, None)],
    )
    def test_manual_adjust_records_transition_statuses(
        self, db_session, inventory_row, delta, expected_from, expected_to
    ):
        """TD-2 회귀: 수동 조정 이력에도 from/to_status가 남는다."""
        result = InventoryService(db_session).manual_adjust(
            inventory_row.product_option_id, inventory_row.warehouse_id, delta
        )
        txn = db_session.query(InventoryTransaction).filter_by(reference_type="MANUAL").one()

        assert txn.type == "ADJUST"
        assert txn.quantity == delta  # 부호 표기는 종전과 동일
        assert txn.from_status == expected_from
        assert txn.to_status == expected_to
        assert result.sellable_stock == 100 + delta

    def test_purchase_receipt_records_transition_statuses(self, db_session, inventory_row):
        """TD-2 회귀: 발주 입고 이력에도 from/to_status가 남는다."""
        InventoryService(db_session).receive_purchase_order(
            inventory_row.product_option_id, inventory_row.warehouse_id, 30, reference_id=7
        )
        txn = db_session.query(InventoryTransaction).filter_by(reference_type="PURCHASE_ORDER").one()

        assert txn.type == "IN"
        assert txn.quantity == 30
        assert txn.from_status is None
        assert txn.to_status == InventoryStatus.SELLABLE

    def test_ledger_reconstructs_inventory(self, db_session, inventory_row):
        """모든 경로를 섞어 쓴 뒤에도 원장 합계가 inventory와 일치한다.

        이것이 엔진 단일화의 최종 목적이다 - 실사 대조와 감사 추적의 근거.
        """
        service = InventoryService(db_session)
        service.receive_purchase_order(inventory_row.product_option_id, inventory_row.warehouse_id, 50, 1)
        service.manual_adjust(inventory_row.product_option_id, inventory_row.warehouse_id, -20)
        service.inspect_return(inventory_row.product_option_id, inventory_row.warehouse_id, 5, "PASS", "검수 합격")
        service.inspect_return(inventory_row.product_option_id, inventory_row.warehouse_id, 3, "DEFECTIVE", "검수 불량")
        service.deduct_on_shipment(inventory_row.product_option_id, inventory_row.warehouse_id, 10)

        txns = db_session.query(InventoryTransaction).all()
        ledger_sellable = sum(
            (
                abs(t.quantity)
                if t.to_status == InventoryStatus.SELLABLE
                else -abs(t.quantity) if t.from_status == InventoryStatus.SELLABLE else 0
            )
            for t in txns
        )
        ledger_defective = sum(
            (
                abs(t.quantity)
                if t.to_status == InventoryStatus.DEFECTIVE
                else -abs(t.quantity) if t.from_status == InventoryStatus.DEFECTIVE else 0
            )
            for t in txns
        )

        db_session.refresh(inventory_row)
        # 초기 100은 fixture가 이력 없이 만든 값이므로 제외하고 대조한다.
        assert ledger_sellable == inventory_row.sellable_stock - 100
        assert ledger_defective == inventory_row.defective_stock
