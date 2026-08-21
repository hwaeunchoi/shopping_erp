"""orders_delivery_message

채널 주문 수집 시 배송지 정보와 함께 배송 요청 메세지를 저장하기 위해
orders.delivery_message 컬럼을 추가한다(수취인명/연락처/우편번호/주소 컬럼은
이미 존재).

Revision ID: d3f5b8a1c9e4
Revises: c7e2a4d9f1b3
Create Date: 2026-08-19 17:00:00.000000+09:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d3f5b8a1c9e4"
down_revision: Union[str, None] = "c7e2a4d9f1b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("delivery_message", sa.String(500), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "delivery_message")
