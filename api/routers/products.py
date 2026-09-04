"""
api/routers/products.py
---------------------------
상품/SKU 조회 + 등록/수정, 플랫폼 매핑, 원가 이력 관리. SRS FR-PRD-01/02/03 대응.
"""

import json
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.deps import get_current_user, get_db, require_permission
from integrations.malls import get_mall_connector
from models.user import User
from repositories.integration_sync_repository import ExternalCommandRepository
from repositories.inventory_repository import InventoryRepository
from repositories.order_repository import OrderRepository
from repositories.platform_repository import PlatformRepository
from repositories.product_repository import (
    ProductCostHistoryRepository,
    ProductImageRepository,
    ProductOptionRepository,
    ProductPlatformMapRepository,
    ProductPublishDraftRepository,
    ProductPublishOptionGroupDraftRepository,
    ProductPublishOptionGroupItemDraftRepository,
    ProductRepository,
    UnmatchedPlatformItemRepository,
)
from services.product_option_publish_service import TARGET_TYPE as PRODUCT_OPTION_PUBLISH_TARGET_TYPE
from services.product_option_publish_service import (
    ProductOptionPublishAlreadyRegisteredError,
    ProductOptionPublishDisabledError,
    ProductOptionPublishDraftNotFoundError,
    ProductOptionPublishItemNotFoundError,
    ProductOptionPublishService,
)
from services.product_publish_service import TARGET_TYPE as PRODUCT_PUBLISH_TARGET_TYPE
from services.product_publish_service import (
    ProductPublishAlreadyRegisteredError,
    ProductPublishDisabledError,
    ProductPublishDraftNotFoundError,
    ProductPublishService,
)
from services.product_service import ProductCostService, ProductService
from services.product_sync_dispatch_service import TARGET_TYPE as PRODUCT_SYNC_TARGET_TYPE
from services.product_sync_dispatch_service import (
    ProductChannelSyncDisabledError,
    ProductSyncDispatchService,
    ProductSyncMappingNotFoundError,
)
from services.product_sync_service import ProductSyncService

_PRODUCT_COMMAND_TARGET_TYPES = {
    PRODUCT_SYNC_TARGET_TYPE,
    PRODUCT_PUBLISH_TARGET_TYPE,
    PRODUCT_OPTION_PUBLISH_TARGET_TYPE,
}

router = APIRouter(
    prefix="/api/products", tags=["products"], dependencies=[Depends(require_permission("PRODUCT_MANAGE"))]
)


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    category: Optional[str]
    brand: Optional[str]
    manufacturer: Optional[str]
    base_price: Optional[float]
    status: str
    is_deleted: bool


class ProductCreate(BaseModel):
    name: str
    category: Optional[str] = None
    brand: Optional[str] = None
    manufacturer: Optional[str] = None
    base_price: Optional[float] = None
    status: str = "ACTIVE"


class ProductUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    brand: Optional[str] = None
    manufacturer: Optional[str] = None
    base_price: Optional[float] = None
    status: Optional[str] = None


class ProductOptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    sku_code: str
    option_name: Optional[str]
    color: Optional[str]
    size: Optional[str]
    barcode: Optional[str]
    unit_cost_price: Optional[float]
    sale_price: Optional[float]
    is_active: bool
    sort_order: int


class ProductOptionCreate(BaseModel):
    sku_code: str
    option_name: Optional[str] = None
    color: Optional[str] = None
    size: Optional[str] = None
    barcode: Optional[str] = None
    unit_cost_price: Optional[float] = None
    sale_price: Optional[float] = None


class ProductOptionUpdate(BaseModel):
    option_name: Optional[str] = None
    color: Optional[str] = None
    size: Optional[str] = None
    barcode: Optional[str] = None
    unit_cost_price: Optional[float] = None
    sale_price: Optional[float] = None


class ProductOptionActiveUpdate(BaseModel):
    is_active: bool


class ProductOptionReorder(BaseModel):
    option_ids: list[int]


class ProductPlatformMapOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_option_id: int
    platform_id: int
    platform_option_id: str
    platform_product_id: Optional[str]
    platform_origin_product_id: Optional[str] = None
    display_name: Optional[str]
    seller_product_code: Optional[str]
    sibling_mapping_ids: list[int] = []
    """이 매핑과 같은 (platform_id, platform_origin_product_id)를 공유하는 다른
    매핑들의 id 목록(자기 자신 제외) - 네이버는 원상품 하나에 스마트스토어/윈도우 등
    복수 채널상품이 연결될 수 있어, 이 매핑에서 판매상태를 바꾸면 목록에 있는 다른
    매핑들의 노출 화면에도 함께 영향을 준다는 것을 화면에서 드러내기 위함이다.
    Coupang은 platform_option_id가 1:1 유니크 제약이라 항상 빈 목록이다."""


def _to_platform_map_out(m: Any, platform_map_repo: ProductPlatformMapRepository) -> ProductPlatformMapOut:
    """ProductPlatformMap ORM 객체를 sibling_mapping_ids까지 채운 응답 모델로
    변환한다 - 이 매핑을 반환하는 모든 엔드포인트(상품상세/매핑 목록조회/매핑
    생성/매핑수정)가 이 헬퍼를 거쳐야 한다. 한 곳(예: 상품상세)에만 적용하고
    다른 곳은 빠뜨리면, 화면이 실제로 쓰는 엔드포인트가 후자일 때 형제 매핑
    경고가 조용히 사라진다(실사용 클릭 검증으로 발견된 결함 - 프론트가 상품상세가
    아니라 이 목록조회 엔드포인트로 표를 그린다)."""
    out = ProductPlatformMapOut.model_validate(m)
    if m.platform_origin_product_id:
        siblings = platform_map_repo.list_by_platform_and_origin_product_id(m.platform_id, m.platform_origin_product_id)
        out.sibling_mapping_ids = sorted(s.id for s in siblings if s.id != m.id)
    return out


class ProductPlatformMapCreate(BaseModel):
    platform_id: int
    platform_option_id: str
    platform_product_id: Optional[str] = None
    display_name: Optional[str] = None
    seller_product_code: Optional[str] = None


class ProductPlatformMapUpdate(BaseModel):
    display_name: Optional[str] = None
    seller_product_code: Optional[str] = None
    platform_product_id: Optional[str] = None
    platform_option_id: Optional[str] = None


class ProductCostHistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_option_id: int
    supplier_id: Optional[int]
    cost_price: float
    effective_from: datetime
    effective_to: Optional[datetime]


class ProductCostHistoryCreate(BaseModel):
    cost_price: float
    effective_from: datetime
    supplier_id: Optional[int] = None


class ProductImageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    product_option_id: Optional[int]
    image_url: str
    is_thumbnail: bool
    sort_order: int


class ProductImageCreate(BaseModel):
    image_url: str
    is_thumbnail: bool = False
    product_option_id: Optional[int] = None


class UnmatchedPlatformItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    platform_id: int
    platform_order_no: Optional[str]
    platform_option_id: Optional[str]
    platform_product_id: Optional[str]
    product_name: Optional[str]
    option_name: Optional[str]
    seller_product_code: Optional[str]
    quantity: Optional[int]
    unit_price: Optional[float]
    status: str
    matched_by: Optional[str]
    matched_at: Optional[datetime]
    created_at: datetime


class UnmatchedPlatformItemResolve(BaseModel):
    product_option_id: int


class ProductOptionStatsOut(BaseModel):
    total_quantity_sold: int
    last_order_date: Optional[datetime]
    current_cost_price: Optional[float]
    sellable_stock: int


class ProductOptionDetailOut(ProductOptionOut):
    platform_maps: list[ProductPlatformMapOut]
    images: list[ProductImageOut]
    stats: ProductOptionStatsOut


