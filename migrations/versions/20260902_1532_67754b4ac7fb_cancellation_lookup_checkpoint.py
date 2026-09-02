"""cancellation_lookup_checkpoint

Revision ID: 67754b4ac7fb
Revises: 0dcbe421ba67
Create Date: 2026-09-02 15:32:06.211825+09:00

상용 ERP 확장(2단계-A 보완) - "후보 주문 단건 조회" 방식 취소 수집(쿠팡 등, 기간만
으로 대량조회가 안 되는 채널 전용)의 회전식 순회 체크포인트 테이블만 담는다.

--noqa 참고: autogenerate가 이번 스키마와 무관한 기존 드리프트(orders.assignee_id/
confirmed_by, order_items.channel_product_id의 미명명 FK 누락, product_options/
product_unmatched_items/products 일부 컬럼 길이 차이 - 이전 2단계 마이그레이션
(0dcbe421ba67)에서도 동일하게 발견돼 제외했던 항목과 같다)도 함께 잡아냈으나, 이번
커밋 범위 밖이므로 포함하지 않고 수동으로 제외했다.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "67754b4ac7fb"
down_revision: Union[str, None] = "0dcbe421ba67"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "claim_collection_cursors",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("platform_id", sa.Integer(), nullable=False),
        sa.Column("claim_type", sa.String(length=30), nullable=False),
        sa.Column("last_order_id", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["platform_id"], ["platforms.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_claim_collection_cursor", "claim_collection_cursors", ["platform_id", "claim_type"], unique=True
    )


def downgrade() -> None:
    op.drop_index("uq_claim_collection_cursor", table_name="claim_collection_cursors")
    op.drop_table("claim_collection_cursors")
