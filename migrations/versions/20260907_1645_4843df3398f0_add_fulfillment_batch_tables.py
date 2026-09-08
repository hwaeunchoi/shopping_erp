"""add_fulfillment_batch_tables

상용 ERP 확장(5단계, A묶음) - 출고 배치(피킹/검수/포장) 신규 테이블.
기존 테이블은 건드리지 않는다(순수 추가) - models.fulfillment 참고.

autogenerate 대신 손으로 작성했다 - 이 환경의 SQLite로는 기존 마이그레이션
이력(9a2c5e8b1f47 이전 어딘가의 PostgreSQL 전용 ALTER COLUMN TYPE) 전체를
처음부터 재생할 수 없어(로드맵에 이미 알려진 한계), autogenerate가 비교할
"현재 head 상태의 SQLite DB"를 만들 방법이 없었다. 새 테이블 3개를 추가하는
것뿐이라 손으로 작성해도 모호함이 없다(기존 테이블의 ALTER/DROP이 전혀 없다).

Revision ID: 4843df3398f0
Revises: c4cd4340574e
Create Date: 2026-09-07 16:45:00.000000+09:00

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "4843df3398f0"
down_revision: Union[str, None] = "c4cd4340574e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fulfillment_batches",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("warehouse_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["warehouse_id"], ["warehouses.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "fulfillment_batch_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("order_item_id", sa.Integer(), nullable=False),
        sa.Column("product_option_id", sa.Integer(), nullable=False),
        sa.Column("requested_quantity", sa.Integer(), nullable=False),
        sa.Column("picked_quantity", sa.Integer(), nullable=True),
        sa.Column("verified_quantity", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("failure_reason_code", sa.String(length=200), nullable=True),
        sa.Column("picked_by", sa.Integer(), nullable=True),
        sa.Column("picked_at", sa.DateTime(), nullable=True),
        sa.Column("verified_by", sa.Integer(), nullable=True),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("packed_by", sa.Integer(), nullable=True),
        sa.Column("packed_at", sa.DateTime(), nullable=True),
        sa.Column("inventory_deducted_at", sa.DateTime(), nullable=True),
        sa.Column("shipment_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["fulfillment_batches.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["order_item_id"], ["order_items.id"]),
        sa.ForeignKeyConstraint(["packed_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["picked_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["product_option_id"], ["product_options.id"]),
        sa.ForeignKeyConstraint(["shipment_id"], ["shipments.id"]),
        sa.ForeignKeyConstraint(["verified_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_fulfillment_batch_items_batch", "fulfillment_batch_items", ["batch_id"], unique=False
    )
    op.create_index(
        "idx_fulfillment_batch_items_order_item", "fulfillment_batch_items", ["order_item_id"], unique=False
    )
    op.create_index(
        "idx_fulfillment_batch_items_shipment", "fulfillment_batch_items", ["shipment_id"], unique=False
    )
    op.create_table(
        "fulfillment_batch_item_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_item_id", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(length=20), nullable=True),
        sa.Column("to_status", sa.String(length=20), nullable=False),
        sa.Column("changed_by", sa.Integer(), nullable=True),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("note", sa.String(length=300), nullable=True),
        sa.ForeignKeyConstraint(["batch_item_id"], ["fulfillment_batch_items.id"]),
        sa.ForeignKeyConstraint(["changed_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_fulfillment_batch_item_history",
        "fulfillment_batch_item_history",
        ["batch_item_id", "changed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_fulfillment_batch_item_history", table_name="fulfillment_batch_item_history")
    op.drop_table("fulfillment_batch_item_history")
    op.drop_index("idx_fulfillment_batch_items_shipment", table_name="fulfillment_batch_items")
    op.drop_index("idx_fulfillment_batch_items_order_item", table_name="fulfillment_batch_items")
    op.drop_index("idx_fulfillment_batch_items_batch", table_name="fulfillment_batch_items")
    op.drop_table("fulfillment_batch_items")
    op.drop_table("fulfillment_batches")
