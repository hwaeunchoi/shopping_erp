"""product_sync_inventory_sale_status

Revision ID: c8d0a24044e4
Revises: 67754b4ac7fb
Create Date: 2026-09-02 16:30:38.249272+09:00

상용 ERP 확장(3단계, 첫 묶음) - 기존 채널 상품(옵션)의 재고 수량/판매상태 전송 스키마만
담는다: 명령(ExternalCommand)의 확정된 목표값 상세 테이블, 그리고 네이버 재고/판매상태
변경 API가 요구하는 원상품번호(originProductNo) 컬럼.

--noqa 참고: autogenerate가 이번 스키마와 무관한 기존 드리프트(orders.assignee_id/
confirmed_by, order_items.channel_product_id의 미명명 FK 누락, product_options/
product_unmatched_items/products 일부 컬럼 길이 차이 - 이전 2단계 마이그레이션들에서도
동일하게 발견돼 제외했던 항목과 같다)도 함께 잡아냈으나, 이번 커밋 범위 밖이므로
포함하지 않고 수동으로 제외했다.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8d0a24044e4"
down_revision: Union[str, None] = "67754b4ac7fb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "product_sync_command_details",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("command_id", sa.Integer(), nullable=False),
        sa.Column("product_platform_map_id", sa.Integer(), nullable=False),
        sa.Column("target_quantity", sa.Integer(), nullable=True),
        sa.Column("target_sale_status", sa.String(length=20), nullable=True),
        sa.ForeignKeyConstraint(["command_id"], ["external_commands.id"]),
        sa.ForeignKeyConstraint(["product_platform_map_id"], ["product_platform_map.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_product_sync_command_detail", "product_sync_command_details", ["command_id"], unique=True)
    op.add_column(
        "product_platform_map", sa.Column("platform_origin_product_id", sa.String(length=100), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("product_platform_map", "platform_origin_product_id")
    op.drop_index("uq_product_sync_command_detail", table_name="product_sync_command_details")
    op.drop_table("product_sync_command_details")
