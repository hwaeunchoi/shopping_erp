"""recent_views_unique_constraint

RecentViewRepository.touch()은 (user_id, target_type, target_id) 기준으로
기존 행을 찾아 viewed_at만 갱신하도록 설계되었으나(Favorite의 uq_favorite와
동일한 패턴), recent_views 테이블에는 이를 강제하는 유니크 제약이 없었다.
이로 인해 중복 행이 쌓이면 touch()의 scalar_one_or_none() 조회가
MultipleResultsFound로 500 에러를 일으켰다.

기존 중복 행은 (user_id, target_type, target_id)별로 가장 최근 viewed_at(동률
시 최대 id)만 남기고 정리한 뒤 유니크 인덱스를 생성한다.

Revision ID: c4cd4340574e
Revises: 9a2c5e8b1f47
Create Date: 2026-09-04 15:30:00.000000+09:00

"""
from typing import Sequence, Union

from alembic import op

revision: str = "c4cd4340574e"
down_revision: Union[str, None] = "9a2c5e8b1f47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM recent_views
        WHERE id NOT IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY user_id, target_type, target_id
                    ORDER BY viewed_at DESC, id DESC
                ) AS rn
                FROM recent_views
            ) ranked
            WHERE rn = 1
        )
        """
    )
    op.create_index(
        "uq_recent_view",
        "recent_views",
        ["user_id", "target_type", "target_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_recent_view", table_name="recent_views")
