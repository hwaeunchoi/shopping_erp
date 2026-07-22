"""
services/search_service.py
------------------------------
UI v1.1 글로벌 통합검색(상단바). orders/products/product_options/customers/
shipments를 동시 조회해 카테고리별로 묶어 반환한다(UI와이어프레임_v1.1_추가반영.md
1장). 전용 검색 인덱스 테이블 없이 기존 테이블을 조합 조회하는 설계다.
"""

from dataclasses import dataclass, field

from repositories.customer_repository import CustomerRepository
from repositories.order_repository import OrderRepository, ShipmentRepository
from repositories.product_repository import ProductOptionRepository, ProductRepository

RESULTS_PER_CATEGORY = 5


@dataclass
class SearchResultItem:
    type: str  # ORDER/PRODUCT/PRODUCT_OPTION/CUSTOMER/SHIPMENT
    id: int
    label: str
    sublabel: str = ""


@dataclass
class SearchResults:
    orders: list[SearchResultItem] = field(default_factory=list)
    products: list[SearchResultItem] = field(default_factory=list)
    product_options: list[SearchResultItem] = field(default_factory=list)
    customers: list[SearchResultItem] = field(default_factory=list)
    shipments: list[SearchResultItem] = field(default_factory=list)


class SearchService:
    def __init__(self, session) -> None:
        self.order_repo = OrderRepository(session)
        self.product_repo = ProductRepository(session)
        self.option_repo = ProductOptionRepository(session)
        self.customer_repo = CustomerRepository(session)
        self.shipment_repo = ShipmentRepository(session)

    def search(self, keyword: str) -> SearchResults:
        keyword = keyword.strip()
        if not keyword:
            return SearchResults()

        orders = [
            SearchResultItem(type="ORDER", id=o.id, label=o.platform_order_no, sublabel=o.status)
            for o in self.order_repo.search(keyword, limit=RESULTS_PER_CATEGORY)
        ]
        products = [
            SearchResultItem(type="PRODUCT", id=p.id, label=p.name, sublabel=p.category or "")
            for p in self.product_repo.search(keyword, limit=RESULTS_PER_CATEGORY)
        ]
        product_options = [
            SearchResultItem(type="PRODUCT_OPTION", id=o.id, label=o.sku_code, sublabel=str(o.product_id))
            for o in self.option_repo.search(keyword, limit=RESULTS_PER_CATEGORY)
        ]
        customers = [
            SearchResultItem(type="CUSTOMER", id=c.id, label=c.name or "(이름없음)", sublabel=c.phone or "")
            for c in self.customer_repo.search(keyword, limit=RESULTS_PER_CATEGORY)
        ]
        shipments = [
            SearchResultItem(type="SHIPMENT", id=s.id, label=s.tracking_no or "(송장없음)", sublabel=s.carrier or "")
            for s in self.shipment_repo.list_filtered(search=keyword, limit=RESULTS_PER_CATEGORY)
        ]

        return SearchResults(
            orders=orders, products=products, product_options=product_options, customers=customers, shipments=shipments
        )