class ProductDetailOut(ProductOut):
    options: list[ProductOptionDetailOut]
    images: list[ProductImageOut]


class ProductNaverSyncRequest(BaseModel):
    platform_id: int


class ProductNaverSyncResult(BaseModel):
    total_items: int
    created_products: int
    created_options: int
    updated_options: int


@router.post(
    "/sync-from-naver",
    response_model=ProductNaverSyncResult,
    status_code=status.HTTP_200_OK,
    summary="네이버 상품 동기화",
    description="지정한 플랫폼(네이버 스마트스토어)의 상품 API에서 상품 목록을 가져와 "
    "등록/갱신하고 대표/추가/옵션 이미지를 저장한다. 상품은 이 엔드포인트를 통해서만 "
    "새로 생성되며, 주문 수집(/api/orders/sync)에서는 더 이상 상품이 생성되지 않는다 - "
    "주문 수집 전에 먼저 실행해야 매핑이 채워진다.",
    responses={404: {"description": "플랫폼을 찾을 수 없습니다."}},
)
def sync_products_from_naver(payload: ProductNaverSyncRequest, db: Session = Depends(get_db)) -> ProductNaverSyncResult:
    platform = PlatformRepository(db).get_by_id(payload.platform_id)
    if platform is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="플랫폼을 찾을 수 없습니다.")

    connector = get_mall_connector(platform.connector_class, session=db, platform_id=platform.id)
    result = ProductSyncService(db).sync_products_from_naver(connector, platform.id)
    db.commit()
    return ProductNaverSyncResult(**result)


@router.get(
    "",
    response_model=list[ProductOut],
    summary="상품 목록 조회",
    description="카테고리로 필터링하거나(category 지정 시), 활성 상품 전체를 조회한다. "
    "include_deleted=true면 소프트 삭제된 상품도 포함해 전체를 조회한다(복원 화면용).",
)
def list_products(category: Optional[str] = None, include_deleted: bool = False, db: Session = Depends(get_db)) -> list:
    repo = ProductRepository(db)
    if include_deleted:
        return repo.list_all_including_deleted(category)
    return repo.list_by_category(category) if category else repo.list_active()


@router.get(
    "/unmatched-items",
    response_model=list[UnmatchedPlatformItemOut],
    summary="미매칭 상품 목록 조회",
    description="자동매칭(플랫폼옵션번호/플랫폼상품번호/판매자상품코드, 실제 고유ID 기준) "
    "3단계가 모두 실패해 아직 연결되지 않은 주문상품 목록을 조회한다. 상품명/옵션명 유사도는 "
    "오탐 위험이 커 자동매칭에 쓰지 않는다 - 사용자가 기존 상품에 직접 연결(resolve)해야 한다.",
)
def list_unmatched_items(db: Session = Depends(get_db)) -> list:
    return ProductService(db).list_unmatched_items()


@router.post(
    "/unmatched-items/{item_id}/resolve",
    response_model=ProductPlatformMapOut,
    summary="미매칭 상품 수동 연결",
    description="미매칭 상품을 지정한 옵션(product_option_id)에 연결한다 - 새 플랫폼 매핑을 "
    "등록하고 미매칭 기록을 RESOLVED로 표시한다.",
    responses={404: {"description": "미매칭 상품 또는 옵션을 찾을 수 없습니다."}},
)
def resolve_unmatched_item(item_id: int, payload: UnmatchedPlatformItemResolve, db: Session = Depends(get_db)):
    item = UnmatchedPlatformItemRepository(db).get_by_id(item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="미매칭 상품을 찾을 수 없습니다.")
    if ProductOptionRepository(db).get_by_id(payload.product_option_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="옵션을 찾을 수 없습니다.")
    mapping = ProductService(db).resolve_unmatched_item(item, payload.product_option_id)
    db.commit()
    return mapping


@router.get(
    "/{product_id}",
    response_model=ProductOut,
    summary="상품 단건 조회",
    responses={404: {"description": "상품을 찾을 수 없습니다."}},
)
def get_product(product_id: int, db: Session = Depends(get_db)):
    product = ProductRepository(db).get_by_id(product_id)
    if product is None or product.is_deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="상품을 찾을 수 없습니다.")
    return product


@router.get(
    "/{product_id}/detail",
    response_model=ProductDetailOut,
    summary="상품 상세 통합 조회",
    description="기본정보/옵션/플랫폼 매핑/이미지/통계(총 판매수량·최근 주문일·현재원가·현재재고)를 "
    "한 번의 호출로 모두 반환한다 - 상품 상세 화면 전용.",
    responses={404: {"description": "상품을 찾을 수 없습니다."}},
)
def get_product_detail(product_id: int, db: Session = Depends(get_db)):
    product = ProductRepository(db).get_by_id(product_id)
    if product is None or product.is_deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="상품을 찾을 수 없습니다.")

    option_repo = ProductOptionRepository(db)
    platform_map_repo = ProductPlatformMapRepository(db)
    cost_history_repo = ProductCostHistoryRepository(db)
    order_repo = OrderRepository(db)
    inventory_repo = InventoryRepository(db)

    all_images = ProductImageRepository(db).list_by_product(product_id)
    product_level_images = [i for i in all_images if i.product_option_id is None]

    option_details = []
    for option in option_repo.list_by_product(product_id):
        total_quantity, last_order_date = order_repo.total_quantity_and_last_order_date(option.id)
        open_cost = cost_history_repo.get_open(option.id)
        option_details.append(
            ProductOptionDetailOut(
                **ProductOptionOut.model_validate(option).model_dump(),
                platform_maps=[
                    _to_platform_map_out(m, platform_map_repo) for m in platform_map_repo.list_by_option(option.id)
                ],
                images=[ProductImageOut.model_validate(i) for i in all_images if i.product_option_id == option.id],
                stats=ProductOptionStatsOut(
                    total_quantity_sold=total_quantity,
                    last_order_date=last_order_date,
                    current_cost_price=float(open_cost.cost_price) if open_cost else None,
                    sellable_stock=inventory_repo.total_stock_by_option(option.id),
                ),
            )
        )

    return ProductDetailOut(
        **ProductOut.model_validate(product).model_dump(),
        options=option_details,
        images=[ProductImageOut.model_validate(i) for i in product_level_images],
    )


@router.post("", response_model=ProductOut, status_code=status.HTTP_201_CREATED, summary="상품 등록")
def create_product(payload: ProductCreate, db: Session = Depends(get_db)):
    product = ProductService(db).create_product(
        payload.name, payload.category, payload.base_price, payload.status, payload.brand, payload.manufacturer
    )
    db.commit()
    return product


@router.patch(
    "/{product_id}",
    response_model=ProductOut,
    summary="상품 수정",
    description="이름/카테고리/기준가/상태를 부분 수정한다. status를 DISCONTINUED로 바꾸면 판매중지 처리된다.",
    responses={404: {"description": "상품을 찾을 수 없습니다."}},
)
def update_product(product_id: int, payload: ProductUpdate, db: Session = Depends(get_db)):
    product = ProductRepository(db).get_by_id(product_id)
    if product is None or product.is_deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="상품을 찾을 수 없습니다.")
    updated = ProductService(db).update_product(
        product, payload.name, payload.category, payload.base_price, payload.status, payload.brand, payload.manufacturer
    )
    db.commit()
    return updated


@router.delete(
    "/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="상품 삭제",
    description="상품을 소프트 삭제한다(is_deleted=True). 이미 주문 이력이 참조하고 있을 "
    "수 있어 실제 행은 지우지 않으며, 목록 조회에서 제외되고 옵션도 함께 비활성화된다.",
    responses={404: {"description": "상품을 찾을 수 없습니다."}},
)
def delete_product(product_id: int, db: Session = Depends(get_db)) -> None:
    product = ProductRepository(db).get_by_id(product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="상품을 찾을 수 없습니다.")
    ProductService(db).delete_product(product)
    db.commit()


