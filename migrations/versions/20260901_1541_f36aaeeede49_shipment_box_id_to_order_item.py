"""shipment_box_id_to_order_item

Revision ID: f36aaeeede49
Revises: 2eeef7d1c2e4
Create Date: 2026-09-01 15:41:49.880127+09:00

쿠팡 배송묶음 ID(shipmentBoxId)를 orders(대표값 1개)에서 order_items(라인별 실값)로
옮긴다 - 쿠팡 응답은 배송묶음(box) 단위이고 한 주문이 여러 배송묶음으로 나뉠 수
있어(CoupangConnector 모듈 docstring 참고), 주문 전체 대표값 하나만 저장하면
분할배송 주문의 두 번째 이후 배송묶음 라인에 잘못된 box id로 송장이 전송되는
오류가 생긴다(1단계 완결 검토에서 발견).

기존 orders.platform_shipment_box_id 값을 잃지 않도록, 컬럼을 드롭하기 전에
그 값을 해당 주문의 모든 order_items 행(아직 값이 없는 것만)으로 백필한다 -
어느 라인이 실제로 그 배송묶음에 속했는지는 과거 데이터로는 알 수 없으므로
근사치(주문 전체에 동일값 적용)이며, 이 한계는 코드 주석/로드맵 문서에도 남겨둔다.

주의: autogenerate가 이 변경과 무관한 항목(FK 제약·컬럼 타입 폭)도 함께
감지했으나 전부 제외했다 - 이전 마이그레이션(2eeef7d1c2e4)과 동일하게, SQLite
리플렉션이 기존 PostgreSQL 운영 스키마와 다르게 보고하는 항목들이며 이번 변경
대상이 아니다.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f36aaeeede49"
down_revision: Union[str, None] = "2eeef7d1c2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("order_items", sa.Column("platform_shipment_box_id", sa.String(length=50), nullable=True))

    # 백필: 기존 orders.platform_shipment_box_id 값을 그 주문의 아직 값이 없는
    # order_items 행 전체에 복사한다(컬럼을 드롭하기 전 손실 방지 - 근사치, 위 docstring 참고).
    op.execute(
        """
        UPDATE order_items
        SET platform_shipment_box_id = (
            SELECT orders.platform_shipment_box_id FROM orders WHERE orders.id = order_items.order_id
        )
        WHERE order_items.platform_shipment_box_id IS NULL
          AND EXISTS (
              SELECT 1 FROM orders
              WHERE orders.id = order_items.order_id AND orders.platform_shipment_box_id IS NOT NULL
          )
        """
    )

    op.drop_column("orders", "platform_shipment_box_id")


def downgrade() -> None:
    op.add_column("orders", sa.Column("platform_shipment_box_id", sa.String(length=50), nullable=True))

    # 역백필: 주문당 대표값 1개만 복원 가능하다(원래 설계의 한계) - 그 주문의
    # order_items 중 값이 있는 첫 행을 대표값으로 사용한다(손실 있는 다운그레이드).
    op.execute(
        """
        UPDATE orders
        SET platform_shipment_box_id = (
            SELECT oi.platform_shipment_box_id FROM order_items oi
            WHERE oi.order_id = orders.id AND oi.platform_shipment_box_id IS NOT NULL
            ORDER BY oi.id
            LIMIT 1
        )
        WHERE EXISTS (
            SELECT 1 FROM order_items oi
            WHERE oi.order_id = orders.id AND oi.platform_shipment_box_id IS NOT NULL
        )
        """
    )

    op.drop_column("order_items", "platform_shipment_box_id")
