# 배포 문서 (운영 환경 구축 가이드)

이 문서는 로컬 개발이 아닌 **운영(production) 배포**를 위한 안내입니다. 로컬
개발 환경 설정은 [README.md](README.md)를 참고하세요.

## 1. 구성 요소

`docker compose up --build` 한 번으로 아래 5개 컨테이너가 함께 기동됩니다.

| 서비스 | 이미지/빌드 | 역할 |
|---|---|---|
| `db` | `postgres:16-alpine` | 운영 DB(PostgreSQL) |
| `redis` | `redis:7-alpine` | 캐시/세션 스토어용으로 예약된 인프라(아래 "Redis 사용 현황" 참고 - 현재 애플리케이션 코드는 아직 연결하지 않음) |
| `api` | 루트 `Dockerfile` | FastAPI 백엔드(포트 8000) |
| `scheduler` | 루트 `Dockerfile`(동일 이미지, `command`만 다름) | `scheduler/scheduler.py` — 주문/광고 수집, 정산 확인, 손익 계산, 백업, 보고서 생성, 알림 평가 배치를 api와 분리된 프로세스로 실행 |
| `web` | `frontend/Dockerfile` | React 정적 빌드 + Nginx(포트 80) — `/api`, `/health`는 `api` 컨테이너로 리버스 프록시 |

```
docker compose up --build
```

최초 기동 시 `api` 컨테이너가 `alembic upgrade head` → `scripts/init_db.py`
(역할/권한/기본 admin 계정/플랫폼 5종/기본 창고 시딩) → `uvicorn` 순서로
동작합니다. 기동 완료 후:

- 웹 화면: `http://localhost/` (Nginx가 서빙)
- API 문서(Swagger): `http://localhost:8000/docs`
- 기본 관리자 계정: `admin` / `ChangeMe!123` — **최초 로그인 후 반드시 변경**

## 2. 배포 전 반드시 교체해야 하는 값

`docker-compose.yml`의 `environment` 항목은 전부 데모용 기본값입니다. 실제
배포 전 아래 값을 반드시 안전한 값으로 교체하세요(.env 파일로 분리해 관리하는
것을 권장합니다 — `.env.example` 참고).

| 환경변수 | 설명 | 교체 방법 |
|---|---|---|
| `POSTGRES_PASSWORD` | PostgreSQL 비밀번호 | 무작위 강력한 비밀번호로 교체 |
| `JWT_SECRET_KEY` | JWT 서명 키 | `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `CREDENTIAL_ENCRYPTION_KEY` | API Credential 암호화(Fernet) 마스터 키 | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` — **이 값을 분실/변경하면 기존에 저장된 모든 API Credential을 복호화할 수 없게 되므로 별도 안전한 곳에 백업 필수** |
| `admin` 계정 비밀번호 | 최초 로그인 후 설정 화면(사용자 관리)에서 변경 | UI에서 직접 변경 |

## 3. PostgreSQL 전환 확인 사항

- `core/database.py`는 `DATABASE_URL` 값만으로 SQLite/PostgreSQL을 전환하도록
  설계되어 있어 애플리케이션 코드 변경이 필요 없습니다.
- `migrations/versions/20260702_0955_91b961bb3be8_initial_schema_50_tables.py`
  (50개 테이블 전체)는 SQLAlchemy Core의 범용 타입(`sa.Boolean`, `sa.String`,
  `sa.DateTime`, `sa.Numeric` 등)만 사용하며 SQLite 전용 구문이 없어
  PostgreSQL에도 그대로 적용됩니다. `docker compose up`이 이를 자동으로
  실행합니다.
- `requirements.txt`의 `psycopg[binary]==3.2.1`이 PostgreSQL 드라이버입니다.
- **주의**: 이 개발 환경에는 Docker가 설치되어 있지 않아(`docker` 명령 없음)
  `docker compose up`을 직접 실행해 컨테이너가 실제로 기동되는지까지는
  검증하지 못했습니다. `docker-compose.yml`/`Dockerfile`/`frontend/Dockerfile`은
  YAML/문법 검토로 오류가 없음을 확인했고, alembic 마이그레이션 자체는
  로컬 SQLite pytest 스위트(308개 전체 통과)로 회귀가 없음을 확인했습니다.
  Docker가 설치된 환경에서 처음 `docker compose up --build`를 실행할 때
  이미지 빌드/컨테이너 기동에 문제가 있으면 로그를 공유해주시면 바로
  수정하겠습니다.

## 4. Redis 사용 현황 (중요)

