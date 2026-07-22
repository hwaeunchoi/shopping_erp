"""product_management_overhaul

- products.brand/manufacturer: 네이버 자동등록 시 채울 수 있도록 준비하는 필드
  (실제 네이버 주문 API 응답에는 브랜드/제조사 정보가 없어 현재는 사용자가 직접
  입력해야 하며, 향후 별도 상품상세 API 연동 시 자동으로 채워질 수 있다).
- product_options.sort_order: 옵션 목록 순서 변경 기능을 위한 컬럼.
- product_platform_map.platform_product_id: 플랫폼의 "상품번호"(예: 네이버 productId).
  기존 platform_product_code는 "옵션번호"(예: 네이버 itemNo)로 매핑 키에 계속 사용하고,
  이 컬럼은 참고 정보로만 저장한다(여러 옵션이 같은 상품번호를 공유할 수 있어 유니크 아님).
- product_unmatched_items: 자동매칭 4단계가 모두 실패한 주문상품을 기록해 사용자가
  상품관리 화면에서 직접 기존 상품에 연결(수동 매칭)할 수 있게 하는 신규 테이블.

Revision ID: 1ca10e435550
Revises: e98d9f0aef9f
Create Date: 2026-07-07 13:38:37.663236+09:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1ca10e435550'
down_revision: Union[str, None] = 'e98d9f0aef9f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('products') as batch_op:
        batch_op.add_column(sa.Column('brand', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('manufacturer', sa.String(length=100), nullable=True))

    with op.batch_alter_table('product_options') as batch_op:
        batch_op.add_column(sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'))

    with op.batch_alter_table('product_platform_map') as batch_op:
        batch_op.add_column(sa.Column('platform_product_id', sa.String(length=100), nullable=True))

    op.create_table(
        'product_unmatched_items',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('platform_id', sa.Integer(), sa.ForeignKey('platforms.id'), nullable=False),
        sa.Column('platform_order_no', sa.String(length=100), nullable=True),
        sa.Column('platform_product_code', sa.String(length=100), nullable=True),
        sa.Column('product_name', sa.String(length=200), nullable=True),
        sa.Column('option_name', sa.String(length=50), nullable=True),
        sa.Column('seller_product_code', sa.String(length=100), nullable=True),
        sa.Column('quantity', sa.Integer(), nullable=True),
        sa.Column('unit_price', sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='PENDING'),
        sa.Column('resolved_option_id', sa.Integer(), sa.ForeignKey('product_options.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('product_unmatched_items')

    with op.batch_alter_table('product_platform_map') as batch_op:
        batch_op.drop_column('platform_product_id')

    with op.batch_alter_table('product_options') as batch_op:
        batch_op.drop_column('sort_order')

    with op.batch_alter_table('products') as batch_op:
        batch_op.drop_column('manufacturer')
        batch_op.drop_column('brand')
