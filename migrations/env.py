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
# Alembic Config는 내부적으로 configparser.ConfigParser(BasicInterpolation)를
# 쓴다 - percent-encoded 비밀번호가 포함된 DATABASE_URL(예: %40, %3A)을 그대로
# set_main_option()에 넘기면, 이후 run_migrations_offline()의
# get_main_option()이나 run_migrations_online()의 get_section()이 값을
# 읽어올 때(둘 다 동일한 interpolation 엔진을 거친다) 단독 "%" 문자를 보간
# 참조(%(name)s)의 시작으로 오인해 InterpolationSyntaxError를 던진다.
# alembic.ini의 file_template(%%(year)d...)이 이미 쓰는 것과 동일한 "%%는
# 리터럴 %"라는 ConfigParser 공식 이스케이프 규칙을 여기서도 그대로 적용한다 -
# 저장 시 한 번만 이스케이프해두면 아래 두 함수는 수정 없이 원래 URL을 그대로
# 돌려받는다(비밀번호 문자를 제한/제거/재인코딩하지 않는다 - % 자체만
# 이스케이프해 ConfigParser를 통과시킬 뿐, 값 자체는 그대로다).
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

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
