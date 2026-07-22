"""widen settlement_cycle to varchar30

settlements.settlement_cycle이 "YYYY-MM-DD~YYYY-MM-DD"(21자) 형식으로 저장되는데
컬럼이 VARCHAR(20)이라 PostgreSQL에서 StringDataRightTruncation 오류가 발생했다
(SQLite는 길이 제약을 강제하지 않아 개발 중에는 드러나지 않았던 결함).

Revision ID: 446863789c9b
Revises: 91b961bb3be8
Create Date: 2026-07-06 16:40:57.006850+09:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '446863789c9b'
down_revision: Union[str, None] = '91b961bb3be8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch_alter_table을 쓰는 이유: SQLite는 ALTER COLUMN ... TYPE을 지원하지
    # 않는다(PostgreSQL은 직접 지원하지만, 로컬 개발 DB는 SQLite이므로 두 DB
    # 모두에서 동작하도록 batch 모드로 작성한다 - SQLite에서는 임시 테이블을
    # 만들어 복사하는 방식으로, PostgreSQL에서는 일반 ALTER COLUMN으로 동작한다).
    with op.batch_alter_table('settlements') as batch_op:
        batch_op.alter_column(
            'settlement_cycle',
            existing_type=sa.String(length=20),
            type_=sa.String(length=30),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table('settlements') as batch_op:
        batch_op.alter_column(
            'settlement_cycle',
            existing_type=sa.String(length=30),
            type_=sa.String(length=20),
            existing_nullable=False,
        )
