"""backup_history_trigger_type

Revision ID: 560dd2f4b2ba
Revises: 54810f29bf6a
Create Date: 2026-09-14 01:00:00+09:00

재기동 후 놓친 예약 백업 보충(fix/postgres-backup-missed-run-recovery) 지원을
위해 backup_history에 nullable 컬럼 하나를 추가한다: trigger_type(SCHEDULE/
CATCHUP/MANUAL - task_execution_history.trigger_type과 같은 값 집합).
기존 행은 전부 NULL로 남는다(데이터 손실/변형 없음) - 이 마이그레이션 이전에
쌓인 백업이 어떤 경로로 만들어졌는지는 소급 판단하지 않는다(추측 금지 -
services/postgres_backup_service.py 모듈 docstring 참고).

⚠️ 이 마이그레이션은 이 브랜치에서 격리 PostgreSQL로만 검증했다 - 운영 DB에는
적용하지 않았다(운영 배포는 별도 승인 절차를 거친다).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "560dd2f4b2ba"
down_revision: Union[str, None] = "54810f29bf6a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("backup_history", sa.Column("trigger_type", sa.String(length=10), nullable=True))


def downgrade() -> None:
    op.drop_column("backup_history", "trigger_type")
