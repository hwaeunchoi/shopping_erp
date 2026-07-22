"""
api/routers/products.py
---------------------------
상품/SKU 조회 + 등록/수정, 플랫폼 매핑, 원가 이력 관리. SRS FR-PRD-01/02/03 대응.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.deps import get_db, require_permission
from integrations.malls import get_mall_connector
from repositories.inventory_repository import InventoryRepository
from repositories.order_repository import OrderRepository
from repositories.platform_repository import PlatformRepository
from repositories.product_repository import (
    ProductCostHistoryRepository,
    ProductImageRepository,
    ProductOptionRepository,
    ProductPlatformMapRepository,
    ProductRepository,
    UnmatchedPlatformItemRepository,
)
from services.product_service import ProductCostService, ProductService
from services.product_sync_service import ProductSyncService

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
    display_name: Optional[str]
    seller_product_code: Optional[str]


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
                    ProductPlatformMapOut.model_validate(m) for m in platform_map_repo.list_by_option(option.id)
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


@router.post(
    "/{product_id}/options",
    response_model=ProductOptionOut,
    status_code=status.HTTP_201_CREATED,
    summary="상품 옵션(SKU) 등록",
    responses={404: {"description": "상품을 찾을 수 없습니다."}, 409: {"description": "이미 존재하는 SKU 코드입니다."}},
)
def create_product_option(product_id: int, payload: ProductOptionCreate, db: Session = Depends(get_db)):
    if ProductRepository(db).get_by_id(product_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="상품을 찾을 수 없습니다.")
    option = ProductService(db).create_option(
        product_id,
        payload.sku_code,
        payload.option_name,
        payload.color,
        payload.size,
        payload.barcode,
        payload.unit_cost_price,
        payload.sale_price,
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
    responses={404: {"description": "옵션을 찾을 수 없습니다."}},
)
def update_product_option(option_id: int, payload: ProductOptionUpdate, db: Session = Depends(get_db)):
    option = ProductOptionRepository(db).get_by_id(option_id)
    if option is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="옵션을 찾을 수 없습니다.")
    updated = ProductService(db).update_option(
        option,
        payload.option_name,
        payload.color,
        payload.size,
        payload.barcode,
        payload.unit_cost_price,
        payload.sale_price,
    )
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
    return ProductPlatformMapRepository(db).list_by_option(option_id)


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
    return mapping


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
    return updated


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