@router.post(
    "/{product_id}/restore",
    response_model=ProductOut,
    summary="삭제된 상품 복원",
    description="소프트 삭제된 상품을 되돌린다(is_deleted=False). 삭제 시 함께 비활성화된 "
    "옵션은 복원되지 않으므로, 필요하면 옵션별로 다시 활성화해야 한다.",
    responses={404: {"description": "상품을 찾을 수 없습니다."}},
)
def restore_product(product_id: int, db: Session = Depends(get_db)):
    product = ProductRepository(db).get_by_id(product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="상품을 찾을 수 없습니다.")
    restored = ProductService(db).restore_product(product)
    db.commit()
    return restored


@router.post(
    "/{product_id}/duplicate",
    response_model=ProductOut,
    status_code=status.HTTP_201_CREATED,
    summary="상품 복제",
    description="상품/옵션/이미지를 복제해 새 상품을 만든다. 플랫폼 매핑은 특정 외부 채널의 "
    "상품코드에 종속된 정보라 복제하지 않는다.",
    responses={404: {"description": "상품을 찾을 수 없습니다."}},
)
def duplicate_product(product_id: int, db: Session = Depends(get_db)):
    product = ProductRepository(db).get_by_id(product_id)
    if product is None or product.is_deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="상품을 찾을 수 없습니다.")
    duplicated = ProductService(db).duplicate_product(product)
    db.commit()
    return duplicated


@router.get(
    "/{product_id}/options",
    response_model=list[ProductOptionOut],
    summary="상품 옵션(SKU) 목록 조회",
    description="상품에 속한 옵션(SKU) 목록을 조회한다. 상품이 없으면 빈 목록을 반환한다.",
)
def list_product_options(product_id: int, db: Session = Depends(get_db)) -> list:
    return ProductOptionRepository(db).list_by_product(product_id)


_OPTION_TEXT_FIELDS = ("option_name", "color", "size", "barcode")
_OPTION_NAME_MAX_LENGTH = 255


def _normalize_option_text(value: Optional[str]) -> Optional[str]:
    """옵션 텍스트 필드(옵션명/색상/사이즈/바코드) 공통 정규화 - 앞뒤 공백을 지우고
    그 결과가 빈 문자열이면 NULL로 취급한다(빈 문자열과 공백만 있는 값을 동일하게
    처리해, "지운다"는 사용자 의도가 실제로 반영되게 한다)."""
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None


def _validate_option_fields(option_name: Optional[str], unit_cost_price: Optional[float]) -> None:
    """옵션명 길이(255자)/단가(0 이상) 제약을 SQLite·PostgreSQL 등 DB 엔진과
    무관하게 애플리케이션 레이어에서 강제한다. 조용히 자르거나 무시하지 않고
    422로 명시적으로 거부한다."""
    if option_name is not None and len(option_name) > _OPTION_NAME_MAX_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"옵션명은 {_OPTION_NAME_MAX_LENGTH}자를 초과할 수 없습니다.",
        )
    if unit_cost_price is not None and unit_cost_price < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="단가(매입원가)는 0 이상이어야 합니다."
        )


@router.post(
    "/{product_id}/options",
    response_model=ProductOptionOut,
    status_code=status.HTTP_201_CREATED,
    summary="상품 옵션(SKU) 등록",
    responses={
        404: {"description": "상품을 찾을 수 없습니다."},
        409: {"description": "이미 존재하는 SKU 코드입니다."},
        422: {"description": "옵션명이 255자를 초과하거나 단가가 음수입니다."},
    },
)
def create_product_option(product_id: int, payload: ProductOptionCreate, db: Session = Depends(get_db)):
    if ProductRepository(db).get_by_id(product_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="상품을 찾을 수 없습니다.")
    option_name = _normalize_option_text(payload.option_name)
    color = _normalize_option_text(payload.color)
    size = _normalize_option_text(payload.size)
    barcode = _normalize_option_text(payload.barcode)
    _validate_option_fields(option_name, payload.unit_cost_price)
    option = ProductService(db).create_option(
        product_id, payload.sku_code, option_name, color, size, barcode, payload.unit_cost_price, payload.sale_price
    )
    db.commit()
    return option


@router.patch(
    "/{product_id}/options/reorder",
    response_model=list[ProductOptionOut],
    summary="상품 옵션(SKU) 순서 변경",
    description="전달한 순서(option_ids)대로 옵션 목록의 표시 순서를 다시 매긴다.",
    responses={404: {"description": "상품을 찾을 수 없습니다."}},
)
def reorder_product_options(product_id: int, payload: ProductOptionReorder, db: Session = Depends(get_db)) -> list:
    if ProductRepository(db).get_by_id(product_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="상품을 찾을 수 없습니다.")
    reordered = ProductService(db).reorder_options(product_id, payload.option_ids)
    db.commit()
    return reordered


@router.patch(
    "/options/{option_id}",
    response_model=ProductOptionOut,
    summary="상품 옵션(SKU) 수정",
    description="부분 수정(PATCH) - 요청 본문에 없는 필드는 기존 값을 그대로 유지하고, "
    "명시적으로 null을 보낸 nullable 필드는 실제로 NULL로 지운다.",
    responses={
        404: {"description": "옵션을 찾을 수 없습니다."},
        422: {"description": "옵션명이 255자를 초과하거나 단가가 음수입니다."},
    },
)
def update_product_option(option_id: int, payload: ProductOptionUpdate, db: Session = Depends(get_db)):
    option = ProductOptionRepository(db).get_by_id(option_id)
    if option is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="옵션을 찾을 수 없습니다.")
    updates: dict[str, Any] = payload.model_dump(exclude_unset=True)
    for field in _OPTION_TEXT_FIELDS:
        if field in updates:
            updates[field] = _normalize_option_text(updates[field])
    _validate_option_fields(updates.get("option_name"), updates.get("unit_cost_price"))
    updated = ProductService(db).update_option(option, updates)
    db.commit()
    return updated


@router.patch(
    "/options/{option_id}/active",
    response_model=ProductOptionOut,
    summary="상품 옵션(SKU) 활성/비활성 전환",
    responses={404: {"description": "옵션을 찾을 수 없습니다."}},
)
def set_product_option_active(option_id: int, payload: ProductOptionActiveUpdate, db: Session = Depends(get_db)):
    option = ProductOptionRepository(db).get_by_id(option_id)
    if option is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="옵션을 찾을 수 없습니다.")
    updated = ProductService(db).set_option_active(option, payload.is_active)
    db.commit()
    return updated


@router.delete(
    "/options/{option_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="상품 옵션(SKU) 삭제",
    description="옵션을 삭제한다. 이미 주문에서 사용 중인 옵션은 삭제할 수 없으며(409), "
    "이 경우 대신 비활성화(활성/비활성 전환)를 사용해야 한다.",
    responses={
        404: {"description": "옵션을 찾을 수 없습니다."},
        409: {"description": "이미 주문에서 사용 중인 옵션은 삭제할 수 없습니다."},
    },
)
def delete_product_option(option_id: int, db: Session = Depends(get_db)) -> None:
    option = ProductOptionRepository(db).get_by_id(option_id)
    if option is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="옵션을 찾을 수 없습니다.")
    ProductService(db).delete_option(option)
    db.commit()


@router.get(
    "/options/{option_id}/platform-map", response_model=list[ProductPlatformMapOut], summary="SKU-플랫폼 매핑 목록 조회"
)
def list_platform_maps(option_id: int, db: Session = Depends(get_db)) -> list:
    repo = ProductPlatformMapRepository(db)
    return [_to_platform_map_out(m, repo) for m in repo.list_by_option(option_id)]


