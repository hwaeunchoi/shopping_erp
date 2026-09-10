"""backup_history_postgres_columns

Revision ID: 54810f29bf6a
Revises: 0b2fdb82091c
Create Date: 2026-09-10 10:00:00+09:00

PostgreSQL 예약 백업(feature/postgres-scheduled-backup) 지원을 위해
backup_history에 컬럼 4개를 추가한다: engine(SQLITE/POSTGRES),
sha256(dump 파일 SHA-256 hex), error_code(안전한 오류 코드 - DUMP_TIMEOUT 등),
retained_count(이번 실행 뒤 보존된 백업 개수). 전부 nullable이라 기존
SQLite 백업 경로(scheduler/jobs/backup_job.py의 _run_sqlite_backup)는 이
컬럼들을 전혀 채우지 않고, 기존에 쌓인 행도 그대로 NULL로 남는다(데이터
손실/변형 없음). status 컬럼 자체는 이미 자유 문자열(VARCHAR(20), CHECK
제약 없음)이라 PARTIAL_SUCCESS/ALREADY_RUNNING 신규 값 저장에 스키마 변경이
필요 없다 - services/postgres_backup_service.py 모듈 docstring 참고.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '54810f29bf6a'
down_revision: Union[str, None] = '0b2fdb82091c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('backup_history', sa.Column('engine', sa.String(length=10), nullable=True))
    op.add_column('backup_history', sa.Column('sha256', sa.String(length=64), nullable=True))
    op.add_column('backup_history', sa.Column('error_code', sa.String(length=40), nullable=True))
    op.add_column('backup_history', sa.Column('retained_count', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('backup_history', 'retained_count')
    op.drop_column('backup_history', 'error_code')
    op.drop_column('backup_history', 'sha256')
    op.drop_column('backup_history', 'engine')
