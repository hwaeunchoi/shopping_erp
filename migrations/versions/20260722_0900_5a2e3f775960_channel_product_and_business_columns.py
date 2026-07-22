"""channel_product_and_business_columns

업무 설계 확정 후 1단계 — 신규 테이블 2종 + 업무 컬럼 추가.
데이터 이관은 하지 않는다(product_platform_map 이관은 별도 단계).

신규 테이블
  - channel_products            채널상품(매칭·세트·가격동기화의 주체)
  - channel_product_components  세트 구성(채널상품 → SKU N개 + 수량)

컬럼 추가
  - orders       currency, order_source, confirmed_at, confirmed_by, receiver_*
  - order_items  channel_product_id, set_quantity, currency
  - inventory    stock_status
  - audit_logs   reason, command

SQLite 주의
  orders/order_items에는 이름 없는 FK가 있어, FK가 붙은 컬럼을 batch 모드로
  추가하면 테이블 재생성 중 "Constraint must have a name" 오류가 난다.
  따라서 FK 컬럼은 엔진별로 분기한다 - PostgreSQL은 네이티브 FK를 만들고,
  SQLite는 FK 없이 컬럼만 추가한다(SQLite는 기본적으로 FK를 강제하지 않으며
  개발/테스트 전용이므로 무결성은 애플리케이션 레벨로 충분하다).

Revision ID: 5a2e3f775960
Revises: efc947fe91e2
Create Date: 2026-07-22 09:00:00.000000+09:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "5a2e3f775960"
down_revision: Union[str, None] = "efc947fe91e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def _fk_column(name: str, target: str) -> sa.Column:
    """FK 컬럼을 엔진에 맞게 만든다(위 docstring의 SQLite 제약 참고)."""
    if _is_sqlite():
        return sa.Column(name, sa.Integer(), nullable=True)
    return sa.Column(name, sa.Integer(), sa.ForeignKey(target), nullable=True)


def upgrade() -> None:
    # --- 신규 테이블 ---------------------------------------------------------
    op.create_table(
        "channel_products",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("platform_id", sa.Integer(), sa.ForeignKey("platforms.id"), nullable=False),
        sa.Column("platform_option_id", sa.String(100), nullable=False),
        sa.Column("platform_product_id", sa.String(100), nullable=True),
        sa.Column("display_name", sa.String(200), nullable=True),
        sa.Column("option_name", sa.String(100), nullable=True),
        sa.Column("seller_product_code", sa.String(100), nullable=True),
        sa.Column("sale_price", sa.Numeric(14, 2), nullable=True),
        sa.Column("currency", sa.String(3), nullable=False, server_default="KRW"),
        sa.Column("last_sent_stock", sa.Integer(), nullable=True),
        sa.Column("last_stock_sent_at", sa.DateTime(), nullable=True),
        sa.Column("last_sent_price", sa.Numeric(14, 2), nullable=True),
        sa.Column("last_price_sent_at", sa.DateTime(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("platform_id", "platform_option_id", name="uq_channel_product_option"),
    )
    op.create_index("idx_channel_products_platform", "channel_products", ["platform_id"])

    op.create_table(
        "channel_product_components",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("channel_product_id", sa.Integer(), sa.ForeignKey("channel_products.id"), nullable=False),
        sa.Column("product_option_id", sa.Integer(), sa.ForeignKey("product_options.id"), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("channel_product_id", "product_option_id", name="uq_channel_product_component"),
    )
    op.create_index("idx_channel_product_components_cp", "channel_product_components", ["channel_product_id"])

    # --- orders ------------------------------------------------------------
    op.add_column("orders", sa.Column("currency", sa.String(3), nullable=False, server_default="KRW"))
    op.add_column("orders", sa.Column("order_source", sa.String(20), nullable=False, server_default="CHANNEL"))
    op.add_column("orders", sa.Column("confirmed_at", sa.DateTime(), nullable=True))
    op.add_column("orders", _fk_column("confirmed_by", "users.id"))
    op.add_column("orders", sa.Column("receiver_name", sa.String(50), nullable=True))
    op.add_column("orders", sa.Column("receiver_phone", sa.String(20), nullable=True))
    op.add_column("orders", sa.Column("receiver_zipcode", sa.String(10), nullable=True))
    op.add_column("orders", sa.Column("receiver_address", sa.String(500), nullable=True))

    # --- order_items -------------------------------------------------------
    op.add_column("order_items", _fk_column("channel_product_id", "channel_products.id"))
    op.add_column("order_items", sa.Column("set_quantity", sa.Integer(), nullable=True))
    op.add_column("order_items", sa.Column("currency", sa.String(3), nullable=False, server_default="KRW"))

    # --- inventory ---------------------------------------------------------
    op.add_column("inventory", sa.Column("stock_status", sa.String(20), nullable=False, server_default="SELLABLE"))

    # --- audit_logs --------------------------------------------------------
    op.add_column("audit_logs", sa.Column("reason", sa.String(500), nullable=True))
    op.add_column("audit_logs", sa.Column("command", sa.String(50), nullable=True))


def downgrade() -> None:
    op.drop_column("audit_logs", "command")
    op.drop_column("audit_logs", "reason")

    op.drop_column("inventory", "stock_status")

    op.drop_column("order_items", "currency")
    op.drop_column("order_items", "set_quantity")
    op.drop_column("order_items", "channel_product_id")

    for col in (
        "receiver_address",
        "receiver_zipcode",
        "receiver_phone",
        "receiver_name",
        "confirmed_by",
        "confirmed_at",
        "order_source",
        "currency",
    ):
        op.drop_column("orders", col)

    op.drop_index("idx_channel_product_components_cp", table_name="channel_product_components")
    op.drop_table("channel_product_components")
    op.drop_index("idx_channel_products_platform", table_name="channel_products")
    op.drop_table("channel_products")
