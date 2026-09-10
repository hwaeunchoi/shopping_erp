# Dockerfile
# -----------
# FastAPI 백엔드 실행용 이미지.
#
# venv(로컬 개발용)와는 별개로, 컨테이너 이미지에는 requirements.txt를
# 그대로 설치한다. DB 접속 정보는 이미지에 내장하지 않고 docker-compose.yml의
# environment 값(DATABASE_URL 등)을 컨테이너 실행 시점에 주입받는다 - 이는
# core/database.py가 이미 "DATABASE_URL 값만 바꾸면 SQLite/PostgreSQL 전환이
# 가능하도록" 설계되어 있음을 그대로 활용한 것이다(3순위 PostgreSQL 호환성
# 점검에서 확인됨).
#
# 스케줄러(scheduler/scheduler.py)는 이 이미지에 포함되어 있지만 기본 CMD는
# API 서버만 기동한다. 별도 컨테이너로 스케줄러를 띄우고 싶다면 동일 이미지에
# command만 다르게 지정하면 된다(현재 docker-compose.yml에는 미포함 - 5순위
# 범위는 "FastAPI + PostgreSQL 함께 실행"으로 한정).

FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1

# PostgreSQL 클라이언트 도구(pg_dump/pg_restore) - services/postgres_backup_service.py
# (상용 ERP 확장, 기본 비활성화)가 scheduler 컨테이너 안에서 실행한다. 운영
# db 서비스(docker-compose.yml)가 postgres:16-alpine이므로 서버 major
# version과 정확히 맞춘 16번대 클라이언트를 PGDG 공식 apt 저장소에서 설치한다
# (Debian 기본 저장소는 이미지 베이스(python:3.11-slim)의 Debian 릴리스에 따라
# 16번대 클라이언트가 없을 수 있다 - PGDG는 릴리스와 무관하게 특정 PostgreSQL
# major version 클라이언트를 안정적으로 제공한다). db 이미지 태그를 다른 major
# version으로 올리면 아래 postgresql-client-16도 함께 맞춰 바꿔야 한다.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && . /etc/os-release \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/*

# requirements.txt만 먼저 복사해 의존성 레이어를 캐시한다
# (소스 코드만 바뀌었을 때 pip install을 다시 하지 않기 위함)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 비root 사용자로 실행 (운영 안정성 - 컨테이너 루트 권한 최소화)
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p logs backup backup/postgres reports/generated reports/templates \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
