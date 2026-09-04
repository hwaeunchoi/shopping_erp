"""add_naver_etc_notice_confirmation

Revision ID: 437293bd44be
Revises: 54ea89dcc4c0
Create Date: 2026-09-03 19:00:00.000000+09:00

product_publish_drafts에 네이버 ETC(기타 재화) 상품정보제공고시 카테고리 적합성
"운영자 확인" 감사 기록 컬럼 4개를 추가한다(services.product_publish_service.
ProductPublishService.confirm_etc_notice 참고) - 이 확인은 공식 API가 검증한
결과가 아니라 운영자가 판매자센터에서 직접 확인했다는 기록일 뿐이다. 원시
channel_fields JSON에 categoryNoticeTypeConfirmedByOperator를 직접 써넣는
방식에 의존하지 않도록, 이 컬럼들만 신뢰의 근거로 삼는다(_draft_snapshot이
전송 스냅샷을 만들 때 이 컬럼들로부터 실제 값을 다시 계산해 원시 JSON 값을
덮어쓴다).

- etc_notice_confirmed_by: 확인한 운영자(users.id, FK) - 감사 추적용.
- etc_notice_confirmed_at: 확인 시각.
- etc_notice_confirmed_category_code / etc_notice_confirmed_notice_type:
  확인 시점의 category_code/고시유형 스냅샷 - 이후 카테고리나 고시유형이
  바뀌면(services.product_publish_service.ProductPublishService.save_draft
  참고) 이 값들과 지금 값이 달라지므로 확인이 자동으로 무효화된다.

모두 nullable(기존 행은 미확인 상태로 남는다) - downgrade는 이 4개 컬럼을
그대로 제거한다(데이터 손실: 기존에 기록된 확인 이력이 사라진다 - 이 컬럼들은
감사 기록 전용이라 다른 컬럼이 그 값에 의존하지 않으므로 upgrade/downgrade
왕복 자체는 실패하지 않는다. FK 제약은 PostgreSQL에서 컬럼과 함께 자동으로
제거된다).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "437293bd44be"
down_revision: Union[str, None] = "54ea89dcc4c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "product_publish_drafts",
        sa.Column("etc_notice_confirmed_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
    )
    op.add_column(
        "product_publish_drafts",
        sa.Column("etc_notice_confirmed_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "product_publish_drafts",
        sa.Column("etc_notice_confirmed_category_code", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "product_publish_drafts",
        sa.Column("etc_notice_confirmed_notice_type", sa.String(length=50), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("product_publish_drafts", "etc_notice_confirmed_notice_type")
    op.drop_column("product_publish_drafts", "etc_notice_confirmed_category_code")
    op.drop_column("product_publish_drafts", "etc_notice_confirmed_at")
    op.drop_column("product_publish_drafts", "etc_notice_confirmed_by")
