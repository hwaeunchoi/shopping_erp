"""
models/product.py
-------------------
ERD 2.4 상품/공급처/재고 그룹 중 상품 관련:
products, product_options(SKU), product_images, product_platform_map,
product_cost_history

핵심 설계 포인트(ERD 원칙 2): product_cost_history는 원가 변경 이력을
시점(effective_from~effective_to)으로 관리하고, 실제 판매 시점의 원가는
order_items.cost_price_snapshot에 스냅샷으로 확정되어 이후 원가가
바뀌어도 과거 손익이 흔들리지 않는다.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, SoftDeleteMixin, TimestampMixin, utcnow


class Product(Base, TimestampMixin, SoftDeleteMixin):
    """상품 마스터."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    brand: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    manufacturer: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    base_price: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", nullable=False)  # ACTIVE/DISCONTINUED

    options: Mapped[list["ProductOption"]] = relationship(back_populates="product", cascade="all, delete-orphan")
    images: Mapped[list["ProductImage"]] = relationship(back_populates="product", cascade="all, delete-orphan")


class ProductOption(Base):
    """상품 옵션 = SKU. 색상/사이즈 등 옵션 조합 단위로 재고·주문·원가를 추적한다."""

    __tablename__ = "product_options"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    option_name: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    color: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    size: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    # ERP 내부 식별자 - 판매자상품코드/플랫폼코드와 절대 동일시하지 않는다(seller_product_code는
    # product_platform_map에 플랫폼별로 별도 저장). 신규 옵션 등록 시 서비스 계층에서
    # "SKU-{option.id:06d}" 형식으로 자체 채번한다 - 어떤 플랫폼 값에도 의존하지 않는다.
    sku_code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    barcode: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # 단가(매입원가) - product_cost_history(이력 관리, 판매시점 원가 스냅샷 근거)와는 별개로
    # 화면에서 바로 확인/수정할 수 있는 "현재값"만 담는다(이력 관리 없음).
    unit_cost_price: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    # 판매가 - 옵션(SKU)마다 다를 수 있어 Product.base_price가 아니라 옵션 단위로 저장한다.
    # 네이버 상품 동기화 시 channelProduct.salePrice로 채워진다.
    sale_price: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 상품 상세 화면의 옵션 목록 순서 변경(드래그 정렬 등)에 사용 - 값이 작을수록 먼저 표시.
    sort_order: Mapped[int] = mapped_column(default=0, nullable=False)

    product: Mapped["Product"] = relationship(back_populates="options")
    platform_maps: Mapped[list["ProductPlatformMap"]] = relationship(
        back_populates="product_option", cascade="all, delete-orphan"
    )
    cost_history: Mapped[list["ProductCostHistory"]] = relationship(
        back_populates="product_option", cascade="all, delete-orphan"
    )


class ProductImage(Base):
    """상품 이미지. is_thumbnail=1 인 이미지를 주문/교환/반품 화면 썸네일로 사용한다.

    product_option_id가 NULL이면 상품 전체 대표/추가이미지, 값이 있으면 특정
    옵션(SKU) 전용 이미지(옵션이미지) - 네이버 상품 동기화 시 옵션별 이미지가
    있으면 이 필드로 구분해 저장한다.
    """

    __tablename__ = "product_images"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    product_option_id: Mapped[Optional[int]] = mapped_column(ForeignKey("product_options.id"), nullable=True)
    image_url: Mapped[str] = mapped_column(String(255), nullable=False)
    is_thumbnail: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(default=0, nullable=False)

    product: Mapped["Product"] = relationship(back_populates="images")
    product_option: Mapped[Optional["ProductOption"]] = relationship()


