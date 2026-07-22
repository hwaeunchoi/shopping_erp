"""
api/routers/suppliers.py
----------------------------
공급처(매입처) 관리: 공급처 CRUD + 담당자 CRUD + 상품옵션↔공급처 매핑.
SUPPLIER_MANAGE 권한 하나로 조회/변경 모두 통제한다(별도 VIEW 권한 미정의).
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.deps import get_db, require_permission
from models.supplier import ProductSupplierMap, Supplier, SupplierContact
from repositories.product_repository import ProductOptionRepository
from repositories.supplier_repository import ProductSupplierMapRepository, SupplierContactRepository, SupplierRepository

router = APIRouter(
    prefix="/api/suppliers", tags=["suppliers"], dependencies=[Depends(require_permission("SUPPLIER_MANAGE"))]
)


class SupplierOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    business_no: Optional[str] = None
    bank_name: Optional[str] = None
    bank_account_no: Optional[str] = None
    bank_account_holder: Optional[str] = None
    payment_terms: Optional[str] = None
    is_active: bool


class SupplierCreateRequest(BaseModel):
    name: str
    business_no: Optional[str] = None
    bank_name: Optional[str] = None
    bank_account_no: Optional[str] = None
    bank_account_holder: Optional[str] = None
    payment_terms: Optional[str] = None


class SupplierUpdateRequest(BaseModel):
    name: Optional[str] = None
    business_no: Optional[str] = None
    bank_name: Optional[str] = None
    bank_account_no: Optional[str] = None
    bank_account_holder: Optional[str] = None
    payment_terms: Optional[str] = None
    is_active: Optional[bool] = None


class SupplierContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    supplier_id: int
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    is_primary: bool


class SupplierContactRequest(BaseModel):
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    is_primary: bool = False


class ProductSupplierMapOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_option_id: int
    supplier_id: int
    is_primary: bool


class ProductSupplierMapRequest(BaseModel):
    product_option_id: int
    supplier_id: int
    is_primary: bool = True


@router.get(
    "",
    response_model=list[SupplierOut],
    summary="공급처 목록 조회",
    description="active_only=true로 지정하면 사용 중인(is_active=true) 공급처만 반환한다.",
)
def list_suppliers(active_only: bool = False, db: Session = Depends(get_db)) -> list:
    repo = SupplierRepository(db)
    return repo.list_active() if active_only else repo.list_all(limit=500)


@router.post("", response_model=SupplierOut, status_code=status.HTTP_201_CREATED, summary="공급처 등록")
def create_supplier(payload: SupplierCreateRequest, db: Session = Depends(get_db)):
    supplier = SupplierRepository(db).add(Supplier(**payload.model_dump()))
    db.commit()
    return supplier


@router.get(
    "/{supplier_id}",
    response_model=SupplierOut,
    summary="공급처 단건 조회",
    responses={404: {"description": "공급처를 찾을 수 없습니다."}},
)
def get_supplier(supplier_id: int, db: Session = Depends(get_db)):
    supplier = SupplierRepository(db).get_by_id(supplier_id)
    if supplier is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="공급처를 찾을 수 없습니다.")
    return supplier


@router.patch(
    "/{supplier_id}",
    response_model=SupplierOut,
    summary="공급처 정보 수정",
    description="is_active=false로 지정하면 공급처를 비활성화한다(삭제 대신 - 발주/매핑 이력 보존).",
    responses={404: {"description": "공급처를 찾을 수 없습니다."}},
)
def update_supplier(supplier_id: int, payload: SupplierUpdateRequest, db: Session = Depends(get_db)):
    supplier = SupplierRepository(db).get_by_id(supplier_id)
    if supplier is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="공급처를 찾을 수 없습니다.")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(supplier, field, value)
    db.commit()
    return supplier


@router.get(
    "/{supplier_id}/contacts",
    response_model=list[SupplierContactOut],
    summary="공급처 담당자 목록 조회",
    responses={404: {"description": "공급처를 찾을 수 없습니다."}},
)
def list_supplier_contacts(supplier_id: int, db: Session = Depends(get_db)) -> list:
    if SupplierRepository(db).get_by_id(supplier_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="공급처를 찾을 수 없습니다.")
    return SupplierContactRepository(db).list_by_supplier(supplier_id)


@router.post(
    "/{supplier_id}/contacts",
    response_model=SupplierContactOut,
    status_code=status.HTTP_201_CREATED,
    summary="공급처 담당자 등록",
    responses={404: {"description": "공급처를 찾을 수 없습니다."}},
)
def create_supplier_contact(supplier_id: int, payload: SupplierContactRequest, db: Session = Depends(get_db)):
    if SupplierRepository(db).get_by_id(supplier_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="공급처를 찾을 수 없습니다.")

    contact = SupplierContactRepository(db).add(SupplierContact(supplier_id=supplier_id, **payload.model_dump()))
    db.commit()
    return contact


@router.delete(
    "/{supplier_id}/contacts/{contact_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="공급처 담당자 삭제",
    responses={404: {"description": "담당자를 찾을 수 없습니다."}},
)
def delete_supplier_contact(supplier_id: int, contact_id: int, db: Session = Depends(get_db)) -> None:
    repo = SupplierContactRepository(db)
    contact = repo.get_by_id(contact_id)
    if contact is None or contact.supplier_id != supplier_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="담당자를 찾을 수 없습니다.")
    repo.delete(contact)
    db.commit()


@router.get(
    "/product-options/{product_option_id}/suppliers",
    response_model=list[ProductSupplierMapOut],
    summary="상품옵션(SKU)에 매핑된 공급처 목록 조회",
)
def list_suppliers_for_product_option(product_option_id: int, db: Session = Depends(get_db)) -> list:
    return ProductSupplierMapRepository(db).list_by_product_option(product_option_id)


@router.post(
    "/product-options/map",
    response_model=ProductSupplierMapOut,
    status_code=status.HTTP_201_CREATED,
    summary="상품옵션(SKU)에 공급처 매핑 추가",
    description="이미 같은 조합의 매핑이 있으면 새로 만들지 않고 기존 매핑을 그대로 반환한다(멱등).",
    responses={404: {"description": "상품옵션 또는 공급처를 찾을 수 없습니다."}},
)
def map_supplier_to_product_option(payload: ProductSupplierMapRequest, db: Session = Depends(get_db)):
    if ProductOptionRepository(db).get_by_id(payload.product_option_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="상품옵션을 찾을 수 없습니다.")
    if SupplierRepository(db).get_by_id(payload.supplier_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="공급처를 찾을 수 없습니다.")

    repo = ProductSupplierMapRepository(db)
    existing = repo.get_by_option_and_supplier(payload.product_option_id, payload.supplier_id)
    if existing is not None:
        return existing

    mapping = repo.add(ProductSupplierMap(**payload.model_dump()))
    db.commit()
    return mapping


@router.delete(
    "/product-options/map/{map_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="상품옵션↔공급처 매핑 삭제",
    responses={404: {"description": "매핑을 찾을 수 없습니다."}},
)
def delete_product_supplier_map(map_id: int, db: Session = Depends(get_db)) -> None:
    repo = ProductSupplierMapRepository(db)
    mapping = repo.get_by_id(map_id)
    if mapping is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="매핑을 찾을 수 없습니다.")
    repo.delete(mapping)
    db.commit()
