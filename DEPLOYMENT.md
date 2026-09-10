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

## 2. 배포 전 반드시 채워야 하는 값

`docker-compose.yml`은 `POSTGRES_PASSWORD`/`DATABASE_URL`/`JWT_SECRET_KEY`/
`CREDENTIAL_ENCRYPTION_KEY`를 전부 `${VAR:?message}`(필수 변수) 문법으로
받습니다 — 리터럴 데모 기본값이 파일에 없으므로, `.env`(또는 셸 환경)에 이
값들을 채우지 않으면 `docker compose config`/`up` 자체가 즉시 오류로
실패합니다(fail-closed, 조용히 데모값으로 폴백하지 않음). **이 값들은 절대
Git에 커밋하지 않습니다** — `.env`는 `.gitignore`에 포함되어 있고,
`.env.example`에는 플레이스홀더와 생성 명령만 있습니다.

| 환경변수 | 설명 | 생성 방법 |
|---|---|---|
| `POSTGRES_PASSWORD` | PostgreSQL 비밀번호 | 무작위 강력한 비밀번호. `DATABASE_URL` 안의 비밀번호와 반드시 일치시킬 것 |
| `DATABASE_URL` | SQLAlchemy 연결 문자열 전체 | `postgresql+psycopg://erp_user:<POSTGRES_PASSWORD와 동일값>@db:5432/erp_db` — `POSTGRES_PASSWORD`와 문자열로 조합하지 않고 완성된 값을 그대로 넣는다(비밀번호에 URL 예약문자가 섞이면 조합 시 깨질 수 있어서) |
| `JWT_SECRET_KEY` | JWT 서명 키 | `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `CREDENTIAL_ENCRYPTION_KEY` | API Credential 암호화 마스터 키 | `python -c "import secrets; print(secrets.token_urlsafe(32))"` — Fernet 자체 포맷일 필요는 없다(`core/crypto.py`가 이 문자열을 SHA-256 해시 후 urlsafe base64로 변환해 Fernet 키로 파생한다, 8.1절 참고). **이 값을 분실하면 기존에 저장된 모든 API Credential을 복호화할 수 없으므로 별도 안전한 곳에 백업 필수** |
| `admin` 계정 비밀번호 | 최초 로그인 후 설정 화면(사용자 관리)에서 변경 | UI에서 직접 변경 |

`JWT_SECRET_KEY`와 `CREDENTIAL_ENCRYPTION_KEY`는 서로 다른 값이어야 하며,
둘 다 알려진 데모 기본값(`CHANGE_ME_IN_PRODUCTION` 등)이면 안 됩니다 — 이
조건은 `scheduler/scheduler.py` 시작 시 `core.crypto.validate_startup_secrets()`로
자동 검증되어, 위반 시 스케줄러가 어떤 작업도 시도하지 않고 즉시 종료됩니다
(fail-closed).

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
  백업합니다. DB 엔진에 따라 자동으로 구현이 갈립니다(`scheduler/jobs/backup_job.py`):
  - **SQLite**: 기존 그대로 DB 파일을 `erp_backup` named volume에 복사합니다
    (보관정책은 `BACKUP_RETENTION_DAYS`/`BACKUP_MAX_COUNT`).
  - **PostgreSQL**: `services/postgres_backup_service.py`가 `pg_dump
    --format=custom`으로 백업하고 `pg_restore --list`로 구조를 검증합니다
    (아래 7.1 참고). **기본값은 비활성화(OFF)입니다** — 운영자가 명시적으로
    켜기 전에는 scheduler가 매일 이 잡을 실행은 하지만 즉시
    `skipped_disabled`로 끝나고, DB 세션도 pg_dump도 전혀 실행하지 않습니다.

### 7.1 PostgreSQL 예약 백업 활성화 절차

**기본 OFF입니다.** 아래 절차를 완료하기 전까지는 `POSTGRES_BACKUP_ENABLED`를
`true`로 바꾸지 마십시오.

**1) 호스트 백업 디렉터리 준비 (Windows)**

저장소 안(`./backup/postgres`, 기본값)이 아니라 **저장소 밖의 전용 디렉터리**를
쓰는 것을 권장합니다. 예: `C:\erp_backups\postgres`.

```powershell
New-Item -ItemType Directory -Force -Path "C:\erp_backups\postgres"
$acl = Get-Acl "C:\erp_backups\postgres"
$acl.SetAccessRuleProtection($true, $false)
$acl.SetOwner([System.Security.Principal.WindowsIdentity]::GetCurrent().User)
foreach ($id in @($env:USERNAME, 'NT AUTHORITY\SYSTEM', 'BUILTIN\Administrators')) {
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($id, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $acl.AddAccessRule($rule)
}
Set-Acl -Path "C:\erp_backups\postgres" -AclObject $acl
```

> Docker Desktop(Windows)에서 컨테이너는 이 디렉터리에 `appuser`(uid 1000)
> 권한으로 씁니다. 컨테이너 내부 `chmod 0700`은 이 모듈이 시도하지만
> **Windows 호스트 bind mount는 컨테이너 내부 chmod만으로 완전히 제어되지
> 않습니다** — 위 ACL 설정(관리자·SYSTEM·현재 사용자로 제한)이 실질적인
> 접근 통제입니다. 이 디렉터리를 다른 사용자와 공유하거나 클라우드 동기화
> 폴더(OneDrive 등) 안에 두지 마십시오 — 백업에는 전체 DB 스키마와 데이터가
> 들어 있습니다(암호화되지 않은 custom-format dump).

**2) `.env`에 값 추가**

```
POSTGRES_BACKUP_ENABLED=true
POSTGRES_BACKUP_HOST_DIR=C:\erp_backups\postgres
POSTGRES_BACKUP_RETENTION_COUNT=5
POSTGRES_BACKUP_TIMEOUT_SECONDS=600
```

- `POSTGRES_BACKUP_RETENTION_COUNT`는 최소 2 이상이어야 합니다(1 이하는
  `services/postgres_backup_service.py`가 fail-closed로 거부합니다 - 방금
  만든 백업 검증 직후 직전 정상 백업까지 한꺼번에 사라질 위험을 막기
  위함입니다).
- `POSTGRES_BACKUP_TIMEOUT_SECONDS`는 실제 DB 크기에 맞춰 넉넉하게
  잡으십시오 - 초과 시 `pg_dump` 자식 프로세스가 즉시 종료되고
  `DUMP_TIMEOUT`으로 기록됩니다.

**3) candidate 이미지 재빌드 및 scheduler만 재기동**

이 기능은 Dockerfile에 `postgresql-client-16`(운영 db와 동일한 major
version)을 추가했으므로, 기존 이미지를 그대로 쓰면 `pg_dump`/`pg_restore`가
없어 `TOOL_MISSING`으로 실패합니다. candidate 이미지를 새로 빌드하고
scheduler 컨테이너에 적용해야 합니다(release-candidate 검증 절차와 동일 -
기존 rollback 태그 보존, 운영 DB/컨테이너 나머지는 그대로).

**4) 수동 1회 dry-run 검증 (운영 활성화 전 필수)**

운영 컨테이너를 재기동하기 전에, **격리된 임시 PostgreSQL**로 먼저
확인하십시오(운영 DB에는 어떤 검증 단계에서도 연결하지 마십시오):

```bash
python -m pytest tests/integration/test_postgres_backup_restore_pg.py -v
```

이 테스트가 실제로 하는 일:
1. 격리 네트워크에 SOURCE/TARGET PostgreSQL 컨테이너 두 개를 만든다.
2. SOURCE에 `alembic upgrade head` + 합성 시드 데이터를 넣는다.
3. 이 저장소의 Dockerfile로 빌드한 러너 이미지 안에서
   `postgres_backup_service.run_backup_job()`을 실제로 실행한다.
4. 만들어진 dump에 `pg_restore --list`가 통과했는지 확인한다(백업 자체가
   이미 이 단계를 거치지만, 테스트가 독립적으로 다시 확인한다).
5. **별도의 빈 TARGET DB로 실제 복원**을 수행하고, Alembic revision·테이블
   수·권한 코드 집합 해시·역할별 권한 개수·플랫폼 활성 상태가 SOURCE와
   완전히 일치하는지 비교한다.
6. 동시 실행 방지(advisory lock)를 검증한다.
7. 끝나면 컨테이너·네트워크·볼륨·이미지를 전부 삭제한다.

운영 전환 전에는 **반드시** 이 테스트가 통과함을 확인하고, 운영과 최대한
비슷한 조건(비슷한 DB 크기)에서 한 번 더 별도 격리 환경으로 재현해보는
것을 권장합니다.

**5) 첫 실제 백업 후 확인**

- 운영 대시보드(스케줄러 잡 상태)에서 `backup` job이 더 이상 FAILED가
  아닌지 확인합니다.
- `POSTGRES_BACKUP_HOST_DIR`에 `erp_postgres_<타임스탬프>_UTC.dump` +
  `.sha256` + `.manifest.json` 세 파일이 생겼는지 확인합니다.
- `.sha256` 파일의 해시가 실제 dump 파일과 일치하는지
  (`sha256sum -c` 또는 동등한 도구로) 별도로 재확인하는 것을 권장합니다.

### 7.2 용량 모니터링

- `POSTGRES_BACKUP_RETENTION_COUNT`개까지만 보존되지만, 각 dump 크기는
  DB가 커질수록 늘어납니다 - 호스트 디스크 여유 공간을 정기적으로
  확인하십시오(`erp_backup` named volume/Docker 전체 디스크 사용량과는
  별개입니다).
- retention 정리가 실패해도(예: 파일 잠금) 백업 자체는 `PARTIAL_SUCCESS`로
  성공 처리되고 새 백업 파일은 보존됩니다 - 이 경우 오래된 백업이 계속
  쌓일 수 있으므로 `backup_history` 테이블의 `status='PARTIAL_SUCCESS'`
  행과 `error_code='RETENTION_FAILURE'`를 주기적으로 확인하십시오.

### 7.3 중요 - 백업 성공이 복원 성공을 보장하지 않는다

이 기능은 `pg_dump` 종료코드 확인, 결과 파일이 비어 있지 않은지 확인,
`pg_restore --list`로 아카이브 구조가 읽히는지 확인, SHA-256 무결성까지
확인합니다 - 그러나 이는 **"복원 가능한 형태로 파일이 만들어졌다"**는
것만 보증할 뿐, 실제 재해복구 시나리오에서 정상 복원되어 서비스가
재개된다는 것까지 자동으로 보증하지 않습니다. 실제 운영 DB의 복원은:

- 반드시 별도 승인을 받은 별개 작업으로 진행하십시오(이 기능 자체는
  복원을 수행하지 않습니다 - `pg_restore`를 운영 DB에 실행하는 코드는
  이 저장소 어디에도 없습니다).
- 주기적으로(예: 분기 1회) 실제 백업 파일 하나를 골라 격리된 임시
  PostgreSQL에 복원해보고 애플리케이션이 정상 기동하는지까지 확인하는
  것을 권장합니다(위 7.1의 dry-run 테스트와 같은 방식이되, 실제 최신
  운영 백업 파일을 입력으로 사용).

## 8. Secret 회전 절차 (JWT / Credential 암호화 키)

### 8.1 암호화 계약 (변경 금지)

`core/crypto.py`의 `build_fernet(secret)`이 앱 전체가 공유하는 **유일한**
키 파생 함수입니다:

```
raw secret 문자열 → SHA-256 다이제스트 → urlsafe base64 → Fernet 32바이트 키
```

`CREDENTIAL_ENCRYPTION_KEY`는 Fernet 자체 포맷(`Fernet.generate_key()`
결과물)일 필요가 없습니다 — 이 함수가 어떤 문자열이든 해시해 파생하기
때문입니다. **이 파생 규칙 자체를 바꾸면 기존에 저장된 모든 API Credential
암호문을 복호화할 수 없게 됩니다** — 절대 `Fernet(raw_secret)`을 직접
호출하지 않습니다. 앱의 `encrypt_value`/`decrypt_value`와
`scripts/rotate_credential_key.py`(구키·신키를 동시에 다뤄야 하는 회전
도구)는 반드시 이 함수 하나만 거칩니다.

### 8.2 사전 준비

1. **DB 백업 필수**: `pg_dump -Fc erp_db > backup_$(date +%Y%m%d_%H%M%S).dump`
   재암호화는 되돌릴 수 없는 작업이 아니지만(구키를 보관하면 재복구 가능),
   백업 없이 진행하지 않습니다.
2. **구키를 잃어버리지 않습니다**: 재암호화가 신키로 완전히 검증(8.4절)될
   때까지 구키(현재 운영 중인 `CREDENTIAL_ENCRYPTION_KEY` 값)를 안전한
   곳(예: 별도 시크릿 매니저, 오프라인 저장소)에 보관합니다. 구키
   폐기(decommission)는 이 회전 작업과 별도의 명시적 승인을 받은 뒤에만
   수행합니다.
3. **최종 정책: 회전 중에는 `api`와 `scheduler`를 모두 정지합니다.**
   ("API는 유지하고 Credential 등록 화면 접근만 막는" 방식은 검토 후
   기각했습니다 — 실행 중인 `api` 프로세스가 이미 구키로 만든 캐시된
   Fernet 인스턴스(`core.crypto._fernet()`, `lru_cache`)를 계속 들고
   있어 재암호화 시점과 프로세스 재시작 시점 사이에 구키/신키가 섞여
   쓰일 위험이 있고, 그 창을 안전하게 좁히는 것보다 아예 없애는 편이
   단순하고 확실하기 때문입니다.)

### 8.3 절차 (JWT/Credential/PostgreSQL password를 한 주기에 함께 회전)

**순서가 중요합니다** — DB 접속 정보(`POSTGRES_PASSWORD`/`DATABASE_URL`)
회전은 credential 재암호화보다 **먼저**, 그러나 `.env` 교체·컨테이너
재기동보다는 **먼저** 실행합니다. 이유: `api`/`scheduler`가 이미 정지된
상태에서 `scripts/rotate_postgres_password.py`는 자신의 DB 연결로 직접
`ALTER ROLE`을 실행하므로 애플리케이션 컨테이너가 떠 있을 필요가
없고(오히려 떠 있으면 안 됩니다 — 8.2절 3번 정책과 동일 이유), 이 순서를
지키면 "새 `.env`로 컨테이너를 올렸는데 아직 DB의 실제 password는 구
password"인 상태(연결 실패)가 아예 생기지 않습니다.

1. `docker compose stop api scheduler` (db/redis/web은 유지 가능 — web은
   백엔드가 죽어도 정적 파일은 서빙되나 API 호출은 실패합니다)
2. 최종 시점 DB 백업(8.2절, `pg_dump -Fc`) — 반드시 이 시점에 새로 받습니다
   (오래된 백업이 아니라 지금 막 정지시킨 시점 기준).
3. credential 구키→신키 재암호화:
   ```
   OLD_CREDENTIAL_ENCRYPTION_KEY=<구키> NEW_CREDENTIAL_ENCRYPTION_KEY=<신키> \
     python scripts/rotate_credential_key.py \
       --old-key-env OLD_CREDENTIAL_ENCRYPTION_KEY \
       --new-key-env NEW_CREDENTIAL_ENCRYPTION_KEY
   ```
   dry-run으로 "처리 대상 N건, 구키 복호화 검증 통과"를 먼저 확인한 뒤
   `--execute`. 내부적으로 전체 레코드를 하나의 트랜잭션에서 재암호화 →
   신키로 재복호화해 원래 평문과 완전히 일치하는지 재검증 → 전부 성공해야만
   commit합니다. 재검증에서 하나라도 실패하면 **자동으로 롤백**되어 DB는
   재암호화 이전 상태 그대로 남습니다(구키가 여전히 유효).

   **구키가 알려진 공개 데모 기본값(`CHANGE_ME_IN_PRODUCTION` 등)인 경우** —
   바로 이 상황(데모 기본값에서 벗어나는 것)이 회전의 목적 그 자체이므로,
   기본 검증은 이를 거부하지만 `--allow-legacy-insecure-old-key` 옵션을
   추가하면 **구키에 한해서만** 그 거부를 우회합니다:
   ```
   OLD_CREDENTIAL_ENCRYPTION_KEY=<데모 기본값이었던 구키> NEW_CREDENTIAL_ENCRYPTION_KEY=<신키> \
     python scripts/rotate_credential_key.py \
       --old-key-env OLD_CREDENTIAL_ENCRYPTION_KEY \
       --new-key-env NEW_CREDENTIAL_ENCRYPTION_KEY \
       --allow-legacy-insecure-old-key --execute
   ```
   이 옵션은 **안전하지 않은 기존 키에서 벗어나는 일회성 마이그레이션 전용**이며,
   신키 검증(데모 기본값 거부·최소 길이 등)·구키 누락 검증·구키==신키 거부·
   구키 복호화 실패 시 전체 중단은 이 옵션과 무관하게 항상 그대로 적용됩니다.
   회전이 끝나 신키가 실제 운영 값으로 자리 잡은 뒤에는 이 옵션을 다시 쓸
   이유가 없습니다(신키 자체는 데모 기본값이 될 수 없으므로).
4. PostgreSQL role(`erp_user`) password 회전:
   ```
   OLD_DATABASE_URL=<현재 DATABASE_URL> \
   NEW_DATABASE_URL=<신규 DATABASE_URL - 새 password 포함> \
   NEW_POSTGRES_PASSWORD=<신규 POSTGRES_PASSWORD> \
     python scripts/rotate_postgres_password.py \
       --current-url-env OLD_DATABASE_URL \
       --new-url-env NEW_DATABASE_URL \
       --new-password-env NEW_POSTGRES_PASSWORD
   ```
   dry-run으로 현재 URL 접속·대상 일치·신규 password 정책을 먼저 확인한 뒤
   `--execute`. **주의: 이 작업은 credential 재암호화와 달리 완전한 단일
   트랜잭션 원자성이 없습니다**(`ALTER ROLE ... WITH PASSWORD`는 그 자체로
   커밋되는 순간 즉시 유효해지고, "새 password가 실제로 통하는지"는 정의상
   별도의 새 연결로만 확인할 수 있기 때문 — PostgreSQL 자체의 근본적 제약이지
   이 도구의 결함이 아닙니다). 대신 **검증 후 보상 롤백** 방식으로 안전을
   확보합니다: commit 직후 새 연결로 재검증하고, 실패하면 아직 열려 있는
   기존 연결(구 password로 이미 인증된 세션이라 role의 password가 바뀌어도
   끊기지 않음)로 즉시 구 password로 되돌립니다. 이 도구의 종료 코드로 결과를
   구분합니다: `0`=성공, `1`=변경 시도 전 안전 중단(DB 무변경), `2`=시도했으나
   실패 후 구 password로 안전 복구 완료, `3`=**CRITICAL**(복구조차 실패 —
   즉시 수동 개입 필요, 아래 "짧은 비원자 구간" 참고).

   **짧은 비원자 구간**: `ALTER ROLE` commit과 새 연결 검증 사이에는
   "role의 실제 password는 이미 새 값인데 아직 아무도 그걸로 접속을 확인하지
   못한" 찰나의 구간이 존재합니다. 이 구간에 프로세스가 죽으면(예: 서버
   전원 차단) — 이번 절차상 4단계 시작 **전에** 이미 5단계에서 쓸 새
   Secret 파일(`erp_production_next_<timestamp>.env` 등, `POSTGRES_PASSWORD`/
   `DATABASE_URL` 포함)이 저장소 밖에 준비돼 있어야 하므로, 그 파일을 그대로
   `.env`에 반영하면 복구됩니다(role의 실제 password도 이미 그 값이므로
   일치). 이 도구 자체가 그 Secret 파일을 만들거나 수정하지 않습니다 —
   준비는 별도 단계(운영 Secret 전환 준비 단계)에서 미리 끝나 있어야 합니다.
5. **재암호화/password 회전과 `.env` 교체 사이에는 어떤 credential 쓰기도,
   추가 DB 변경도 없어야 합니다** — 3·4단계 완료 후 즉시 5단계로 진행합니다.
6. root `.env`(또는 배포 환경변수)를 새 Secret 파일 내용으로 통째로
   교체합니다(`JWT_SECRET_KEY`/`CREDENTIAL_ENCRYPTION_KEY`/
   `POSTGRES_PASSWORD`/`DATABASE_URL` 전부 — 4단계에서 실제로 적용한 값과
   반드시 동일해야 합니다).
7. 새 이미지로 `api`를 기동합니다(`docker compose up -d api`). `api`는
   시작 시 lifespan에서 `validate_startup_secrets()`로 신키가 안전 기준을
   만족하는지 확인한 뒤에만 요청을 받기 시작합니다.
8. DB·credential 검증: 로그인 → 설정 화면에서 기존 API Credential 마스킹
   표시가 정상 조회되는지(신 `CREDENTIAL_ENCRYPTION_KEY`로 기존 암호문이
   실제로 복호화되는지) → DB 연결 자체가 신 `POSTGRES_PASSWORD`로 정상
   동작하는지 확인.
9. `scheduler`를 기동합니다(`docker compose up -d scheduler`) — 동일하게
   `validate_startup_secrets()` 통과 후에만 잡을 등록합니다.
10. 자연 실행 잡 관찰: 다음 주기(주문수집/상품동기화 등)가 스케줄대로
    자동 실행되고 실패 없이 완료되는지 시스템 모니터링 → 작업이력에서
    확인합니다(수동으로 동기화 엔드포인트를 호출하지 않습니다).
11. 문제가 있으면 롤백: 구 이미지(롤백 태그)/구 `.env`/구 PostgreSQL
    password로 되돌리고 재기동합니다. DB는 3·4단계에서 실패 시 이미
    자동/보상 롤백되어 구 키·구 password 상태이므로, 구 값으로 되돌리기만
    하면 됩니다. **DB 다운그레이드는 하지 않습니다** — 재암호화/password
    회전 모두 스키마 변경이 아니라 값 재작성이므로 downgrade 대상이
    아닙니다.

### 8.4 JWT 회전이 사용자에게 미치는 영향

- `JWT_SECRET_KEY`를 바꾸면 그 즉시 기존에 발급된 모든 액세스 토큰이
  거부됩니다(`core/security.py`가 매 검증 시 현재 `settings.jwt_secret_key`로
  서명을 확인하는 stateless 방식이라 별도 토큰 무효화 로직이 없습니다) —
  **모든 사용자가 다시 로그인해야 합니다.** 이는 데이터 손실이 아니라 예상된
  동작입니다.
- 비밀번호 해시(`users.password_hash`, bcrypt)는 JWT 서명 키와 완전히
  독립적입니다 — JWT 키 회전이 저장된 비밀번호 해시를 바꾸지 않습니다
  (`tests/unit/test_security.py`로 회귀 검증).

### 8.5 시크릿 값 보관 원칙

- 실제 시크릿 값은 이 저장소(Git)에 절대 두지 않습니다 — `.env`는
  `.gitignore`에 포함되어 있고, `.env.example`에는 플레이스홀더와 생성
  명령만 있습니다.
- 배포 서버에서는 `.env` 파일 권한을 소유자만 읽기 가능하도록 제한하는 것을
  권장합니다(`chmod 600 .env`). 별도 시크릿 매니저(예: Docker secrets,
  Vault, 클라우드 Secret Manager)를 쓸 수 있으면 그쪽을 우선 고려하세요 —
  이번 작업 범위에서는 `.env` 파일 기반 주입까지만 다룹니다.
- 구키 폐기(완전 삭제)는 신키로의 전환이 충분히 검증된 뒤, 별도의 명시적
  승인을 받은 다음 단계에서 진행합니다.
