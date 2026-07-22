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

# requirements.txt만 먼저 복사해 의존성 레이어를 캐시한다
# (소스 코드만 바뀌었을 때 pip install을 다시 하지 않기 위함)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 비root 사용자로 실행 (운영 안정성 - 컨테이너 루트 권한 최소화)
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p logs backup reports/generated reports/templates \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
