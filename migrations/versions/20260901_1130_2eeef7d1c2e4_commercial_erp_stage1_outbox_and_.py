"""commercial_erp_stage1_outbox_and_conflicts

Revision ID: 2eeef7d1c2e4
Revises: e5c1a7b93d24
Create Date: 2026-09-01 11:30:35.894695+09:00

상용 ERP 확장(1단계) 공통 기반: 채널 쓰기 명령 outbox(external_commands),
내부/채널 주문상태 충돌 기록(order_status_conflicts), 쿠팡 송장 전송에
필요한 배송묶음 ID(orders.platform_shipment_box_id).

주의: autogenerate가 이 변경과 무관한 항목(FK 제약·컬럼 타입 폭)도 함께
감지했으나 전부 제외했다 - SQLite 리플렉션이 기존 PostgreSQL 운영 스키마와
다르게 보고하는 항목들이며, 이번 변경 대상이 아니다.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2eeef7d1c2e4"
down_revision: Union[str, None] = "e5c1a7b93d24"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "external_commands",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("command_type", sa.String(length=30), nullable=False),
        sa.Column("platform_id", sa.Integer(), nullable=False),
        sa.Column("platform_code", sa.String(length=30), nullable=True),
        sa.Column("target_type", sa.String(length=20), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(length=50), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column("request_summary", sa.String(length=500), nullable=True),
        sa.Column("response_summary", sa.String(length=500), nullable=True),
        sa.Column("trace_id", sa.String(length=36), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["platform_id"], ["platforms.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_external_commands_status", "external_commands", ["status", "next_retry_at"], unique=False)
    op.create_index("idx_external_commands_target", "external_commands", ["target_type", "target_id"], unique=False)
    op.create_index("uq_external_commands_idempotency_key", "external_commands", ["idempotency_key"], unique=True)

    op.create_table(
        "order_status_conflicts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("internal_status", sa.String(length=20), nullable=False),
        sa.Column("channel_status", sa.String(length=20), nullable=False),
        sa.Column("detected_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_by", sa.Integer(), nullable=True),
        sa.Column("resolution", sa.String(length=20), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["resolved_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_order_status_conflicts_order", "order_status_conflicts", ["order_id", "resolved_at"], unique=False
    )

    op.add_column("orders", sa.Column("platform_shipment_box_id", sa.String(length=50), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "platform_shipment_box_id")

    op.drop_index("idx_order_status_conflicts_order", table_name="order_status_conflicts")
    op.drop_table("order_status_conflicts")

    op.drop_index("uq_external_commands_idempotency_key", table_name="external_commands")
    op.drop_index("idx_external_commands_target", table_name="external_commands")
    op.drop_index("idx_external_commands_status", table_name="external_commands")
    op.drop_table("external_commands")
