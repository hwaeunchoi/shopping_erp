"""
models/channel_product.py
---------------------------
채널상품(쇼핑몰에 등록되어 실제로 팔리는 판매 단위)과 그 구성품.

업무 설계상 채널상품은 SKU와 다르다. 세트상품은 채널상품 1개가 SKU 여러 개에
대응하므로, SKU 연결을 컬럼이 아니라 별도 테이블(구성품)로 분리한다.

    channel_products ──1:N──▶ channel_product_components ──▶ product_options(SKU)

- 단품 = 구성품 1행(수량 1). 세트의 특수 케이스가 아니라 부분집합이다.
- 미매칭 = 구성품 0행. 별도 테이블이 아니라 채널상품의 상태로 표현한다.

주의: 이 단계에서는 테이블만 만든다. 기존 product_platform_map에서의 데이터
이관과 코드 전환은 이후 단계에서 백업·건수대조와 함께 수행한다.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, TimestampMixin


class ChannelProduct(Base, TimestampMixin):
    """채널상품. 매칭·세트구성·가격동기화의 주체."""

    __tablename__ = "channel_products"
    __table_args__ = (
        UniqueConstraint("platform_id", "platform_option_id", name="uq_channel_product_option"),
        Index("idx_channel_products_platform", "platform_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    platform_id: Mapped[int] = mapped_column(ForeignKey("platforms.id"), nullable=False)
    # 채널 옵션번호 - 매칭의 기준 키(네이버 channelProductNo 등)
    platform_option_id: Mapped[str] = mapped_column(String(100), nullable=False)
    # 채널 상품번호 - 여러 옵션이 공유 가능(비유니크), 참고용
    platform_product_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    option_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    seller_product_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # 우리가 정한 판매가 - 채널 가격동기화의 원본
    sale_price: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="KRW", nullable=False)

    # 채널에 마지막으로 보낸 값 - 변경분만 전송하기 위한 비교 기준
    # (안 바뀌었는데 매번 보내면 채널 API rate limit에 걸린다)
    last_sent_stock: Mapped[Optional[int]] = mapped_column(nullable=True)
    last_stock_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_sent_price: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    last_price_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    components: Mapped[list["ChannelProductComponent"]] = relationship(
        back_populates="channel_product", cascade="all, delete-orphan"
    )


class ChannelProductComponent(Base):
    """채널상품 구성품. 이 채널상품이 어떤 SKU 몇 개로 이루어지는가."""

    __tablename__ = "channel_product_components"
    __table_args__ = (
        UniqueConstraint("channel_product_id", "product_option_id", name="uq_channel_product_component"),
        Index("idx_channel_product_components_cp", "channel_product_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel_product_id: Mapped[int] = mapped_column(ForeignKey("channel_products.id"), nullable=False)
    product_option_id: Mapped[int] = mapped_column(ForeignKey("product_options.id"), nullable=False)
    # 채널상품 1개 판매 시 차감되는 SKU 수량(단품=1)
    quantity: Mapped[int] = mapped_column(default=1, nullable=False)

    channel_product: Mapped["ChannelProduct"] = relationship(back_populates="components")
