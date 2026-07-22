"""
models/base.py
---------------
모든 SQLAlchemy ORM 모델의 공통 베이스와 Mixin을 정의한다.

설계 원칙(ERD v1.0 0장) 반영:
- 모든 시간 컬럼은 UTC 저장을 원칙으로 한다 (표시 시점에 KST로 변환은 Service/API 계층 책임).
- 핵심 엔티티는 물리 삭제 대신 소프트 삭제(is_deleted)를 사용한다. (SoftDeleteMixin)
"""

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """DB 서버가 아닌 애플리케이션 레벨에서 UTC 시각을 생성한다.

    SQLite는 server_default=func.now()가 UTC를 보장하지 않는 환경이 있어
    애플리케이션 레벨 default를 사용해 PostgreSQL 전환 시에도 동일하게 동작하도록 한다.
    """
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """모든 ORM 모델의 공통 베이스 클래스."""

    pass


class TimestampMixin:
    """생성일/수정일 공통 컬럼. 대부분의 테이블에 적용."""

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class SoftDeleteMixin:
    """소프트 삭제 공통 컬럼. orders/products/customers 등 핵심 엔티티에 적용."""

    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
