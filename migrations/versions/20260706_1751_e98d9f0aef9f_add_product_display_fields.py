"""add product display fields

- product_options.unit_cost_price: 매입원가(단가) 현재값. product_cost_history(이력
  관리)와는 별개 - 화면에서 바로 확인/수정하는 용도.
- product_platform_map.display_name: 쇼핑몰 노출상품명(플랫폼마다 다를 수 있음).
- product_platform_map.seller_product_code: 판매자상품코드(플랫폼마다 다를 수 있음).

Revision ID: e98d9f0aef9f
Revises: 446863789c9b
Create Date: 2026-07-06 17:51:32.064096+09:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e98d9f0aef9f'
down_revision: Union[str, None] = '446863789c9b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('product_options') as batch_op:
        batch_op.add_column(sa.Column('unit_cost_price', sa.Numeric(precision=14, scale=2), nullable=True))

    with op.batch_alter_table('product_platform_map') as batch_op:
        batch_op.add_column(sa.Column('display_name', sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column('seller_product_code', sa.String(length=100), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('product_platform_map') as batch_op:
        batch_op.drop_column('seller_product_code')
        batch_op.drop_column('display_name')

    with op.batch_alter_table('product_options') as batch_op:
        batch_op.drop_column('unit_cost_price')