`redis` 컨테이너는 운영 스택 구성 요소로 함께 기동되지만, **현재 애플리케이션
코드는 Redis에 실제로 연결하거나 사용하지 않습니다.** 캐시/세션/속도제한 등에
Redis를 실제로 활용하는 것은 새 기능 추가에 해당해 이번 "신규 기능 개발 중단,
운영 준비만 진행" 범위에서 의도적으로 제외했습니다. 필요해지면
`REDIS_URL`(이미 `api`/`scheduler` 컨테이너 환경변수로 주입되고 있음)을
읽어 클라이언트를 구성하는 별도 작업으로 진행하면 됩니다.

## 5. 실제 쇼핑몰 API 연동 현황 (중요)

`integrations/malls/*.py`, `integrations/ads/*.py`의 커넥터는 기본적으로
**더미(가짜) 데이터를 생성**합니다(실제 네이버/쿠팡/ESM/11번가/카카오/광고
플랫폼 API를 호출하지 않음). 이번 작업에서 네이버 스마트스토어
(`NaverSmartstoreConnector`) 1개 플랫폼에 한해 **연동 가능한 구조**를
추가했습니다.

- 설정 화면(`/settings` → API Credential 관리)에서 해당 플랫폼에
  `client_id`/`client_secret`을 등록하면(Fernet으로 암호화되어 저장됨),
  다음 주문 수집부터 네이버 커머스 API(OAuth2 client_credentials, bcrypt
  서명 방식)를 통한 실제 HTTP 호출을 시도합니다.
- 등록하지 않으면(기본값) 기존과 동일하게 더미 데이터로 동작합니다 — 즉
  실제 키를 등록하기 전까지는 운영 동작이 전혀 바뀌지 않습니다.
- **이 환경에는 실제 발급받은 네이버 커머스 API 키가 없어 라이브 API
  호출을 실제로 테스트하지는 못했습니다.** OAuth2 토큰 발급 흐름(서명 방식)은
  네이버 커머스 API 공식 문서 기준으로 구현했으나, 주문 조회 엔드포인트의
  정확한 경로와 응답 필드 매핑(`integrations/malls/naver_smartstore_connector.py`의
  `ORDER_LIST_PATH`, `_fetch_raw_orders_live()`)은 실제 키 발급 후 네이버
  개발자센터 문서로 재확인이 필요합니다. 테스트는 `httpx.MockTransport`로
  네트워크를 모의(mock)해 요청 구성(URL/헤더/서명)과 실패 시 에러 처리만
  검증했습니다(`tests/unit/test_naver_smartstore_connector.py`).
- 나머지 4개 쇼핑몰(쿠팡/ESM/11번가/카카오쇼핑)과 광고 플랫폼 3종은 여전히
  더미 전용입니다. 실제 계약/승인된 키를 확보하면 동일한 패턴
  (`_get_credentials()` → 있으면 실제 호출, 없으면 더미)으로 확장하면 됩니다.

## 6. 운영 테스트(실데이터 검증)에 대하여

실제 쇼핑몰 계정으로 주문을 수집해 검증하는 "운영 테스트"는 위 5번 항목과
동일한 이유로 실제 API 키/계약된 판매자 계정이 있어야만 가능합니다. 이번
범위에서는 진행하지 못했으며, 실제 키를 등록한 뒤 다음 순서로 검증하는 것을
권장합니다.

1. 설정 화면에서 API Credential 등록
2. 대시보드 "빠른실행 → 주문수집" 또는 `POST /api/orders/sync`로 1회 수동
   수집 실행 → 응답의 `created`/`updated`/`skipped_items` 건수 확인
3. 시스템 모니터링 → 연동상태 탭에서 해당 플랫폼이 🟢정상으로 바뀌는지 확인
4. 문제 발생 시 시스템 모니터링 → 작업이력 탭에서 오류 메시지 확인(모두
   `integration_status`/`notifications`/`task_execution_history`에 자동
   기록됨 - 별도 로그 파일을 뒤질 필요 없음)

## 7. 백업/복구

- `scheduler` 컨테이너가 매일 새벽 3시(UTC) `backup_job.run()`으로 DB를
  백업합니다(`erp_backup` named volume, 보관정책은 `BACKUP_RETENTION_DAYS`/
  `BACKUP_MAX_COUNT` 환경변수로 설정).
- PostgreSQL 전환 후에는 `backup_job.py`가 SQLite 파일 복사 방식이 아니라
  `pg_dump` 등으로 교체되어야 합니다 — 현재 `backup_job.py`는 SQLite 외
  DB에서는 `NotImplementedError`를 발생시키도록 명시적으로 막아뒀습니다
  (자세한 내용은 해당 파일 주석 참고). **PostgreSQL 운영 전환 시 반드시
  이 부분을 `pg_dump` 기반으로 교체해야 자동 백업이 동작합니다** — 이번
  "신규 기능 추가 금지" 범위상 구현하지 않고 리스크로 남깁니다.