@router.post(
    "/options/{option_id}/platform-map",
    response_model=ProductPlatformMapOut,
    status_code=status.HTTP_201_CREATED,
    summary="SKU-플랫폼 매핑 등록",
    description="동일 상품이 여러 플랫폼에 다른 상품코드로 등록되어 있어도 하나의 SKU로 매핑한다.",
    responses={
        404: {"description": "옵션을 찾을 수 없습니다."},
        409: {"description": "이미 등록된 (플랫폼, 플랫폼옵션번호) 조합입니다."},
    },
)
def create_platform_map(option_id: int, payload: ProductPlatformMapCreate, db: Session = Depends(get_db)):
    if ProductOptionRepository(db).get_by_id(option_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="옵션을 찾을 수 없습니다.")
    mapping = ProductService(db).create_platform_map(
        option_id,
        payload.platform_id,
        payload.platform_option_id,
        payload.display_name,
        payload.seller_product_code,
        payload.platform_product_id,
    )
    db.commit()
    return _to_platform_map_out(mapping, ProductPlatformMapRepository(db))


@router.patch(
    "/platform-map/{mapping_id}",
    response_model=ProductPlatformMapOut,
    summary="SKU-플랫폼 매핑 수정",
    description="쇼핑몰 노출상품명/판매자상품코드/플랫폼 상품번호를 수정한다.",
    responses={404: {"description": "매핑을 찾을 수 없습니다."}},
)
def update_platform_map(mapping_id: int, payload: ProductPlatformMapUpdate, db: Session = Depends(get_db)):
    mapping = ProductPlatformMapRepository(db).get_by_id(mapping_id)
    if mapping is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="매핑을 찾을 수 없습니다.")
    updated = ProductService(db).update_platform_map(
        mapping,
        payload.display_name,
        payload.seller_product_code,
        payload.platform_product_id,
        payload.platform_option_id,
    )
    db.commit()
    return _to_platform_map_out(updated, ProductPlatformMapRepository(db))


@router.delete(
    "/platform-map/{mapping_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="SKU-플랫폼 매핑 삭제",
    responses={404: {"description": "매핑을 찾을 수 없습니다."}},
)
def delete_platform_map(mapping_id: int, db: Session = Depends(get_db)) -> None:
    repo = ProductPlatformMapRepository(db)
    mapping = repo.get_by_id(mapping_id)
    if mapping is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="매핑을 찾을 수 없습니다.")
    ProductService(db).delete_platform_map(mapping)
    db.commit()


@router.get(
    "/options/{option_id}/cost-history",
    response_model=list[ProductCostHistoryOut],
    summary="SKU 원가 이력 조회",
    description="effective_from 최신순으로 원가 변경 이력을 조회한다.",
)
def list_cost_history(option_id: int, db: Session = Depends(get_db)) -> list:
    return ProductCostService(db).list_history(option_id)


@router.post(
    "/options/{option_id}/cost-history",
    response_model=ProductCostHistoryOut,
    status_code=status.HTTP_201_CREATED,
    summary="SKU 원가 등록",
    description="새 원가를 effective_from 시점부터 적용되도록 등록한다. 기존에 열려있던(종료일 없는) "
    "원가 레코드가 있으면 자동으로 effective_to를 새 원가의 effective_from으로 닫는다.",
    responses={404: {"description": "옵션을 찾을 수 없습니다."}},
)
def create_cost_history(option_id: int, payload: ProductCostHistoryCreate, db: Session = Depends(get_db)):
    if ProductOptionRepository(db).get_by_id(option_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="옵션을 찾을 수 없습니다.")
    record = ProductCostService(db).add_cost(option_id, payload.cost_price, payload.effective_from, payload.supplier_id)
    db.commit()
    return record


@router.get(
    "/{product_id}/images",
    response_model=list[ProductImageOut],
    summary="상품 이미지 목록 조회",
    description="대표이미지가 먼저 오도록 정렬해 반환한다.",
)
def list_product_images(product_id: int, db: Session = Depends(get_db)) -> list:
    images = ProductImageRepository(db).list_by_product(product_id)
    return sorted(images, key=lambda i: (not i.is_thumbnail, i.sort_order))


@router.post(
    "/{product_id}/images",
    response_model=ProductImageOut,
    status_code=status.HTTP_201_CREATED,
    summary="상품 이미지 등록",
    description="첫 이미지이거나 is_thumbnail=true로 등록하면 대표이미지로 지정된다(대표이미지는 항상 1개만 존재).",
    responses={404: {"description": "상품을 찾을 수 없습니다."}},
)
def create_product_image(product_id: int, payload: ProductImageCreate, db: Session = Depends(get_db)):
    if ProductRepository(db).get_by_id(product_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="상품을 찾을 수 없습니다.")
    image = ProductService(db).add_image(product_id, payload.image_url, payload.is_thumbnail, payload.product_option_id)
    db.commit()
    return image


@router.patch(
    "/images/{image_id}/thumbnail",
    response_model=ProductImageOut,
    summary="대표이미지 지정",
    description="이 이미지를 대표이미지로 지정하고, 같은 상품의 다른 이미지는 모두 대표이미지에서 해제한다.",
    responses={404: {"description": "이미지를 찾을 수 없습니다."}},
)
def set_product_image_thumbnail(image_id: int, db: Session = Depends(get_db)):
    image = ProductImageRepository(db).get_by_id(image_id)
    if image is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="이미지를 찾을 수 없습니다.")
    updated = ProductService(db).set_thumbnail(image.product_id, image)
    db.commit()
    return updated


@router.delete(
    "/images/{image_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="상품 이미지 삭제",
    description="대표이미지를 삭제하면 남은 이미지 중 하나가 자동으로 새 대표이미지로 지정된다.",
    responses={404: {"description": "이미지를 찾을 수 없습니다."}},
)
def delete_product_image(image_id: int, db: Session = Depends(get_db)) -> None:
    image = ProductImageRepository(db).get_by_id(image_id)
    if image is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="이미지를 찾을 수 없습니다.")
    ProductService(db).delete_image(image)
    db.commit()


# --- 상용 ERP 확장(3단계, 첫 묶음): 재고 수량/판매상태 전송 -----------------------


class InventorySyncRequest(BaseModel):
    target_quantity: int


class SaleStatusSyncRequest(BaseModel):
    target_status: str  # "ON_SALE" | "SUSPENDED"


class ProductSyncCommandOut(BaseModel):
    command_id: int
    status: str
    already_processed: bool
    error_code: Optional[str] = None


@router.post(
    "/platform-map/{mapping_id}/sync-inventory",
    response_model=ProductSyncCommandOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="채널로 재고 수량 전송 요청(비동기)",
    description="지정한 플랫폼 매핑의 재고 수량을 채널(네이버/쿠팡)에 전송할 명령을 생성한다. "
    "이 API는 실제 채널 호출을 하지 않고 명령만 접수한다(202) - 실제 전송은 스케줄러의 "
    "product_sync_dispatch_job이 비동기로 수행하며, 처리 결과는 GET /api/products/"
    "sync-commands/{command_id}로 폴링해 확인해야 한다. 같은 매핑에 이미 같은 목표 수량으로 "
    "생성된 명령이 있으면(버튼 연타 포함) 새로 만들지 않고 기존 명령을 그대로 반환한다.",
    responses={
        404: {"description": "플랫폼 매핑을 찾을 수 없습니다."},
        400: {"description": "재고 수량이 계약 범위를 벗어났습니다(음수/소수/상한 초과 등)."},
        503: {"description": "재고/판매상태 전송 기능이 비활성화(OFF) 상태입니다(실계정 검증 승인 전)."},
    },
)
def sync_inventory(
    mapping_id: int, payload: InventorySyncRequest, db: Session = Depends(get_db)
) -> ProductSyncCommandOut:
    service = ProductSyncDispatchService(db)
    try:
        outcome = service.enqueue_inventory_update(mapping_id, payload.target_quantity)
    except ProductChannelSyncDisabledError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)) from e
    except ProductSyncMappingNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    db.commit()
    return ProductSyncCommandOut(
        command_id=outcome.command.id,
        status=outcome.command.status,
        already_processed=outcome.already_processed,
        error_code=outcome.command.error_code,
    )


