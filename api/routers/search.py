"""
api/routers/search.py
--------------------------
UI v1.1 글로벌 통합검색(상단바). 전용 권한 없이 로그인한 사용자면 사용할 수
있다(customers.py와 동일한 방침).
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.deps import get_current_user, get_db
from services.search_service import SearchResults, SearchService

router = APIRouter(prefix="/api/search", tags=["search"], dependencies=[Depends(get_current_user)])


class SearchResultItemOut(BaseModel):
    type: str
    id: int
    label: str
    sublabel: str


class SearchResultsOut(BaseModel):
    orders: list[SearchResultItemOut]
    products: list[SearchResultItemOut]
    product_options: list[SearchResultItemOut]
    customers: list[SearchResultItemOut]
    shipments: list[SearchResultItemOut]


def _to_out(results: SearchResults) -> SearchResultsOut:
    return SearchResultsOut(
        orders=[SearchResultItemOut(**vars(i)) for i in results.orders],
        products=[SearchResultItemOut(**vars(i)) for i in results.products],
        product_options=[SearchResultItemOut(**vars(i)) for i in results.product_options],
        customers=[SearchResultItemOut(**vars(i)) for i in results.customers],
        shipments=[SearchResultItemOut(**vars(i)) for i in results.shipments],
    )


@router.get(
    "",
    response_model=SearchResultsOut,
    summary="통합검색",
    description="주문번호/상품명/SKU/고객명·전화번호/송장번호를 동시에 검색해 카테고리별로 최대 5건씩 반환한다. "
    "빈 문자열이면 모든 카테고리가 빈 목록으로 반환된다.",
)
def search(q: str, db: Session = Depends(get_db)) -> SearchResultsOut:
    return _to_out(SearchService(db).search(q))
