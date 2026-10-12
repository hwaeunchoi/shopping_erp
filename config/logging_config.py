"""
config/logging_config.py
--------------------------
logs/{yyyy-mm-dd}.log 형태로 날짜별 로그 파일을 남기는 로깅 설정.

DB의 system_logs 테이블은 "화면에서 조회하기 위한" 요약 로그이고,
이 파일 로그는 "장애 발생 시 원본 스택트레이스까지 확인하기 위한" 상세
로그로, 서로 역할을 이원화한다 (폴더구조설계 3장 매핑표 참고).
"""

import logging
import sys
from logging.handlers import TimedRotatingFileHandler

from config.settings import settings

# 외부 HTTP 클라이언트 라이브러리는 INFO에 요청 전체 URL(쇼핑몰 vendorId, 경로의 식별자, 조회 조건)을, DEBUG에 요청 헤더까지
# 남긴다. 어느 환경(debug 포함)에서도 WARNING 미만은 막는다. 오류는 앱의 안전한 reason-code 로그로만 남긴다.
OUTBOUND_HTTP_LOGGERS = ("httpx", "httpcore", "urllib3")
OUTBOUND_HTTP_LOG_LEVEL = logging.WARNING


def quiet_outbound_http_loggers() -> None:
    for name in OUTBOUND_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(OUTBOUND_HTTP_LOG_LEVEL)


def setup_logging() -> None:
    """애플리케이션 시작 시 1회 호출하여 로깅을 초기화한다."""
    settings.logs_dir.mkdir(parents=True, exist_ok=True)

    log_format = logging.Formatter(fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    file_handler = TimedRotatingFileHandler(
        filename=settings.logs_dir / "erp.log",
        when="midnight",
        backupCount=90,  # 90일치 보관, 이후 backup_retention 정책과 별개로 로그는 넉넉히 유지
        encoding="utf-8",
    )
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setFormatter(log_format)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(log_format)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG if settings.debug else logging.INFO)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    quiet_outbound_http_loggers()
