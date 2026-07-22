"""product_sync_refactor_fields

- product_images.product_option_id: 옵션(SKU) 전용 이미지를 상품 전체 이미지와
  구분해 저장하기 위한 컬럼(NULL이면 상품 대표/추가이미지).
- product_platform_map.platform_option_id: platform_product_code(매핑 키)와
  동일한 값을 "옵션번호"라는 의미가 드러나는 이름으로도 저장한다.
- product_unmatched_items: platform_option_id 추가, resolved_option_id/resolved_at를
  matched_option_id/matched_at로 이름 변경, matched_by 추가(누가/무엇이 연결했는지).

Revision ID: ee0fee106bfc
Revises: 1ca10e435550
Create Date: 2026-07-07 15:40:32.701052+09:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ee0fee106bfc'
down_revision: Union[str, None] = '1ca10e435550'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('product_images') as batch_op:
        batch_op.add_column(sa.Column('product_option_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_product_images_product_option_id', 'product_options', ['product_option_id'], ['id']
        )

    with op.batch_alter_table('product_platform_map') as batch_op:
        batch_op.add_column(sa.Column('platform_option_id', sa.String(length=100), nullable=True))

    with op.batch_alter_table('product_unmatched_items') as batch_op:
        batch_op.add_column(sa.Column('platform_option_id', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('matched_by', sa.String(length=20), nullable=True))
        batch_op.alter_column('resolved_option_id', new_column_name='matched_option_id')
        batch_op.alter_column('resolved_at', new_column_name='matched_at')

    op.execute("UPDATE product_unmatched_items SET status = 'MATCHED' WHERE status = 'RESOLVED'")


def downgrade() -> None:
    op.execute("UPDATE product_unmatched_items SET status = 'RESOLVED' WHERE status = 'MATCHED'")

    with op.batch_alter_table('product_unmatched_items') as batch_op:
        batch_op.alter_column('matched_at', new_column_name='resolved_at')
        batch_op.alter_column('matched_option_id', new_column_name='resolved_option_id')
        batch_op.drop_column('matched_by')
        batch_op.drop_column('platform_option_id')

    with op.batch_alter_table('product_platform_map') as batch_op:
        batch_op.drop_column('platform_option_id')

    with op.batch_alter_table('product_images') as batch_op:
        batch_op.drop_constraint('fk_product_images_product_option_id', type_='foreignkey')
        batch_op.drop_column('product_option_id')
