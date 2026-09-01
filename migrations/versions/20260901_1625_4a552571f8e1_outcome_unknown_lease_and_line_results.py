"""outcome_unknown_lease_and_line_results

Revision ID: 4a552571f8e1
Revises: f36aaeeede49
Create Date: 2026-09-01 16:25:52.906334+09:00

1단계 완결 검토("결과 불명 요청의 중복 송장 전송" 위험) 대응:
- external_commands.lease_token: worker가 명령을 원자적으로 선점(claim)할 때 발급하는
  소유권 토큰 - 두 worker의 동시 실행 방지, 소유권을 잃은 worker의 뒤늦은 결과 반영 방지.
- external_command_line_results: 여러 라인을 전송하는 명령에서 라인별 성공/실패를
  기록 - 부분성공 시 이미 성공한 라인을 재시도에서 제외하기 위한 근거 테이블.
  (status 컬럼에 새 값 "UNKNOWN"이 추가됐지만 문자열 컬럼이라 스키마 변경은 없다.)

주의: autogenerate가 이 변경과 무관한 항목(FK 제약·컬럼 타입 폭)도 함께 감지했으나
이전 마이그레이션들과 동일하게 전부 제외했다 - SQLite 리플렉션이 기존 PostgreSQL
운영 스키마와 다르게 보고하는 항목들이며 이번 변경 대상이 아니다.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4a552571f8e1"
down_revision: Union[str, None] = "f36aaeeede49"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "external_command_line_results",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("command_id", sa.Integer(), nullable=False),
        sa.Column("order_item_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("result_code", sa.String(length=50), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["command_id"], ["external_commands.id"]),
        sa.ForeignKeyConstraint(["order_item_id"], ["order_items.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("command_id", "order_item_id", name="uq_command_line_result"),
    )
    op.create_index(
        "idx_command_line_results_order_item",
        "external_command_line_results",
        ["order_item_id", "status"],
        unique=False,
    )
    op.add_column("external_commands", sa.Column("lease_token", sa.String(length=36), nullable=True))


def downgrade() -> None:
    op.drop_column("external_commands", "lease_token")
    op.drop_index("idx_command_line_results_order_item", table_name="external_command_line_results")
    op.drop_table("external_command_line_results")
