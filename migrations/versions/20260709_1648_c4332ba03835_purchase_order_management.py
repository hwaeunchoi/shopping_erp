"""purchase_order_management

공급처 발주(자동발주 확장 대상) 관리 테이블 추가: purchase_orders, purchase_order_items.
models/purchase_order.py 참고.

Revision ID: c4332ba03835
Revises: 7d4a1f9c3b2e
Create Date: 2026-07-09 16:48:00.000000+09:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4332ba03835'
down_revision: Union[str, None] = '7d4a1f9c3b2e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'purchase_orders',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('supplier_id', sa.Integer(), sa.ForeignKey('suppliers.id'), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='DRAFT'),
        sa.Column('order_date', sa.DateTime(), nullable=True),
        sa.Column('memo', sa.String(200), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_table(
        'purchase_order_items',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('purchase_order_id', sa.Integer(), sa.ForeignKey('purchase_orders.id'), nullable=False),
        sa.Column('product_option_id', sa.Integer(), sa.ForeignKey('product_options.id'), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('unit_cost', sa.Numeric(14, 2), nullable=False, server_default='0'),
        sa.Column('received_quantity', sa.Integer(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_table('purchase_order_items')
    op.drop_table('purchase_orders')
