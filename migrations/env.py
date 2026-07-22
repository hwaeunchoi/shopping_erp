"""
migrations/env.py
-------------------
Alembic이 마이그레이션을 생성/실행할 때 사용하는 환경 스크립트.

- target_metadata를 models.Base.metadata로 지정하여, `alembic revision
  --autogenerate` 실행 시 models/ 아래 50개 테이블 정의를 전부 인식하게 한다.
- DB 접속 정보는 alembic.ini가 아니라 config/settings.py(.env)를 사용하여,
  애플리케이션 설정과 마이그레이션 설정이 항상 일치하도록 한다.
"""

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 프로젝트 루트를 sys.path에 추가 (models, config를 import하기 위함)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from models import Base  # noqa: E402  (모든 모델을 import하는 models/__init__.py)

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """DB에 접속하지 않고 SQL 스크립트만 생성하는 모드."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url, target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"}
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """실제 DB에 접속하여 마이그레이션을 적용하는 모드 (일반적으로 이 모드 사용)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
