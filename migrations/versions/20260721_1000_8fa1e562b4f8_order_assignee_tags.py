"""order_assignee_tags

주문 워크벤치(허브) 재설계: 담당자 배정과 태그를 위해 orders에 컬럼 2개를 추가한다.
- assignee_id: 주문을 처리하는 담당 사용자(users FK). 담당자별 분담/필터에 사용.
- tags: 쉼표 구분 태그 문자열(선물포장/재발송 등 운영 라벨). 초기엔 단순 TEXT.

Revision ID: 8fa1e562b4f8
Revises: c4332ba03835
Create Date: 2026-07-21 10:00:00.000000+09:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8fa1e562b4f8"
down_revision: Union[str, None] = "c4332ba03835"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """엔진별로 분기한다.

    PostgreSQL은 ALTER TABLE ADD COLUMN ... REFERENCES를 네이티브 지원하지만,
    SQLite는 FK가 붙은 컬럼 추가에 batch 모드(테이블 재생성)가 필요하다. 그런데
    orders의 기존 FK(platform_id, customer_id)에 이름이 없어 재생성이 실패한다.
    SQLite에서는 FK 없이 컬럼만 추가한다(SQLite는 기본적으로 FK를 강제하지 않고,
    개발/테스트 전용이라 무결성은 애플리케이션 레벨로 충분하다).
    """
    is_sqlite = op.get_bind().dialect.name == "sqlite"
    assignee = (
        sa.Column("assignee_id", sa.Integer(), nullable=True)
        if is_sqlite
        else sa.Column("assignee_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True)
    )
    op.add_column("orders", assignee)
    op.add_column("orders", sa.Column("tags", sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "tags")
    op.drop_column("orders", "assignee_id")
