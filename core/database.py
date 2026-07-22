"""
core/database.py
------------------
SQLAlchemy 엔진 및 세션을 생성한다.

설계 원칙(ERD v1.0 0.6): Repository 계층만 이 모듈에 의존하고, Service/API
계층은 세션을 직접 다루지 않는다. 이렇게 하면 SQLite → PostgreSQL 전환 시
아래 create_engine() 호출부(및 .env의 DATABASE_URL)만 수정하면 된다.

SQLite 특이사항:
- 기본적으로 스레드 간 연결 공유가 제한되므로 check_same_thread=False로 완화한다
  (FastAPI는 요청마다 별도 스레드/코루틴에서 세션을 사용할 수 있음).
- 외래키 제약은 SQLite에서 기본 비활성화이므로 PRAGMA foreign_keys=ON으로 켠다.
"""

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from config.settings import settings

_is_sqlite = settings.database_url.startswith("sqlite")
_connect_args = {"check_same_thread": False} if _is_sqlite else {}

engine = create_engine(settings.database_url, connect_args=_connect_args, echo=settings.debug, future=True)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
        """SQLite 연결마다 외래키 제약을 활성화한다.

        이 모듈이 만든 engine 인스턴스에만 등록한다 - 예전에는 전역 Engine
        클래스에 리스너를 걸고 콜백 안에서 매번 settings.database_url을
        다시 확인했는데, 이러면 프로세스 안에서 만들어지는 다른 모든
        SQLAlchemy 엔진(테스트용 엔진, Alembic 엔진 등)에도 콜백이 걸려
        불필요하게 실행된다. PostgreSQL에서는 이 리스너 자체가 등록되지
        않는다.
        """
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 의존성 주입용 세션 제너레이터.

    사용 예:
        @router.get("/orders")
        def list_orders(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """스케줄러 잡 등 FastAPI 요청 컨텍스트 밖에서 사용하는 세션 컨텍스트 매니저.

    사용 예:
        with session_scope() as db:
            db.add(obj)
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
