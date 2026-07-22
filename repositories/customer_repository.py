"""
repositories/customer_repository.py
--------------------------------------
ERD 2.3 고객 그룹(customers)에 대한 Repository.
"""

from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from models.customer import Customer
from repositories.base_repository import BaseRepository


class CustomerRepository(BaseRepository[Customer]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Customer)

    def get_by_platform_key(self, platform_id: int, platform_customer_key: str) -> Optional[Customer]:
        stmt = select(Customer).where(
            Customer.platform_id == platform_id, Customer.platform_customer_key == platform_customer_key
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def list_dormant(self) -> list[Customer]:
        stmt = select(Customer).where(Customer.is_dormant.is_(True))
        return list(self.session.execute(stmt).scalars().all())

    def list_vip(self) -> list[Customer]:
        stmt = select(Customer).where(Customer.is_vip.is_(True))
        return list(self.session.execute(stmt).scalars().all())

    def search(self, keyword: str, limit: int = 5) -> list[Customer]:
        """SRS UI v1.1 통합검색: 고객명/전화번호로 검색한다."""
        like = f"%{keyword}%"
        stmt = (
            select(Customer)
            .where(Customer.is_deleted.is_(False), or_(Customer.name.ilike(like), Customer.phone.ilike(like)))
            .limit(limit)
        )
        return list(self.session.execute(stmt).scalars().all())
