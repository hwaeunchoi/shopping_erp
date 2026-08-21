"""order_item_platform_order_item_no

주문상품(OrderItem)에 쇼핑몰 상품주문번호(라인 단위 외부 식별자, 네이버
productOrderId 등)를 저장할 컬럼과 유일성 인덱스를 추가한다.

- order_items.platform_order_item_no : VARCHAR(100) NULL
- UNIQUE INDEX (order_id, platform_order_item_no)
  · NULL은 여러 개 허용(상품주문번호 미제공 채널/과거 데이터)
  · 유니크 "인덱스"로 구현해 SQLite 테이블 재생성(이름 없는 FK 충돌)을 피하고
    PostgreSQL·SQLite 양쪽에서 동일하게 동작하게 한다.

이 마이그레이션은 컬럼/인덱스만 추가하며 기존 데이터를 삭제하거나 백필하지 않는다.
백필은 별도 idempotent 스크립트(scripts/backfill_naver_product_order_no.py)로 수행한다.

Revision ID: e5c1a7b93d24
Revises: d3f5b8a1c9e4
Create Date: 2026-08-20 10:00:00.000000+09:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5c1a7b93d24"
down_revision: Union[str, None] = "d3f5b8a1c9e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = "uq_order_item_platform_no"


def upgrade() -> None:
    op.add_column("order_items", sa.Column("platform_order_item_no", sa.String(100), nullable=True))
    # 유니크 인덱스(양 엔진 동일, NULL 다중 허용). 테이블 재생성 없이 생성 가능.
    op.create_index(_INDEX, "order_items", ["order_id", "platform_order_item_no"], unique=True)


def downgrade() -> None:
    # 이번 리비전이 만든 인덱스·컬럼만 제거(기존 데이터/스키마는 건드리지 않음).
    op.drop_index(_INDEX, table_name="order_items")
    op.drop_column("order_items", "platform_order_item_no")
