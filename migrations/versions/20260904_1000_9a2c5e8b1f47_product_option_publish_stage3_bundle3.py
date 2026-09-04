"""product_option_publish_stage3_bundle3

Revision ID: 9a2c5e8b1f47
Revises: 437293bd44be
Create Date: 2026-09-04 10:00:00.000000+09:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9a2c5e8b1f47'
down_revision: Union[str, None] = '437293bd44be'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('product_publish_option_group_drafts',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=False),
    sa.Column('platform_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=True),
    sa.Column('description_html', sa.Text(), nullable=True),
    sa.Column('category_code', sa.String(length=50), nullable=True),
    sa.Column('image_urls_json', sa.Text(), nullable=True),
    sa.Column('base_sale_price', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('channel_fields_json', sa.Text(), nullable=True),
    sa.Column('channel_product_id', sa.String(length=100), nullable=True),
    sa.Column('channel_option_id', sa.String(length=100), nullable=True),
    sa.Column('registered_at', sa.DateTime(), nullable=True),
    sa.Column('etc_notice_confirmed_by', sa.Integer(), nullable=True),
    sa.Column('etc_notice_confirmed_at', sa.DateTime(), nullable=True),
    sa.Column('etc_notice_confirmed_category_code', sa.String(length=50), nullable=True),
    sa.Column('etc_notice_confirmed_notice_type', sa.String(length=50), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['etc_notice_confirmed_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['platform_id'], ['platforms.id'], ),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('product_id', 'platform_id', name='uq_product_publish_option_group_draft')
    )
    op.create_table('product_publish_option_group_item_drafts',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('group_draft_id', sa.Integer(), nullable=False),
    sa.Column('product_option_id', sa.Integer(), nullable=False),
    sa.Column('option_values_json', sa.Text(), nullable=True),
    sa.Column('seller_product_code', sa.String(length=100), nullable=True),
    sa.Column('sale_price', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('stock_quantity', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['group_draft_id'], ['product_publish_option_group_drafts.id'], ),
    sa.ForeignKeyConstraint(['product_option_id'], ['product_options.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('group_draft_id', 'product_option_id', name='uq_product_publish_option_group_item')
    )
    op.create_table('product_option_publish_command_details',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('command_id', sa.Integer(), nullable=False),
    sa.Column('group_draft_id', sa.Integer(), nullable=False),
    sa.Column('snapshot_json', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['command_id'], ['external_commands.id'], ),
    sa.ForeignKeyConstraint(['group_draft_id'], ['product_publish_option_group_drafts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(
        'uq_product_option_publish_command_detail',
        'product_option_publish_command_details',
        ['command_id'],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index('uq_product_option_publish_command_detail', table_name='product_option_publish_command_details')
    op.drop_table('product_option_publish_command_details')
    op.drop_table('product_publish_option_group_item_drafts')
    op.drop_table('product_publish_option_group_drafts')