@router.post(
    "/platform-map/{mapping_id}/sync-sale-status",
    response_model=ProductSyncCommandOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="채널로 판매상태 전송 요청(비동기)",
    description="지정한 플랫폼 매핑의 판매상태(ON_SALE|SUSPENDED)를 채널에 전송할 명령을 "
    "생성한다. sync-inventory와 동일하게 명령만 접수하고(202) 실제 전송은 스케줄러가 수행한다.",
    responses={
        404: {"description": "플랫폼 매핑을 찾을 수 없습니다."},
        400: {"description": "알 수 없는 target_status입니다."},
        503: {"description": "재고/판매상태 전송 기능이 비활성화(OFF) 상태입니다(실계정 검증 승인 전)."},
    },
)
def sync_sale_status(
    mapping_id: int, payload: SaleStatusSyncRequest, db: Session = Depends(get_db)
) -> ProductSyncCommandOut:
    service = ProductSyncDispatchService(db)
    try:
        outcome = service.enqueue_sale_status_update(mapping_id, payload.target_status)
    except ProductChannelSyncDisabledError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)) from e
    except ProductSyncMappingNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    db.commit()
    return ProductSyncCommandOut(
        command_id=outcome.command.id,
        status=outcome.command.status,
        already_processed=outcome.already_processed,
        error_code=outcome.command.error_code,
    )


class ProductSyncExternalCommandOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    command_type: str
    status: str
    attempt_count: int
    retryable: bool
    error_code: Optional[str]
    next_retry_at: Optional[datetime]
    completed_at: Optional[datetime]


@router.get(
    "/sync-commands/{command_id}",
    response_model=ProductSyncExternalCommandOut,
    summary="재고/판매상태 전송 명령 상태 조회",
    description="POST .../sync-inventory 또는 .../sync-sale-status가 반환한 command_id로 "
    "처리 상태를 폴링한다. 화면은 status가 SUCCESS로 확인된 뒤에만 성공으로 표시해야 한다.",
    responses={404: {"description": "명령을 찾을 수 없습니다."}},
)
def get_product_sync_command(command_id: int, db: Session = Depends(get_db)) -> ProductSyncExternalCommandOut:
    command = ExternalCommandRepository(db).get_by_id(command_id)
    if command is None or command.target_type not in _PRODUCT_COMMAND_TARGET_TYPES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="명령을 찾을 수 없습니다.")
    return ProductSyncExternalCommandOut.model_validate(command, from_attributes=True)


class ResolveProductSyncCommandRequest(BaseModel):
    resolution: str  # "CONFIRMED_NOT_SENT" | "CONFIRMED_SUCCESS" | "CONFIRMED_FAILED"


@router.post(
    "/sync-commands/{command_id}/resolve",
    response_model=ProductSyncExternalCommandOut,
    summary="결과 확인 필요(UNKNOWN) 재고/판매상태 명령 수동 해소",
    description="채널이 실제로 처리했는지 알 수 없는(UNKNOWN) 명령을, 운영자가 채널을 직접 "
    "확인한 뒤 해소한다(services.shipment_dispatch_service.resolve_unknown_command와 동일한 "
    "세 가지 해소 방식).",
    responses={
        404: {"description": "명령을 찾을 수 없습니다."},
        400: {"description": "UNKNOWN 상태가 아니거나 알 수 없는 해소 방식입니다."},
    },
)
def resolve_product_sync_command(
    command_id: int,
    payload: ResolveProductSyncCommandRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ProductSyncExternalCommandOut:
    existing = ExternalCommandRepository(db).get_by_id(command_id)
    if existing is None or existing.target_type not in _PRODUCT_COMMAND_TARGET_TYPES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="명령을 찾을 수 없습니다.")
    if existing.target_type == PRODUCT_PUBLISH_TARGET_TYPE:
        resolver: Any = ProductPublishService(db)
    elif existing.target_type == PRODUCT_OPTION_PUBLISH_TARGET_TYPE:
        resolver = ProductOptionPublishService(db)
    else:
        resolver = ProductSyncDispatchService(db)
    try:
        resolved = resolver.resolve_unknown_command(command_id, payload.resolution, resolved_by=current_user.id)
    except ValueError as e:
        db.rollback()
        code = status.HTTP_404_NOT_FOUND if "찾을 수 없습니다" in str(e) else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=code, detail=str(e)) from e
    db.commit()
    return ProductSyncExternalCommandOut.model_validate(resolved, from_attributes=True)


# --- 상용 ERP 확장(3단계, 두 번째 묶음): 단순 상품 신규 등록 + 제한된 정보 수정 ---


class ProductInfoUpdateRequest(BaseModel):
    name: Optional[str] = None
    sale_price: Optional[float] = None
    description: Optional[str] = None


@router.post(
    "/platform-map/{mapping_id}/update-info",
    response_model=ProductSyncCommandOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="기존 채널 상품의 상품명/판매가/상세설명 수정 요청(비동기)",
    description="공식 계약상 지원되는 상품명·판매가격·상세설명 중 실제로 바뀐 값만 보낸다 - "
    "None으로 둔 항목은 채널의 현재값을 그대로 보존한다(빈 값/0으로 지우지 않음). "
    "재고/판매상태 명령과 같은 외부 대상의 실행 순서를 공유한다(같은 대상에 동시에 나가지 않음).",
    responses={
        404: {"description": "플랫폼 매핑을 찾을 수 없습니다."},
        400: {"description": "수정할 항목이 하나도 없거나 판매가가 유효하지 않습니다."},
        503: {"description": "재고/판매상태/정보수정 전송 기능이 비활성화(OFF) 상태입니다(실계정 검증 승인 전)."},
    },
)
def update_platform_map_info(
    mapping_id: int, payload: ProductInfoUpdateRequest, db: Session = Depends(get_db)
) -> ProductSyncCommandOut:
    service = ProductSyncDispatchService(db)
    try:
        outcome = service.enqueue_info_update(
            mapping_id, name=payload.name, sale_price=payload.sale_price, description=payload.description
        )
    except ProductChannelSyncDisabledError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)) from e
    except ProductSyncMappingNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    db.commit()
    return ProductSyncCommandOut(
        command_id=outcome.command.id,
        status=outcome.command.status,
        already_processed=outcome.already_processed,
        error_code=outcome.command.error_code,
    )


class ProductPublishDraftRequest(BaseModel):
    platform_id: int
    name: Optional[str] = None
    sale_price: Optional[float] = None
    description_html: Optional[str] = None
    category_code: Optional[str] = None
    image_urls: Optional[list[str]] = None
    stock_quantity: Optional[int] = None
    channel_fields: Optional[dict[str, Any]] = None


