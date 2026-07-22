"""shipment_items_consolidation

주문관리 재설계 1단계 — 배송 구조 재설계(합포장/분할배송 지원).

기존: shipments.order_id UNIQUE  →  주문:배송 = 1:1 로 고정
      이 제약 때문에 합포장(주문 N:배송 1)과 분할배송(주문 1:배송 N)이
      둘 다 구조적으로 불가능했다.

변경: shipment_items(shipment_id, order_id, order_item_id, quantity)를 도입해
      주문:배송을 N:M으로 해소하고, shipments.order_id 컬럼을 제거한다.

데이터 이관: 기존 shipments 각 행을 shipment_items 1행(order_item_id=NULL =
      '주문 전체')으로 복사한 뒤 order_id 컬럼을 제거한다. 컬럼 삭제 전에
      복사하므로 기존 배송-주문 연결이 유실되지 않는다.

Revision ID: efc947fe91e2
Revises: 8fa1e562b4f8
Create Date: 2026-07-21 16:00:00.000000+09:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "efc947fe91e2"
down_revision: Union[str, None] = "8fa1e562b4f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shipment_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("shipment_id", sa.Integer(), sa.ForeignKey("shipments.id"), nullable=False),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("order_item_id", sa.Integer(), sa.ForeignKey("order_items.id"), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=True),
    )
    op.create_index("idx_shipment_items_shipment", "shipment_items", ["shipment_id"])
    op.create_index("idx_shipment_items_order", "shipment_items", ["order_id"])

    # 기존 1:1 배송을 shipment_items로 이관(order_item_id NULL = 주문 전체).
    op.execute(
        "INSERT INTO shipment_items (shipment_id, order_id, order_item_id, quantity) "
        "SELECT id, order_id, NULL, NULL FROM shipments"
    )

    # SQLite는 DROP COLUMN 시 batch 모드로 테이블을 재생성하는데, 기존 order_id의
    # UNIQUE 제약에 이름이 없어 그대로는 실패한다(Postgres는 네이티브 DROP이라 무관).
    # naming_convention을 주어 재생성 시 제약 이름을 만들 수 있게 한다.
    naming_convention = {
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s",
        "ix": "ix_%(table_name)s_%(column_0_name)s",
    }
    with op.batch_alter_table("shipments", naming_convention=naming_convention) as batch_op:
        batch_op.drop_column("order_id")


def downgrade() -> None:
    op.add_column("shipments", sa.Column("order_id", sa.Integer(), nullable=True))

    # 배송 1건당 첫 주문 하나만 되돌린다(합포장 이후에는 완전 복원이 불가능하다).
    op.execute(
        "UPDATE shipments SET order_id = ("
        "  SELECT min(si.order_id) FROM shipment_items si WHERE si.shipment_id = shipments.id"
        ")"
    )

    op.drop_index("idx_shipment_items_order", table_name="shipment_items")
    op.drop_index("idx_shipment_items_shipment", table_name="shipment_items")
    op.drop_table("shipment_items")