class ProductPlatformMap(Base):
    """SKU(ProductOption) ↔ 플랫폼별 식별자 매핑. 동일 SKU가 플랫폼마다 다른 코드로
    등록되어도 내부적으로 하나로 관리한다.

    플랫폼마다 "상품번호"(상품/그룹 단위)와 "옵션번호"(옵션/채널상품 단위) 두 식별자가
    모두 존재한다(네이버: productId/groupProductNo vs itemNo/channelProductNo). 이 둘의
    의미가 다르므로 컬럼도 분리해서 저장한다:

    - platform_option_id: 옵션(ProductOption) 단위 식별자(네이버 channelProductNo).
      하나의 옵션 = 하나의 플랫폼 옵션번호이므로 매핑의 유니크 키로 쓴다.
    - platform_product_id: 상품(Product) 단위 식별자(네이버 groupProductNo). 한 상품의
      여러 옵션이 같은 값을 공유할 수 있어(비유니크) 매칭 2순위/참고 정보로만 쓴다.

    과거에는 platform_product_code(옵션번호, 매핑 키)와 platform_option_id가 등록 시
    항상 같은 값으로 중복 저장되어 의미 없는 컬럼이 하나 더 있었다 - platform_option_id
    하나로 통합하고 platform_product_code는 폐기했다.
    """

    __tablename__ = "product_platform_map"
    __table_args__ = (UniqueConstraint("platform_id", "platform_option_id", name="uq_platform_option_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_option_id: Mapped[int] = mapped_column(ForeignKey("product_options.id"), nullable=False)
    platform_id: Mapped[int] = mapped_column(ForeignKey("platforms.id"), nullable=False)
    # 옵션(ProductOption) 단위 식별자 - 네이버 channelProductNo. 매핑의 기준 키(유니크 제약).
    platform_option_id: Mapped[str] = mapped_column(String(100), nullable=False)
    # 상품(Product) 단위 식별자 - 네이버 groupProductNo. 여러 옵션이 공유 가능(비유니크),
    # 자동매칭 2순위 및 참고 정보로만 사용한다.
    platform_product_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # 동일 SKU라도 플랫폼마다 노출상품명/판매자상품코드가 다를 수 있어 매핑 단위로 저장한다.
    display_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)  # 쇼핑몰 노출상품명
    # 판매자상품코드(네이버 sellerManagementCode 등) - ERP 내부 SKU(ProductOption.sku_code)와는
    # 완전히 별개다. SKU는 ERP 자체 채번값이고, 이 값은 플랫폼에 등록된 셀러 관리코드다.
    seller_product_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)  # 판매자상품코드

    product_option: Mapped["ProductOption"] = relationship(back_populates="platform_maps")
    platform: Mapped["Platform"] = relationship()  # type: ignore[name-defined]  # 순환참조 방지용 지연 문자열 참조 (models.platform.Platform)


class ProductCostHistory(Base):
    """SKU별 원가/매입가 변경 이력. 판매 시점의 원가를 조회하는 근거 테이블."""

    __tablename__ = "product_cost_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_option_id: Mapped[int] = mapped_column(ForeignKey("product_options.id"), nullable=False)
    supplier_id: Mapped[Optional[int]] = mapped_column(ForeignKey("suppliers.id"), nullable=True)
    cost_price: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    effective_to: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)

    product_option: Mapped["ProductOption"] = relationship(back_populates="cost_history")


class UnmatchedPlatformItem(Base):
    """자동매칭 3단계(플랫폼옵션번호 -> 플랫폼상품번호 -> 판매자상품코드)가 모두 실패한
    주문상품을 기록한다. 상품명/옵션명 유사도는 오탐 위험이 커 자동매칭에 쓰지 않는다 -
    확실한 고유ID 기준 매칭이 모두 실패하면 조용히 건너뛰지 않고 이 테이블에 기록해
    사용자가 상품관리 화면에서 직접 기존 상품에 연결(수동 매칭)할 수 있게 한다."""

    __tablename__ = "product_unmatched_items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    platform_id: Mapped[int] = mapped_column(ForeignKey("platforms.id"), nullable=False)
    platform_order_no: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # 옵션(채널상품) 단위 식별자 - product_platform_map.platform_option_id와 동일 의미.
    platform_option_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # 상품(그룹상품) 단위 식별자 - product_platform_map.platform_product_id와 동일 의미.
    platform_product_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    product_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    option_name: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    seller_product_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    quantity: Mapped[Optional[int]] = mapped_column(nullable=True)
    unit_price: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)  # PENDING/MATCHED
    matched_option_id: Mapped[Optional[int]] = mapped_column(ForeignKey("product_options.id"), nullable=True)
    # 누가/무엇이 연결했는지 - "MANUAL"(사용자가 화면에서 직접 연결) 또는 향후 자동화 시 "AUTO".
    matched_by: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    matched_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    platform: Mapped["Platform"] = relationship()  # type: ignore[name-defined]
    matched_option: Mapped[Optional["ProductOption"]] = relationship()