class ProductPublishDraftOut(BaseModel):
    id: int
    product_option_id: int
    platform_id: int
    name: Optional[str] = None
    sale_price: Optional[float] = None
    description_html: Optional[str] = None
    category_code: Optional[str] = None
    image_urls: list[str] = []
    stock_quantity: Optional[int] = None
    channel_fields: dict[str, Any] = {}
    registered_at: Optional[datetime] = None
    pending_platform_product_id: Optional[str] = None
    # 네이버 ETC 카테고리 적합성 확인 기록(공식 검증이 아니라 운영자 확인 기록 -
    # services.product_publish_service.ProductPublishService.confirm_etc_notice
    # 참고). etc_notice_confirmation_valid는 확인 시점의 category_code/고시유형이
    # 지금 값과 여전히 같은지까지 반영한 값이다 - 카테고리/고시유형이 바뀌면
    # 확인 기록 자체는 남아 있어도(감사 목적) 이 값은 False가 된다.
    etc_notice_confirmed_by: Optional[int] = None
    etc_notice_confirmed_at: Optional[datetime] = None
    etc_notice_confirmed_category_code: Optional[str] = None
    etc_notice_confirmed_notice_type: Optional[str] = None
    etc_notice_confirmation_valid: bool = False

    @classmethod
    def from_draft(cls, draft: Any) -> "ProductPublishDraftOut":
        channel_fields = json.loads(draft.channel_fields_json) if draft.channel_fields_json else {}
        notice = channel_fields.get("productInfoProvidedNotice") if isinstance(channel_fields, dict) else None
        current_notice_type = notice.get("productInfoProvidedNoticeType") if isinstance(notice, dict) else None
        confirmation_valid = (
            draft.etc_notice_confirmed_at is not None
            and draft.etc_notice_confirmed_category_code == draft.category_code
            and draft.etc_notice_confirmed_notice_type == current_notice_type
        )
        return cls(
            id=draft.id,
            product_option_id=draft.product_option_id,
            platform_id=draft.platform_id,
            name=draft.name,
            sale_price=float(draft.sale_price) if draft.sale_price is not None else None,
            description_html=draft.description_html,
            category_code=draft.category_code,
            image_urls=json.loads(draft.image_urls_json) if draft.image_urls_json else [],
            stock_quantity=draft.stock_quantity,
            channel_fields=channel_fields,
            registered_at=draft.registered_at,
            pending_platform_product_id=draft.pending_platform_product_id,
            etc_notice_confirmed_by=draft.etc_notice_confirmed_by,
            etc_notice_confirmed_at=draft.etc_notice_confirmed_at,
            etc_notice_confirmed_category_code=draft.etc_notice_confirmed_category_code,
            etc_notice_confirmed_notice_type=draft.etc_notice_confirmed_notice_type,
            etc_notice_confirmation_valid=confirmation_valid,
        )


@router.post(
    "/options/{option_id}/publish-draft",
    response_model=ProductPublishDraftOut,
    summary="신규 상품 등록 초안 저장",
    description="옵션 조합 없는 단순 상품의 채널 신규 등록 초안을 저장한다(미입력 항목이 있어도 "
    "그대로 저장할 수 있다 - 전송 가능 여부는 제출 시점에 커넥터가 공식 계약 기준으로 검증한다). "
    "이미지는 http(s) URL만 허용한다(로컬 경로/file:// 금지).",
    responses={
        404: {"description": "옵션 또는 플랫폼을 찾을 수 없습니다."},
        400: {"description": "이미지 URL이 유효하지 않습니다."},
    },
)
def save_publish_draft(
    option_id: int, payload: ProductPublishDraftRequest, db: Session = Depends(get_db)
) -> ProductPublishDraftOut:
    service = ProductPublishService(db)
    try:
        draft = service.save_draft(
            option_id,
            payload.platform_id,
            name=payload.name,
            sale_price=payload.sale_price,
            description_html=payload.description_html,
            category_code=payload.category_code,
            image_urls=payload.image_urls,
            stock_quantity=payload.stock_quantity,
            channel_fields=payload.channel_fields,
        )
    except ProductPublishDraftNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    db.commit()
    return ProductPublishDraftOut.from_draft(draft)


@router.get(
    "/options/{option_id}/publish-draft/{platform_id}",
    response_model=ProductPublishDraftOut,
    summary="신규 상품 등록 초안 조회",
    responses={404: {"description": "초안이 없습니다."}},
)
def get_publish_draft(option_id: int, platform_id: int, db: Session = Depends(get_db)) -> ProductPublishDraftOut:
    draft = ProductPublishDraftRepository(db).get_by_option_and_platform(option_id, platform_id)
    if draft is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="초안이 없습니다.")
    return ProductPublishDraftOut.from_draft(draft)


@router.post(
    "/publish-drafts/{draft_id}/confirm-etc-notice",
    response_model=ProductPublishDraftOut,
    summary="네이버 ETC 카테고리 적합성 확인 기록",
    description="이 카테고리에 ETC(기타 재화) 상품정보제공고시 양식을 쓰는 것이 맞는지 판매자센터에서 "
    "직접 확인했다는 사실을 기록한다 - 이 API 자체가 공식 적합성을 검증하는 것은 아니다(공식으로 "
    "검증할 API가 없다 - integrations.malls.naver_smartstore_connector 모듈 docstring 참고). "
    "현재 채널별 세부 계약 정보의 고시유형이 ETC가 아니면 거부한다. 이후 카테고리 코드나 고시유형이 "
    "바뀌면 이 확인은 자동으로 무효화되며 다시 확인해야 한다.",
    responses={
        404: {"description": "초안을 찾을 수 없습니다."},
        400: {"description": "카테고리 코드가 없거나 현재 고시유형이 ETC가 아닙니다."},
    },
)
def confirm_publish_etc_notice(
    draft_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
) -> ProductPublishDraftOut:
    service = ProductPublishService(db)
    try:
        draft = service.confirm_etc_notice(draft_id, confirmed_by=current_user.id)
    except ProductPublishDraftNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    db.commit()
    return ProductPublishDraftOut.from_draft(draft)


@router.post(
    "/publish-drafts/{draft_id}/submit",
    response_model=ProductSyncCommandOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="신규 상품 등록 요청(비동기)",
    description="초안을 확정 스냅샷으로 접수한다 - 이 API는 실제 채널 호출을 하지 않고 명령만 "
    "접수한다(202). 실제 등록은 스케줄러의 product_publish_dispatch_job이 수행하며, 처리 결과는 "
    "GET /api/products/sync-commands/{command_id}로 폴링해 확인해야 한다. 필수 항목이 비어 있으면 "
    "채널 호출 전에 차단된다(추측으로 채우지 않음) - 이 오류는 실제 실행 시점(FAILED)에 나타난다.",
    responses={
        404: {"description": "초안을 찾을 수 없습니다."},
        409: {"description": "이미 이 채널에 등록된 매핑이 있습니다."},
        503: {"description": "상품 등록 기능이 비활성화(OFF) 상태입니다(실계정 검증 승인 전)."},
    },
)
def submit_publish_draft(draft_id: int, db: Session = Depends(get_db)) -> ProductSyncCommandOut:
    service = ProductPublishService(db)
    try:
        outcome = service.enqueue_create(draft_id)
    except ProductPublishDisabledError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)) from e
    except ProductPublishDraftNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except ProductPublishAlreadyRegisteredError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    db.commit()
    return ProductSyncCommandOut(
        command_id=outcome.command.id,
        status=outcome.command.status,
        already_processed=outcome.already_processed,
        error_code=outcome.command.error_code,
    )


class RegistrationStatusOut(BaseModel):
    status_name: Optional[str] = None
    channel_option_ids: list[str] = []


