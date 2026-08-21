"""widen_marketplace_text_columns

실제 마켓(쿠팡 등) 데이터가 기존 VARCHAR 길이를 초과해 Postgres에서 삽입이
실패하는 문제를 해결한다(예: 쿠팡 옵션명 63자 > option_name VARCHAR(50)).
SQLite는 VARCHAR 길이를 강제하지 않아 개발/테스트에서는 드러나지 않았고,
Postgres(운영)에서만 발생했다.

변경 내용(무손실 확장만)
  product_options.option_name          VARCHAR(50)  -> (255)
  product_unmatched_items.option_name  VARCHAR(50)  -> (255)
  product_unmatched_items.product_name VARCHAR(200) -> (500)
  products.category                    VARCHAR(50)  -> (100)

SQLite 주의
  SQLite는 VARCHAR(n)의 n을 무시하므로(길이 강제 없음) 이 마이그레이션은
  Postgres 등에서만 실제 ALTER를 수행하고 SQLite에서는 no-op으로 둔다.
  (batch_alter_table로 테이블을 재생성하면 이름 없는 FK 때문에 오류가 나므로,
   기능상 불필요한 SQLite 변경은 아예 하지 않는다.)

Revision ID: c7e2a4d9f1b3
Revises: 24f6e457d1dc
Create Date: 2026-08-19 15:00:00.000000+09:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c7e2a4d9f1b3"
down_revision: Union[str, None] = "24f6e457d1dc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, column, new_len, old_len)
_WIDENINGS = [
    ("product_options", "option_name", 255, 50),
    ("product_unmatched_items", "option_name", 255, 50),
    ("product_unmatched_items", "product_name", 500, 200),
    ("products", "category", 100, 50),
]


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    if _is_sqlite():
        return  # SQLite는 VARCHAR 길이를 강제하지 않아 변경 불필요
    for table, column, new_len, _old_len in _WIDENINGS:
        op.alter_column(table, column, type_=sa.String(new_len), existing_type=sa.String(_old_len))


def downgrade() -> None:
    if _is_sqlite():
        return
    # 축소는 데이터 잘림 위험이 있으나, 확장 이후 축소이므로 원래 길이로 되돌린다.
    for table, column, new_len, old_len in _WIDENINGS:
        op.alter_column(table, column, type_=sa.String(old_len), existing_type=sa.String(new_len))
