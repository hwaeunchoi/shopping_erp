"""inventory_stock_buckets

단계 A — 재고 구조 스키마 변경 **만** 수행한다(업무 로직 변경 없음).

설계 원칙(Invariant): Inventory는 현재 창고에 존재하는 재고만 저장한다.
  - sellable_stock  : 판매 가능 실재고 총량(reserved_stock 포함)
  - reserved_stock  : sellable_stock 중 주문에 배정된 수량(별도 재고 아님)
  - defective_stock : 불량 판정됐으나 창고에 보관 중인 실물
  - (disposed_stock 없음) 폐기는 창고를 떠난 것이므로 이벤트로만 관리한다

변경 내용
  A1  inventory.stock_status 제거      (컬럼 분리 방식에서 불필요)
  A2  inventory.current_stock -> sellable_stock 개명 (의미 불변)
  A3  inventory.defective_stock 추가
  A4  inventory_transactions.from_status / to_status 추가
  A5  기존 이력 백필 (IN 계열 -> to=SELLABLE, OUT -> from=SELLABLE)

SQLite 주의
  inventory에는 이름 없는 FK가 있어 batch 모드(테이블 재생성)를 쓰면
  "Constraint must have a name" 오류가 난다. SQLite 3.25+/3.35+는
  RENAME COLUMN / DROP COLUMN을 네이티브 지원하므로 raw SQL로 직접 처리해
  batch를 우회한다.

Revision ID: 24f6e457d1dc
Revises: 5a2e3f775960
Create Date: 2026-07-22 14:00:00.000000+09:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "24f6e457d1dc"
down_revision: Union[str, None] = "5a2e3f775960"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    # A1 + A2 — 제거와 개명(엔진별 분기로 batch 회피)
    if _is_sqlite():
        op.execute("ALTER TABLE inventory DROP COLUMN stock_status")
        op.execute("ALTER TABLE inventory RENAME COLUMN current_stock TO sellable_stock")
    else:
        op.drop_column("inventory", "stock_status")
        op.alter_column("inventory", "current_stock", new_column_name="sellable_stock")

    # A3 — 불량 재고 칸
    op.add_column("inventory", sa.Column("defective_stock", sa.Integer(), nullable=False, server_default="0"))

    # A4 — 이력에 "어느 칸에서 어느 칸으로"를 기록 (NULL = 창고 외부)
    op.add_column("inventory_transactions", sa.Column("from_status", sa.String(20), nullable=True))
    op.add_column("inventory_transactions", sa.Column("to_status", sa.String(20), nullable=True))

    # A5 — 기존 이력 백필. 과거에는 판매가능 재고만 존재했으므로 SELLABLE로 채운다.
    op.execute("UPDATE inventory_transactions SET to_status = 'SELLABLE' WHERE type IN ('IN', 'RETURN_IN', 'ADJUST')")
    op.execute("UPDATE inventory_transactions SET from_status = 'SELLABLE' WHERE type = 'OUT'")


def downgrade() -> None:
    op.drop_column("inventory_transactions", "to_status")
    op.drop_column("inventory_transactions", "from_status")
    op.drop_column("inventory", "defective_stock")

    if _is_sqlite():
        op.execute("ALTER TABLE inventory RENAME COLUMN sellable_stock TO current_stock")
    else:
        op.alter_column("inventory", "sellable_stock", new_column_name="current_stock")

    op.add_column("inventory", sa.Column("stock_status", sa.String(20), nullable=False, server_default="SELLABLE"))