@router.get(
    "/publish-drafts/{draft_id}/registration-status",
    response_model=RegistrationStatusOut,
    summary="등록 접수된 상품의 채널 심사/승인 상태 조회",
    description="등록 응답이 옵션 단위 식별자를 즉시 돌려주지 않는 채널(쿠팡)을 위한 조회다 - "
    "승인이 끝나야 channel_option_ids가 채워진다. 확인 후에는 POST .../confirm-mapping으로 "
    "운영자가 직접 매핑을 확정해야 한다(자동 확정하지 않음).",
    responses={
        400: {"description": "등록 접수된 상품 단위 식별자가 없습니다."},
        404: {"description": "초안을 찾을 수 없습니다."},
    },
)
def get_publish_registration_status(draft_id: int, db: Session = Depends(get_db)) -> RegistrationStatusOut:
    service = ProductPublishService(db)
    try:
        result = service.check_registration_status(draft_id)
    except ProductPublishDraftNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    return RegistrationStatusOut(**result)


class ConfirmMappingRequest(BaseModel):
    channel_option_id: str


@router.post(
    "/publish-drafts/{draft_id}/confirm-mapping",
    response_model=ProductPlatformMapOut,
    summary="승인 완료된 옵션 단위 식별자로 매핑 확정",
    description="GET .../registration-status로 확인한 옵션 단위 식별자(예: 쿠팡 vendorItemId)를 "
    "운영자가 직접 지정해 플랫폼 매핑을 만든다 - 이 API가 채널 응답에서 자동으로 하나를 골라 "
    "추측하지 않는다.",
    responses={
        400: {"description": "확정할 상품 단위 식별자가 없습니다."},
        404: {"description": "초안을 찾을 수 없습니다."},
    },
)
def confirm_publish_mapping(
    draft_id: int, payload: ConfirmMappingRequest, db: Session = Depends(get_db)
) -> ProductPlatformMapOut:
    service = ProductPublishService(db)
    try:
        mapping = service.confirm_mapping(draft_id, payload.channel_option_id)
    except ProductPublishDraftNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    db.commit()
    return _to_platform_map_out(mapping, ProductPlatformMapRepository(db))


# --- 옵션조합 상품 등록 (상용 ERP 확장 3단계, 세 번째 묶음) ---


class ProductOptionGroupDraftRequest(BaseModel):
    platform_id: int
    name: Optional[str] = None
    description_html: Optional[str] = None
    category_code: Optional[str] = None
    image_urls: Optional[list[str]] = None
    base_sale_price: Optional[float] = None
    channel_fields: Optional[dict[str, Any]] = None


class ProductOptionGroupItemOut(BaseModel):
    id: int
    product_option_id: int
    option_values: list[list[str]] = []
    seller_product_code: Optional[str] = None
    sale_price: Optional[float] = None
    stock_quantity: Optional[int] = None

    @classmethod
    def from_item(cls, item: Any) -> "ProductOptionGroupItemOut":
        return cls(
            id=item.id,
            product_option_id=item.product_option_id,
            option_values=json.loads(item.option_values_json) if item.option_values_json else [],
            seller_product_code=item.seller_product_code,
            sale_price=float(item.sale_price) if item.sale_price is not None else None,
            stock_quantity=item.stock_quantity,
        )


class ProductOptionGroupDraftOut(BaseModel):
    id: int
    product_id: int
    platform_id: int
    name: Optional[str] = None
    description_html: Optional[str] = None
    category_code: Optional[str] = None
    image_urls: list[str] = []
    base_sale_price: Optional[float] = None
    channel_fields: dict[str, Any] = {}
    channel_product_id: Optional[str] = None
    channel_option_id: Optional[str] = None
    registered_at: Optional[datetime] = None
    etc_notice_confirmed_by: Optional[int] = None
    etc_notice_confirmed_at: Optional[datetime] = None
    etc_notice_confirmed_category_code: Optional[str] = None
    etc_notice_confirmed_notice_type: Optional[str] = None
    etc_notice_confirmation_valid: bool = False
    items: list[ProductOptionGroupItemOut] = []

    @classmethod
    def from_draft(cls, draft: Any, items: list[Any]) -> "ProductOptionGroupDraftOut":
        channel_fields = json.loads(draft.channel_fields_json) if draft.channel_fields_json else {}
        notice = channel_fields.get("productInfoProvidedNotice") if isinstance(channel_fields, dict) else None
        current_notice_type = notice.get("productInfoProvidedNoticeType") if isinstance(notice, dict) else None
        confirmation_valid = (
            draft.etc_notice_confirmed_at is not None
            and draft.etc_notice_confirmed_category_code == draft.category_code
            and draft.etc_notice_confirmed_notice_type == current_notice_type
        )
        return cls(
            id=draft.id,
            product_id=draft.product_id,
            platform_id=draft.platform_id,
            name=draft.name,
            description_html=draft.description_html,
            category_code=draft.category_code,
            image_urls=json.loads(draft.image_urls_json) if draft.image_urls_json else [],
            base_sale_price=float(draft.base_sale_price) if draft.base_sale_price is not None else None,
            channel_fields=channel_fields,
            channel_product_id=draft.channel_product_id,
            channel_option_id=draft.channel_option_id,
            registered_at=draft.registered_at,
            etc_notice_confirmed_by=draft.etc_notice_confirmed_by,
            etc_notice_confirmed_at=draft.etc_notice_confirmed_at,
            etc_notice_confirmed_category_code=draft.etc_notice_confirmed_category_code,
            etc_notice_confirmed_notice_type=draft.etc_notice_confirmed_notice_type,
            etc_notice_confirmation_valid=confirmation_valid,
            items=[ProductOptionGroupItemOut.from_item(i) for i in items],
        )


@router.post(
    "/{product_id}/option-publish-draft",
    response_model=ProductOptionGroupDraftOut,
    summary="옵션조합 상품 등록 초안 저장(상품 레벨)",
    description="하나의 로컬 상품에 속한 여러 SKU를 채널 옵션 조합 상품 하나로 묶어 등록하기 위한 "
    "상품 레벨(공통) 초안을 저장한다 - 미입력 항목이 있어도 자유롭게 저장할 수 있다. 품목(SKU)별 "
    "값은 POST .../items로 별도 추가한다.",
    responses={
        404: {"description": "상품 또는 플랫폼을 찾을 수 없습니다."},
        400: {"description": "이미지 URL이 유효하지 않습니다."},
    },
)
def save_option_publish_draft(
    product_id: int, payload: ProductOptionGroupDraftRequest, db: Session = Depends(get_db)
) -> ProductOptionGroupDraftOut:
    service = ProductOptionPublishService(db)
    try:
        draft = service.save_group_draft(
            product_id,
            payload.platform_id,
            name=payload.name,
            description_html=payload.description_html,
            category_code=payload.category_code,
            image_urls=payload.image_urls,
            base_sale_price=payload.base_sale_price,
            channel_fields=payload.channel_fields,
        )
    except ProductOptionPublishDraftNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    db.commit()
    items = ProductPublishOptionGroupItemDraftRepository(db).list_by_group(draft.id)
    return ProductOptionGroupDraftOut.from_draft(draft, items)


@router.get(
    "/{product_id}/option-publish-draft/{platform_id}",
    response_model=ProductOptionGroupDraftOut,
    summary="옵션조합 상품 등록 초안 조회",
    responses={404: {"description": "초안이 없습니다."}},
)
def get_option_publish_draft(
    product_id: int, platform_id: int, db: Session = Depends(get_db)
) -> ProductOptionGroupDraftOut:
    draft = ProductPublishOptionGroupDraftRepository(db).get_by_product_and_platform(product_id, platform_id)
    if draft is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="초안이 없습니다.")
    items = ProductPublishOptionGroupItemDraftRepository(db).list_by_group(draft.id)
    return ProductOptionGroupDraftOut.from_draft(draft, items)


