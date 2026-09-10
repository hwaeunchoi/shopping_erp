"""
tests/unit/test_migrations_env_url_escaping.py
------------------------------------------------------
migrations/env.py가 DATABASE_URL을 Alembic Config에 넘기는 방식(fix(migrations):
support percent-encoded database URLs)을 검증한다.

migrations/env.py 자체는 import 시점에 Alembic 컨텍스트(context.config가
실제로 구성된 상태)를 요구하고 즉시 run_migrations_online()/offline()을
실행하는 스크립트라 일반 모듈처럼 import해 단위테스트하기 어렵다(이 저장소의
기존 방침과도 일치한다 - 마이그레이션 "실행" 자체의 검증은 격리 PostgreSQL
라운드트립으로 한다, tests/integration/test_alembic_percent_encoded_url_pg.py
참고). 이 파일은 그 스크립트가 실제로 의존하는 메커니즘 - "Alembic Config는
configparser.ConfigParser(BasicInterpolation)를 쓰므로 %를 %%로 이스케이프해
set_main_option()에 넣으면 get_main_option()/get_section() 양쪽 모두 원래
값을 그대로 돌려받는다" - 을 실제 alembic.ini와 진짜 Config 객체로 직접
검증한다(값을 재구현해 흉내내지 않는다).
"""

import logging

import pytest
from alembic.config import Config

REPO_ALEMBIC_INI = "alembic.ini"


# migrations/env.py의 수정된 한 줄과 정확히 동일한 표현식.
def _escape_for_configparser(url: str) -> str:
    return url.replace("%", "%%")


PERCENT_ENCODED_URLS = [
    # 대표 인코딩 조합: %25(리터럴 %), %40(@), %3A(:), %2F(/) 전부 포함.
    "postgresql+psycopg://erp_user:P%40ss%3Aw%2Frd%251@dbhost:5432/erp_db",
    "postgresql://u:%25@dbhost/db",  # 비밀번호 전체가 인코딩된 % 하나뿐인 극단 케이스.
    "postgresql://u:tail%25@dbhost/db",  # 문자열 끝이 %로 끝나는 케이스.
    "postgresql://u:%25%25double@dbhost/db",  # 연속된 %가 여러 번 나오는 케이스.
]

PLAIN_URLS = ["postgresql+psycopg://erp_user:hunter2@dbhost:5432/erp_db", "sqlite:///erp.db", "sqlite:///:memory:"]


