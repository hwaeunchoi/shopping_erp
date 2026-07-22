"""platform_map_and_sku_redesign

ERP 데이터 모델 재설계(2026-07-08 승인):
- product_options.sale_price 추가: 판매가는 옵션(SKU) 단위로 저장한다(Product.base_price가
  아니라 옵션마다 다를 수 있어서).
- product_platform_map.platform_product_code 폐기: platform_option_id(옵션 단위 식별자,
  네이버 channelProductNo)와 항상 같은 값이 중복 저장되던 컬럼이었다. platform_option_id
  하나로 통합하고, 매핑의 유니크 키도 (platform_id, platform_product_code)에서
  (platform_id, platform_option_id)로 이전한다. 기존 행은 모두 platform_option_id에
  이미 platform_product_code와 동일한 값이 채워져 있으므로(과거 등록 로직이 항상 그렇게
  저장했다) 데이터 손실 없이 컬럼만 정리한다.
- product_unmatched_items.platform_product_code -> platform_product_id로 교체: 이전에는
  이 테이블도 platform_product_code(사실상 옵션번호)만 있었다 - 매칭 2순위로 쓸 상품
  단위 식별자(platform_product_id)를 별도 컬럼으로 추가하고, 옵션번호는 기존
  platform_option_id 컬럼을 그대로 쓴다(이미 존재).

Revision ID: 7d4a1f9c3b2e
Revises: ee0fee106bfc
Create Date: 2026-07-08 16:00:00.000000+09:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7d4a1f9c3b2e'
down_revision: Union[str, None] = 'ee0fee106bfc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('product_options') as batch_op:
        batch_op.add_column(sa.Column('sale_price', sa.Numeric(14, 2), nullable=True))

    # platform_option_id는 과거 등록 로직이 항상 platform_product_code와 동일한 값으로
    # 채웠으므로 이 백필은 방어적 조치일 뿐이다(정상적으로는 대상 행이 없어야 한다).
    op.execute("UPDATE product_platform_map SET platform_option_id = platform_product_code WHERE platform_option_id IS NULL")

    with op.batch_alter_table('product_platform_map') as batch_op:
        batch_op.alter_column('platform_option_id', existing_type=sa.String(length=100), nullable=False)
        batch_op.drop_constraint('uq_platform_product_code', type_='unique')
        batch_op.create_unique_constraint('uq_platform_option_id', ['platform_id', 'platform_option_id'])
        batch_op.drop_column('platform_product_code')

    with op.batch_alter_table('product_unmatched_items') as batch_op:
        batch_op.add_column(sa.Column('platform_product_id', sa.String(length=100), nullable=True))
        batch_op.drop_column('platform_product_code')


def downgrade() -> None:
    with op.batch_alter_table('product_unmatched_items') as batch_op:
        batch_op.add_column(sa.Column('platform_product_code', sa.String(length=100), nullable=True))
        batch_op.drop_column('platform_product_id')
    op.execute("UPDATE product_unmatched_items SET platform_product_code = platform_option_id")

    with op.batch_alter_table('product_platform_map') as batch_op:
        batch_op.add_column(sa.Column('platform_product_code', sa.String(length=100), nullable=True))
    op.execute("UPDATE product_platform_map SET platform_product_code = platform_option_id")
    with op.batch_alter_table('product_platform_map') as batch_op:
        batch_op.alter_column('platform_product_code', existing_type=sa.String(length=100), nullable=False)
        batch_op.drop_constraint('uq_platform_option_id', type_='unique')
        batch_op.create_unique_constraint('uq_platform_product_code', ['platform_id', 'platform_product_code'])
        batch_op.alter_column('platform_option_id', existing_type=sa.String(length=100), nullable=True)

    with op.batch_alter_table('product_options') as batch_op:
        batch_op.drop_column('sale_price')