@router.post(
    "/option-publish-drafts/{draft_id}/confirm-etc-notice",
    response_model=ProductOptionGroupDraftOut,
    summary="네이버 ETC 카테고리 적합성 확인 기록(옵션조합 초안)",
    description="services.product_publish_service.ProductPublishService.confirm_etc_notice와 동일한 "
    "원칙이다 - 이 API 자체가 공식 적합성을 검증하는 것은 아니다.",
    responses={
        404: {"description": "초안을 찾을 수 없습니다."},
        400: {"description": "카테고리 코드가 없거나 현재 고시유형이 ETC가 아닙니다."},
    },
)
def confirm_option_publish_etc_notice(
    draft_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
) -> ProductOptionGroupDraftOut:
    service = ProductOptionPublishService(db)
    try:
        draft = service.confirm_etc_notice(draft_id, confirmed_by=current_user.id)
    except ProductOptionPublishDraftNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    db.commit()
    items = ProductPublishOptionGroupItemDraftRepository(db).list_by_group(draft.id)
    return ProductOptionGroupDraftOut.from_draft(draft, items)


class ProductOptionGroupItemRequest(BaseModel):
    product_option_id: int
    option_values: Optional[list[list[str]]] = None
    seller_product_code: Optional[str] = None
    sale_price: Optional[float] = None
    stock_quantity: Optional[int] = None


@router.post(
    "/option-publish-drafts/{draft_id}/items",
    response_model=ProductOptionGroupItemOut,
    summary="옵션조합 상품 등록 초안에 SKU(품목) 추가/수정",
    description="이 초안이 속한 상품의 SKU만 추가할 수 있다. seller_product_code를 입력하지 않으면 "
    "이 SKU의 자체 채번 코드(sku_code)를 그대로 쓴다(채널 등록 후 응답에서 이 SKU를 되찾는 근거).",
    responses={
        404: {"description": "초안 또는 옵션을 찾을 수 없습니다."},
        400: {"description": "이 SKU는 이 초안의 상품에 속하지 않습니다."},
    },
)
def save_option_publish_item(
    draft_id: int, payload: ProductOptionGroupItemRequest, db: Session = Depends(get_db)
) -> ProductOptionGroupItemOut:
    service = ProductOptionPublishService(db)
    try:
        item = service.save_item(
            draft_id,
            payload.product_option_id,
            option_values=payload.option_values,
            seller_product_code=payload.seller_product_code,
            sale_price=payload.sale_price,
            stock_quantity=payload.stock_quantity,
        )
    except ProductOptionPublishDraftNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    db.commit()
    return ProductOptionGroupItemOut.from_item(item)


@router.delete(
    "/option-publish-drafts/{draft_id}/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="옵션조합 상품 등록 초안에서 SKU(품목) 제거",
    responses={404: {"description": "품목을 찾을 수 없습니다."}},
)
def delete_option_publish_item(draft_id: int, item_id: int, db: Session = Depends(get_db)) -> None:
    service = ProductOptionPublishService(db)
    try:
        service.delete_item(draft_id, item_id)
    except ProductOptionPublishItemNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    db.commit()


@router.post(
    "/option-publish-drafts/{draft_id}/submit",
    response_model=ProductSyncCommandOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="옵션조합 상품 등록 요청(비동기)",
    description="초안(상품 공통값 + 전체 품목)을 확정 스냅샷으로 접수한다 - 이 API는 실제 채널 "
    "호출을 하지 않고 명령만 접수한다(202). 실제 등록은 스케줄러의 "
    "product_option_publish_dispatch_job이 수행하며, 처리 결과는 GET /api/products/"
    "sync-commands/{command_id}로 폴링해 확인해야 한다.",
    responses={
        404: {"description": "초안을 찾을 수 없습니다."},
        409: {"description": "이미 이 채널에 등록된 SKU가 있습니다."},
        503: {"description": "옵션조합 상품 등록 기능이 비활성화(OFF) 상태입니다(실계정 검증 승인 전)."},
    },
)
def submit_option_publish_draft(draft_id: int, db: Session = Depends(get_db)) -> ProductSyncCommandOut:
    service = ProductOptionPublishService(db)
    try:
        outcome = service.enqueue_create(draft_id)
    except ProductOptionPublishDisabledError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)) from e
    except ProductOptionPublishDraftNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except ProductOptionPublishAlreadyRegisteredError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    db.commit()
    return ProductSyncCommandOut(
        command_id=outcome.command.id,
        status=outcome.command.status,
        already_processed=outcome.already_processed,
        error_code=outcome.command.error_code,
    )


class OptionRegistrationStatusItemOut(BaseModel):
    product_option_id: int
    seller_product_code: Optional[str] = None
    mapped: bool
    channel_option_id: Optional[str] = None
    ambiguous: bool = False


class OptionRegistrationStatusOut(BaseModel):
    channel_status_name: Optional[str] = None
    overall_status: str
    items: list[OptionRegistrationStatusItemOut] = []


@router.get(
    "/option-publish-drafts/{draft_id}/registration-status",
    response_model=OptionRegistrationStatusOut,
    summary="옵션조합 상품의 채널 심사/승인 상태 및 품목별 매핑 조회",
    description="채널에 재조회해 우리가 등록 시 보낸 판매자 관리코드와 정확히 일치하는 품목만 "
    "자동으로 매핑을 확정한다(배열 순서/이름 유사도로 추정하지 않는다). 모호하거나(ambiguous) "
    "응답에 없는 품목은 매핑 미확정으로 남고, POST .../confirm-item-mapping으로 운영자가 직접 "
    "확정할 수 있다. overall_status: PENDING_REVIEW(0건 확정)/PARTIALLY_MAPPED(일부)/"
    "FULLY_MAPPED(전부 확정 - 이때만 등록 완료로 표시해야 한다).",
    responses={
        400: {"description": "등록 접수된 상품 단위 식별자가 없습니다."},
        404: {"description": "초안을 찾을 수 없습니다."},
    },
)
def get_option_publish_registration_status(draft_id: int, db: Session = Depends(get_db)) -> OptionRegistrationStatusOut:
    service = ProductOptionPublishService(db)
    try:
        result = service.check_registration_status(draft_id)
    except ProductOptionPublishDraftNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    db.commit()  # 자동 매칭으로 새로 생성된 매핑을 확정한다.
    return OptionRegistrationStatusOut(**result)


class ConfirmOptionItemMappingRequest(BaseModel):
    product_option_id: int
    channel_option_id: str


@router.post(
    "/option-publish-drafts/{draft_id}/confirm-item-mapping",
    response_model=ProductPlatformMapOut,
    summary="옵션조합 상품의 SKU 1개에 대해 승인된 옵션 단위 식별자로 매핑 확정",
    description="GET .../registration-status로 확인한 옵션 단위 식별자를 운영자가 직접 지정해 이 "
    "SKU 하나의 플랫폼 매핑을 만든다 - 이 API가 채널 응답에서 자동으로 하나를 골라 추측하지 않고, "
    "지정값이 실제로 이 SKU의 판매자 관리코드에 대응하는 후보인지 매번 서버에 재확인한다.",
    responses={
        400: {"description": "확정할 수 없는 식별자이거나 이미 매핑이 존재합니다."},
        404: {"description": "초안 또는 품목을 찾을 수 없습니다."},
    },
)
def confirm_option_publish_item_mapping(
    draft_id: int, payload: ConfirmOptionItemMappingRequest, db: Session = Depends(get_db)
) -> ProductPlatformMapOut:
    service = ProductOptionPublishService(db)
    try:
        mapping = service.confirm_item_mapping(draft_id, payload.product_option_id, payload.channel_option_id)
    except (ProductOptionPublishDraftNotFoundError, ProductOptionPublishItemNotFoundError) as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    db.commit()
    return _to_platform_map_out(mapping, ProductPlatformMapRepository(db))