class TestConfigParserRoundTrip:
    """migrations/env.py가 의존하는 이스케이프 메커니즘 자체를 검증한다."""

    @pytest.mark.parametrize("url", PERCENT_ENCODED_URLS)
    def test_percent_encoded_url_survives_get_main_option(self, url):
        """offline 모드(run_migrations_offline)가 쓰는 get_main_option() 경로."""
        cfg = Config(REPO_ALEMBIC_INI)
        cfg.set_main_option("sqlalchemy.url", _escape_for_configparser(url))
        assert cfg.get_main_option("sqlalchemy.url") == url

    @pytest.mark.parametrize("url", PERCENT_ENCODED_URLS)
    def test_percent_encoded_url_survives_get_section(self, url):
        """online 모드(run_migrations_online)가 쓰는 get_section() 경로 -
        engine_from_config()에 그대로 넘어가는 dict를 만드는 바로 그 호출이다."""
        cfg = Config(REPO_ALEMBIC_INI)
        cfg.set_main_option("sqlalchemy.url", _escape_for_configparser(url))
        section = cfg.get_section(cfg.config_ini_section, {})
        assert section["sqlalchemy.url"] == url

    @pytest.mark.parametrize("url", PLAIN_URLS)
    def test_plain_url_without_percent_is_unaffected(self, url):
        """% 문자가 아예 없는 URL(일반 PostgreSQL/SQLite)은 이스케이프가
        완전한 no-op이라 수정 전후 동작이 100% 동일해야 한다(회귀 없음)."""
        assert _escape_for_configparser(url) == url  # 변경 자체가 없다.

        cfg = Config(REPO_ALEMBIC_INI)
        cfg.set_main_option("sqlalchemy.url", _escape_for_configparser(url))
        assert cfg.get_main_option("sqlalchemy.url") == url
        section = cfg.get_section(cfg.config_ini_section, {})
        assert section["sqlalchemy.url"] == url

    def test_without_escaping_percent_encoded_url_raises(self):
        """수정 전 증상(회귀 감시용) - 이스케이프 없이 그대로 넣으면 여전히
        InterpolationSyntaxError류 예외가 남을 확인한다(실제로는 set_main_option()
        자체에서 즉시 발생한다 - configparser.BasicInterpolation.before_set()이
        저장 시점에 값을 검증하기 때문이다. get()에서만 늦게 터지는 게
        아니라는 뜻이다). 이 테스트가 실패로 바뀐다면(예외가 안 남)
        Alembic/ConfigParser 쪽 동작이 바뀐 것이므로 이 파일의 나머지
        가정을 재검토해야 한다."""
        cfg = Config(REPO_ALEMBIC_INI)
        raw_url = "postgresql+psycopg://erp_user:P%40ss%3Aword@dbhost:5432/erp_db"
        with pytest.raises(Exception) as exc_info:
            cfg.set_main_option("sqlalchemy.url", raw_url)  # 이스케이프 없이 그대로.
        assert "interpolat" in str(exc_info.value).lower()

    def test_missing_url_behavior_is_unchanged(self):
        """URL 자체가 비어있는 기존 실패 동작은 이스케이프와 무관하게 그대로다 -
        %가 없는 빈 문자열은 no-op이라 이전과 동일하게 그대로 저장/조회된다."""
        cfg = Config(REPO_ALEMBIC_INI)
        cfg.set_main_option("sqlalchemy.url", _escape_for_configparser(""))
        assert cfg.get_main_option("sqlalchemy.url") == ""

    def test_error_and_log_output_never_contain_raw_password(self, caplog):
        """이스케이프가 적용된 percent-encoded URL은 예외를 던지지 않지만,
        혹시 몰라 이 과정의 로그 출력에 비밀번호가 그대로 노출되지 않는지도
        확인한다(migrations/env.py는 URL을 print/log하는 코드를 담고 있지
        않다 - 이 테스트는 Alembic/ConfigParser 자신이 로그를 남기지 않는지
        확인하는 안전망이다)."""
        secret_marker = "VeryUniqueSecretMarker999"
        url = f"postgresql://u:{secret_marker}%40x@dbhost/db"
        with caplog.at_level(logging.DEBUG):
            cfg = Config(REPO_ALEMBIC_INI)
            cfg.set_main_option("sqlalchemy.url", _escape_for_configparser(url))
            _ = cfg.get_main_option("sqlalchemy.url")
            _ = cfg.get_section(cfg.config_ini_section, {})
        for record in caplog.records:
            assert secret_marker not in record.getMessage()

    def test_escaping_is_idempotent_free_of_double_escaping_when_reapplied_once(self):
        """set_main_option은 migrations/env.py 안에서 프로세스당 정확히 한 번만
        호출된다(모듈 최상단, import 시 1회) - 두 번 이스케이프해 넣는 실수가
        생기면 %%%%처럼 깨진 값이 나온다는 것만 확인해, 실수로 이중 적용되면
        바로 실패로 드러나게 한다."""
        url = "postgresql://u:P%40ss@dbhost/db"
        once = _escape_for_configparser(url)
        twice = _escape_for_configparser(once)
        assert once != twice  # 이중 적용은 다른 값이 된다 - env.py가 한 번만 호출해야 하는 이유.

        cfg = Config(REPO_ALEMBIC_INI)
        cfg.set_main_option("sqlalchemy.url", once)  # env.py와 동일하게 정확히 한 번.
        assert cfg.get_main_option("sqlalchemy.url") == url
