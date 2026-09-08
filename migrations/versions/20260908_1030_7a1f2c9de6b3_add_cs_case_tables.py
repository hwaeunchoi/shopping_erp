"""add_cs_case_tables

상용 ERP 확장(5단계, B묶음) - CS(고객문의) 케이스 신규 테이블(cs_cases/
cs_case_history). 기존 테이블은 건드리지 않는다(순수 추가) - models.cs_case
참고. 내부 메모/첨부파일 메타데이터는 새 테이블을 만들지 않고 기존
memos/attachments 테이블을 target_type="CS_CASE"로 재사용하므로 이번
마이그레이션에 포함하지 않는다.

autogenerate 대신 손으로 작성했다 - 20260907_1645_4843df3398f0(직전 5-A단계
마이그레이션)와 동일한 이유(이 환경의 SQLite로는 기존 마이그레이션 이력
전체를 처음부터 재생할 수 없어 autogenerate가 비교할 "현재 head 상태의
SQLite DB"를 만들 수 없다). 새 테이블 2개를 추가하는 것뿐이라 손으로
작성해도 모호함이 없다(기존 테이블의 ALTER/DROP이 전혀 없다).

Revision ID: 7a1f2c9de6b3
Revises: 4843df3398f0
Create Date: 2026-09-08 10:30:00.000000+09:00

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "7a1f2c9de6b3"
down_revision: Union[str, None] = "4843df3398f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cs_cases",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("platform_id", sa.Integer(), nullable=True),
        sa.Column("external_inquiry_id", sa.String(length=100), nullable=True),
        sa.Column("external_source", sa.String(length=30), nullable=True),
        sa.Column("external_raw_status", sa.String(length=50), nullable=True),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("order_item_id", sa.Integer(), nullable=True),
        sa.Column("product_option_id", sa.Integer(), nullable=True),
        sa.Column("shipment_id", sa.Integer(), nullable=True),
        sa.Column("fulfillment_batch_item_id", sa.Integer(), nullable=True),
        sa.Column("claim_type", sa.String(length=20), nullable=True),
        sa.Column("claim_id", sa.Integer(), nullable=True),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("inquiry_type", sa.String(length=30), nullable=False),
        sa.Column("priority", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("assignee_id", sa.Integer(), nullable=True),
        sa.Column("subject", sa.String(length=200), nullable=True),
        sa.Column("customer_message", sa.String(length=4000), nullable=False),
        sa.Column("reply_draft", sa.String(length=4000), nullable=True),
        sa.Column("reply_sent_snapshot", sa.String(length=4000), nullable=True),
        sa.Column("reply_sent_at", sa.DateTime(), nullable=True),
        sa.Column("due_at", sa.DateTime(), nullable=True),
        sa.Column("last_customer_message_at", sa.DateTime(), nullable=True),
        sa.Column("last_agent_response_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("reopened_count", sa.Integer(), nullable=False),
        sa.Column("tags", sa.String(length=200), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["assignee_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(["fulfillment_batch_item_id"], ["fulfillment_batch_items.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["order_item_id"], ["order_items.id"]),
        sa.ForeignKeyConstraint(["platform_id"], ["platforms.id"]),
        sa.ForeignKeyConstraint(["product_option_id"], ["product_options.id"]),
        sa.ForeignKeyConstraint(["shipment_id"], ["shipments.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_cs_case_external_inquiry", "cs_cases", ["platform_id", "external_inquiry_id"], unique=True
    )
    op.create_index("idx_cs_cases_status", "cs_cases", ["status"], unique=False)
    op.create_index("idx_cs_cases_assignee", "cs_cases", ["assignee_id"], unique=False)
    op.create_index("idx_cs_cases_due_at", "cs_cases", ["due_at"], unique=False)
    op.create_index("idx_cs_cases_order", "cs_cases", ["order_id"], unique=False)

    op.create_table(
        "cs_case_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("from_value", sa.String(length=50), nullable=True),
        sa.Column("to_value", sa.String(length=50), nullable=True),
        sa.Column("changed_by", sa.Integer(), nullable=True),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("note", sa.String(length=300), nullable=True),
        sa.ForeignKeyConstraint(["case_id"], ["cs_cases.id"]),
        sa.ForeignKeyConstraint(["changed_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_cs_case_history", "cs_case_history", ["case_id", "changed_at"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_cs_case_history", table_name="cs_case_history")
    op.drop_table("cs_case_history")
    op.drop_index("idx_cs_cases_order", table_name="cs_cases")
    op.drop_index("idx_cs_cases_due_at", table_name="cs_cases")
    op.drop_index("idx_cs_cases_assignee", table_name="cs_cases")
    op.drop_index("idx_cs_cases_status", table_name="cs_cases")
    op.drop_index("uq_cs_case_external_inquiry", table_name="cs_cases")
    op.drop_table("cs_cases")
