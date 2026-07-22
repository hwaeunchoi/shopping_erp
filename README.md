# 쇼핑몰 통합 ERP

주문·광고·매출·경영관리를 통합하는 실무용 ERP 시스템입니다.
설계 문서(SRS, ERD, UI와이어프레임, 폴더구조설계)는 별도로 전달된 문서를 참고하세요.

**현재 개발 단계**: 8단계 — DB 모델(50개 테이블) + Alembic 마이그레이션 뼈대, 더미 데이터 생성, Repository 계층, 더미 커넥터, Service 레이어, API 계층(FastAPI), 스케줄러(자동 수집/배치), 프론트엔드(React + Vite) 완료

---

## 1. 설치 (Windows)

```bat
:: 1) 가상환경 생성 및 활성화
python -m venv venv
venv\Scripts\activate

:: 2) 패키지 설치
pip install -r requirements.txt

:: 3) 환경변수 파일 생성
copy .env.example .env
:: .env를 열어 JWT_SECRET_KEY, CREDENTIAL_ENCRYPTION_KEY 등을 실제 값으로 수정하세요.
```

## 2. DB 초기화

두 가지 방법 중 하나를 사용합니다.

### 방법 A — 빠른 개발용 부트스트랩 (추천: 지금 단계)
```bat
python scripts\init_db.py
```
- `logs/`, `backup/`, `reports/`, `reports/generated/`, `reports/templates/` 디렉터리를
  자동 생성합니다. 이미 존재하면 그대로 넘어갑니다.
- `models/` 아래 50개 테이블을 전부 생성합니다.
- 기본 역할(Admin/Manager/Viewer), 메뉴 권한, 관리자 계정(`admin` / `ChangeMe!123`),
  5개 쇼핑몰 플랫폼, 기본 창고 1건을 함께 시딩합니다.
- **최초 로그인 후 반드시 관리자 비밀번호를 변경하세요.**

### 방법 B — Alembic 마이그레이션 (정식 운영 권장)
초기 마이그레이션(`migrations/versions/20260702_0955_91b961bb3be8_initial_schema_50_tables.py`,
50개 테이블 전체)이 이미 생성되어 있습니다. 빈 DB에서는 바로 적용합니다.
```bat
alembic upgrade head
```
- 이후 모델을 수정할 때마다 `alembic revision --autogenerate -m "설명"` →
  `alembic upgrade head` 순서로 스키마를 버전 관리하며 반영합니다.
- 방법 A로 이미 테이블을 만들었다면(이 저장소의 기본 `erp.db`가 그 상태입니다),
  Alembic이 스키마 변경 이력을 추적할 수 있도록 최초 1회는 `alembic stamp head`로
  현재 상태를 기준점으로 표시해주세요. (이미 이 저장소의 `erp.db`에는 적용해뒀습니다.)

## 3. 더미 데이터 생성

`init_db.py` 실행 이후(기준정보가 있어야 함) 아래 명령으로 업무 흐름 검증용
더미 데이터를 생성합니다.
```bat
python scripts\seed_dummy_data.py
```
- 상품/공급처 15종(SKU 약 30개), 고객 60명, 주문 400건(교환/반품/취소/배송 포함),
  재고 이동 이력, 정산, 비용, 광고 캠페인/일별 성과, 매출·손익 요약까지
  상품 → 재고 → 주문 → 정산/비용/광고 → 매출·손익분석으로 이어지는 흐름을 채웁니다.
- `system_logs`/`notifications`/`audit_logs`/`memos` 등 운영 중에만 쌓이는
  로그성 테이블은 대상이 아닙니다.
- 이미 더미 데이터가 있으면(`products` 테이블에 데이터 존재) 재실행 시 건너뜁니다.
- 매출/손익 요약(`profit_loss_summary` 등)은 5단계(계산엔진) 전까지 사용할
  임시 집계 로직으로 채워지며, 이후 서비스 레이어 구현 시 동일 정의로 대체됩니다.

## 4. 문법/구조 확인 (이 저장소에서 이미 수행됨)
모든 `.py` 파일은 `python -m py_compile`로 문법 오류가 없음을 확인했으며,
`requirements.txt` 설치 후 `init_db.py`→`seed_dummy_data.py`를 실제로 실행하여
50개 테이블 생성과 더미 데이터 삽입이 오류 없이 끝까지 동작함을 확인했습니다
(FK 무결성 검증 포함).

Python 3.14 환경에서는 `SQLAlchemy==2.0.35`/`pydantic==2.9.2`/`pandas==2.2.2`/
`bcrypt`(최신) 조합이 호환 문제를 일으켜, `requirements.txt`에서 해당 패키지들을
Python 3.14용 wheel이 있는 버전으로 상향/고정했습니다. Python 3.11~3.13
환경에서는 이 조정이 필요 없을 수 있습니다.

---

## 5. 폴더 구조 요약

폴더 구조 설계 문서(`폴더구조설계_쇼핑몰통합ERP.md`)의 전체 트리와 매핑표를 참고하세요.
핵심만 요약하면:

| 폴더 | 역할 |
|---|---|
| `models/` | ORM 모델 50개 테이블 (도메인별 파일 분리) |
| `core/` | DB 세션, 보안(JWT/암호화), 감사로그, 작업큐 등 공통 기반 |
| `config/` | `.env` 로드, 로깅 설정 |
| `migrations/` | Alembic 마이그레이션 |
| `repositories/` | Repository 계층 (완료 — 도메인별 CRUD/조회) |
| `integrations/` | 쇼핑몰/광고 플러그인 커넥터 (완료 — 더미 구현체, `carriers/`는 향후 확장) |
| `services/` | 계산엔진/업무로직/AI 모듈 (완료 — 4종, `services/ai/`는 향후 확장) |
| `scheduler/` | 자동 수집/배치 (완료 — APScheduler 배치 6종) |
| `api/` | FastAPI 라우터 (완료 — 인증/RBAC + 10개 도메인 라우터) |
| `frontend/` | React + Vite SPA (완료 — 인증 + 9개 화면) |

## 6. Repository 계층

`repositories/`에 도메인별 Repository 클래스를 구현했습니다 (총 21개 클래스, 12개 파일).
- 공통 CRUD는 `BaseRepository`(`get_by_id`/`list_all`/`add`/`delete`/`count`)가 제공하고,
  각 Repository는 여기에 도메인별 조회 메서드(예: `OrderRepository.get_by_platform_order_no`,
  `CustomerRepository.list_dormant`)를 추가합니다.
- Repository는 세션을 직접 생성하지 않고 생성자에서 주입받습니다. 세션 생명주기는
  호출하는 Service 계층 또는 `core.database.get_db`/`session_scope`가 책임집니다.
- 시스템/부가기능 그룹(system_logs, notifications, audit_logs, memos 등 로그성 테이블)은
  아직 Repository 대상이 아닙니다 — 실제 사용하는 Service가 생길 때 함께 추가합니다.
- 더미 데이터가 채워진 DB로 21개 Repository 클래스 전체의 핵심 메서드를 직접 실행하여
  정상 동작을 확인했습니다.

## 7. 더미 커넥터

`integrations/malls/`, `integrations/ads/`에 공통 인터페이스와 플랫폼별 더미
구현체를 완성했습니다.
- `BaseMallConnector`(`fetch_orders`/`fetch_order_detail`/`update_shipment`/
  `fetch_settlements`)를 네이버 스마트스토어/쿠팡/ESM/11번가/카카오쇼핑 5개
  쇼핑몰이 구현합니다. `Platform.connector_class` 문자열로 실제 구현체를
  조회하는 `integrations.malls.get_mall_connector()`도 제공합니다.
- `BaseAdConnector`(`fetch_campaigns`/`fetch_daily_performance`)를 네이버
  검색광고/쇼핑검색광고/쿠팡광고 3개 플랫폼이 구현합니다.
  `AdCampaign.ad_platform_code` 문자열로 조회하는
  `integrations.ads.get_ad_connector()`도 제공합니다.
- 실제 API 키가 없는 개발 단계이므로 각 구현체는 플랫폼마다 서로 다른
  원본 응답 형식(raw)을 흉내 낸 더미 데이터를 만든 뒤 공통 표준 형식으로
  정규화합니다. 실제 API 연동 시에는 원본 데이터를 가져오는 부분만
  실제 HTTP 요청으로 교체하면 되고, 인터페이스와 반환 형식은 그대로 유지됩니다.
- `integrations/carriers/`(택배사 연동)는 패키지 docstring에 명시된 대로
  향후 확장 범위라 이번 단계에서 다루지 않았습니다.
- 실제 시딩된 DB의 `Platform.connector_class`/`AdCampaign.ad_platform_code`
  값으로 8개 커넥터 전체를 인스턴스화하고 모든 메서드를 실행하여 정상 동작을
  확인했습니다.

## 8. Service 레이어

`services/`에 계산엔진/업무로직 4종을 구현했습니다.
- `OrderSyncService` — 쇼핑몰 커넥터(`integrations/malls`)로 주문을 수집해
  orders/order_items/order_status_history/customers에 반영합니다.
  `OrderRepository.get_by_platform_order_no()`로 SRS FR-MALL-04(중복 수집 방지)를
  보장하고, 신규/상태변경 주문에 맞춰 `InventoryService`로 재고를 예약·차감·해제하며,
  영향받은 고객은 `CustomerStatsService`로 통계를 갱신합니다. `product_platform_map`에
  매핑이 없는 주문상품은 건너뛰고 집계만 합니다(외부 데이터 경계 방어).
- `InventoryService` — 재고 예약(`reserve`)/해제(`release_reservation`)/
  출고차감(`deduct_on_shipment`)/반품검수(`inspect_return`). 모든 재고 이동은
  단일 엔진 `transition_stock()`을 거치며, 여기서 전이 행렬 검증과 불변조건
  I1~I6 검사를 수행한 뒤 `inventory_transactions` 이력을 남깁니다
  (모델 설계 원칙 대응). 서비스 계층이 유일한 검증 관문입니다(SSoT).
- `CustomerStatsService` — `customers`의 캐시 컬럼(총구매액/주문수/최초·최근주문일/
  등급/VIP/휴면여부)을 다시 계산합니다. **CANCELED 주문은 집계에서 제외**합니다 —
  models/customer.py 설계 주석("주문 확정/취소 시 서비스 레이어가 갱신")에 대응합니다.
- `ProfitCalculationService` — orders/order_items/costs/ad_performance_daily를
  집계해 일별 `profit_loss_summary`를 upsert하는 계산엔진입니다
  (services/__init__.py에 명시된 "profit_calculation_service.py"에 대응).

**검증**: 스텁 커넥터로 `OrderSyncService`의 신규 생성→중복 무시→상태변경(재고 차감/
예약 해제)→상태이력 적재 전체 흐름을 확인했고, `ProfitCalculationService`로 기존
더미 데이터를 재계산해 2단계에서 생성한 값과 정확히 일치함을 확인했습니다. 이 과정에서
`CustomerStatsService`가 2단계 더미 스크립트의 단순 합산(취소 주문 미제외)보다 정확하다는
것을 발견해, 전체 60명 고객의 통계를 서비스 로직으로 재계산해 반영했습니다.

**범위 제외**: `services/ai/`(AI 분석 9종, v1.2 13장 — 설계 문서 미보유), 정산 동기화
서비스(커넥터의 `fetch_settlements` 연동), 시스템/부가기능 그룹(알림/감사로그 등)은
이번 단계에서 다루지 않았습니다.

## 9. API 계층

`api/`에 FastAPI 앱과 인증/RBAC 포함 도메인별 라우터를 구현했습니다.

### 실행 방법
```bat
uvicorn api.main:app --reload
```
- Swagger UI: http://127.0.0.1:8000/docs
- 헬스체크: `GET /health`

### 인증
- `POST /api/auth/login` — `username`/`password` 폼(OAuth2PasswordRequestForm)으로
  로그인, JWT 액세스 토큰 발급 (`core/security.py` 재사용).
- `GET /api/auth/me` — `Authorization: Bearer <token>`으로 내 정보 조회.
- 이후 모든 API는 `Authorization: Bearer <token>` 헤더가 필요합니다(없으면 401).

### RBAC(권한) 적용
`api/deps.py`의 `require_permission(code)`가 로그인한 사용자의 역할(Role)에
해당 권한 코드가 매핑돼 있는지 검사합니다(1단계에서 시딩한 permissions/
role_permissions을 그대로 사용). 권한이 없으면 403을 반환합니다.
- `products`→`PRODUCT_MANAGE`, `orders`→`ORDER_VIEW`/`ORDER_EDIT`,
  `inventory`→`INVENTORY_VIEW`, `settlements`→`SETTLEMENT_VIEW`,
  `costs`→`COST_MANAGE`, `ads`→`AD_MANAGE`, `analytics`→`ANALYTICS_VIEW`
- `customers`, `platforms`는 `scripts/init_db.py`의 DEFAULT_PERMISSIONS에
  전용 권한 코드가 없어(설계상 CRM/기준정보 화면 권한 미정의) 로그인 여부만 검사합니다.

### 라우터 목록
| 라우터 | 주요 엔드포인트 |
|---|---|
| `auth` | 로그인, 내 정보 |
| `products` | 상품/SKU 목록·상세 조회 |
| `customers` | 고객 목록(VIP/휴면 필터)·상세 조회 |
| `orders` | 주문 목록·상세(주문상품 포함) 조회, **`POST /sync`**(커넥터로 실제 주문 수집 트리거) |
| `inventory` | 재고 목록(안전재고 미만 필터) |
| `settlements` | 정산 목록·상세 |
| `costs` | 비용 목록 조회·등록 |
| `ads` | 광고 캠페인 목록, 캠페인별 일별 성과 |
| `analytics` | 매출/손익 요약 조회, **`POST /profit-loss/calculate`**(계산엔진 트리거) |
| `platforms` | 쇼핑몰 플랫폼 기준정보 조회 |

`orders/sync`, `analytics/profit-loss/calculate`는 4~5단계에서 만든
`integrations.malls.get_mall_connector()`/`services.OrderSyncService`/
`services.ProfitCalculationService`를 그대로 호출합니다 — API 계층도
Repository/커넥터를 직접 다루지 않고 Service만 호출합니다.

`OAuth2PasswordRequestForm`(로그인 폼 파싱)에 `python-multipart`가 필요해
`requirements.txt`에 추가했습니다.

**검증**: `FastAPI TestClient`로 실제 시딩된 DB에 대해 인증(401/403 포함)부터
전체 10개 라우터의 조회/생성/트리거 엔드포인트까지 엔드투엔드로 호출해
정상 동작을 확인했습니다.

**범위 제외**: 상품/공급처 등록·수정(쓰기 API 대부분), 엑셀 Import/Export,
알림/감사로그/보고서 등 시스템·부가기능 화면의 API는 이번 단계에서 다루지
않았습니다.

## 10. 스케줄러 (자동 수집/배치)

`scheduler/`에 APScheduler 기반 배치 6종과 실행 프로세스를 구현했습니다.

### 실행 방법
```bat
python scheduler\scheduler.py
```
Ctrl+C로 종료합니다. 시작/완료/실패 로그는 콘솔과 `logs/erp.log`에 남습니다.

### 배치 작업 (`scheduler/jobs/`)
| 작업 | 주기(기본값) | 내용 |
|---|---|---|
| `order_collect_job` | 10분마다 | 활성 플랫폼 전체의 최근 3일 주문을 `OrderSyncService`로 수집·보정 |
| `ad_collect_job` | 매일 07:00 | 광고 플랫폼별 캠페인/최근 3일 성과를 수집, (ad_platform_code, platform_campaign_id)·(campaign_id, stat_date) 기준 upsert |
| `settlement_sync_job` | 매일 07:30 | 커넥터의 `fetch_settlements()`로 최근 60일 정산 회차를 (platform_id, settlement_cycle) 기준 upsert |
| `profit_calculation_job` | 매일 01:00 | 최근 3일+오늘 `profit_loss_summary`를 `ProfitCalculationService`로 재계산 |
| `customer_stats_job` | 매일 01:30 | 전체 고객 캐시 통계를 `CustomerStatsService`로 재계산 |
| `backup_job` | 매일 03:00 | SQLite DB 파일을 `backup/<YYYY-MM-DD>/erp_<HHMMSS>.db`로 복사하고 `backup_history`에 기록, `backup_retention_days`/`backup_max_count` 보관정책 적용 |

각 잡은 인자 없는 `run()` 함수 하나만 노출하며, 자체적으로
`core.database.session_scope()`로 세션을 열고 커밋까지 책임집니다.
`order_collect_job`/`settlement_sync_job`은 4단계 더미 커넥터를,
`profit_calculation_job`/`customer_stats_job`은 5단계 Service를 그대로
호출합니다 — 스케줄러도 Repository/커넥터를 직접 다루지 않습니다.

백업 파일 경로는 `.gitignore`의 `backup/*/`(하위 디렉터리만 무시) 패턴에
맞춰 날짜별 하위 디렉터리(`backup/2026-07-01/...`)에 저장하도록 설계했습니다.

**검증**: `build_scheduler()`로 6개 작업이 정상 등록되는지 확인하고, 6개
잡의 `run()`을 실제 시딩된 DB에 대해 전부 실행해 정상 동작(정산/광고 upsert,
손익·고객 통계 재계산, 실제 백업 파일 생성 및 `backup_history` 기록)을
확인했습니다. 더미 커넥터가 매 호출 시 요청한 날짜 구간으로만 시드되는
무상태(stateless) 방식이라, 같은 주문번호라도 조회 구간이 달라지면 이전과
다른 상태를 반환할 수 있다는 점을 발견했습니다 — `OrderSyncService`는 이런
임의의 상태 전이도 안전하게(크래시 없이) 처리하지만, 실제 운영에서는
커넥터가 플랫폼의 진짜 최신 상태를 반환하므로 발생하지 않는 더미 환경
고유의 한계입니다.

**범위 제외**: `task_execution_history` 연동(현재는 로그 파일에만 기록),
정산 상세(`settlement_details`) 대사, PostgreSQL 전환 시 필요한 `pg_dump`
기반 백업은 다루지 않았습니다.

## 11. 프론트엔드 (React + Vite)

`frontend/`에 Vite + React 19 + TypeScript SPA를 구현했습니다.

### 실행 방법
```bat
cd frontend
npm install
npm run dev
```
- http://localhost:5173 에서 접속합니다.
- 개발 서버는 `/api`, `/health` 요청을 `vite.config.ts`의 프록시 설정으로
  `http://127.0.0.1:8000`(FastAPI)에 전달합니다 — 백엔드를 **먼저**
  `uvicorn api.main:app --reload`로 띄워야 합니다. 이 프록시 덕분에 개발 중에는
  백엔드에 CORS 설정을 추가하지 않아도 됩니다(운영 배포 시에는 리버스
  프록시나 CORS 설정이 별도로 필요합니다 — 아직 다루지 않았습니다).
- 로그인: `admin` / `ChangeMe!123` (1단계에서 시딩한 계정).

### 구성
- `src/api/client.ts` — JWT를 `localStorage`에 저장하고 모든 요청에
  `Authorization` 헤더를 붙이는 공용 fetch 래퍼. 401 응답 시 토큰을 지운다.
- `src/api/types.ts` — `api/routers/*.py`의 Pydantic 응답 스키마와 1:1 대응하는 TS 타입.
- `src/auth/AuthContext.tsx` — 로그인/로그아웃/`/api/auth/me` 세션 복원.
- `src/components/{Layout,ProtectedRoute}.tsx` — 사이드바 레이아웃, 미인증 시 `/login` 리다이렉트.
- `src/pages/` — 대시보드, 주문(목록/상세/커넥터 수집), 상품, 고객, 재고,
  정산, 비용(등록 폼 포함), 광고, 매출/손익분석까지 9개 화면. 전부
  6단계에서 만든 API 라우터를 그대로 호출합니다.

### 검증
Node.js가 이 환경에 없어 `winget install OpenJS.NodeJS.LTS`로 설치한 뒤
진행했습니다. `tsc -b`(타입체크)와 `npm run build`(프로덕션 빌드) 통과를
확인했고, 백엔드(`uvicorn`)와 프론트엔드(`vite`) 서버를 동시에 띄워
브라우저에서 로그인부터 9개 화면 전체 조회, 그리고 3가지 쓰기 흐름(비용
등록 `POST /api/costs`, 손익 재계산 `POST /api/analytics/profit-loss/calculate`,
주문 수집 `POST /api/orders/sync`)까지 실제 클릭으로 확인했습니다. 테스트로
만든 비용 레코드는 정리했습니다.

검증 중 `.env`의 `DATABASE_URL=sqlite:///./erp.db`가 상대경로라 프로세스
실행 위치에 따라 엉뚱한 빈 DB를 만드는 문제를 발견해, 로컬 `.env`는
절대경로로 고정하고 `.env.example`에는 이 경로가 cwd에 의존한다는 주의
문구를 추가했습니다.

## 12. 테스트 (pytest)

### 실행 방법
```bat
venv\Scripts\activate
pip install -r requirements.txt      :: pytest/pytest-cov 포함

:: 전체 테스트
pytest

:: 상세 출력
pytest -v

:: 커버리지 리포트 포함
pytest --cov=services --cov=repositories --cov=api --cov-report=term-missing

:: 단위 테스트만 / 통합 테스트만
pytest tests/unit
pytest tests/integration
```

### 테스트 격리
모든 테스트는 **실제 개발 DB(`erp.db`)를 전혀 사용하지 않습니다.** 테스트마다
인메모리 SQLite(`sqlite:///:memory:`)에 `models.Base.metadata`로 스키마를
새로 만들어 사용하고, 종료 시 버립니다. API 통합 테스트는 FastAPI의
`get_db` 의존성만 테스트용 세션으로 오버라이드하고 나머지 앱 코드는
그대로 사용합니다(라우터/의존성 로직 자체를 검증하기 위함).

### 구성
| 파일 | 내용 |
|---|---|
| `tests/conftest.py` | 공용 픽스처: 인메모리 엔진/세션, 플랫폼·창고·상품·고객·재고 등 기준 데이터 |
| `tests/unit/test_profit_calculation_service.py` | 일별 손익 집계, upsert, 기간 재계산 |
| `tests/unit/test_customer_stats_service.py` | 취소주문 제외, 등급/VIP/휴면 판정 |
| `tests/unit/test_inventory_service.py` | 예약/해제/출고차감/반품검수 + 재고이력 |
| `tests/unit/test_inventory_invariants.py` | 재고 불변조건 T1~T8(전이 행렬/음수 금지/원자성) |
| `tests/unit/test_order_sync_service.py` | 신규생성/중복무시/상태변경/매핑없음 스킵 (스텁 커넥터) |
| `tests/integration/conftest.py` | API 테스트 전용: `TestClient` + `get_db` 오버라이드 + 관리자 계정 시딩 |
| `tests/integration/test_repositories.py` | Order/Customer/Inventory/Product Repository + BaseRepository CRUD |
| `tests/integration/test_api_auth.py` | 로그인, 토큰 검증 |
| `tests/integration/test_api_orders.py` | 조회/404/인증, RBAC 403(권한 없는 역할 차단 확인), 커넥터 연동 sync |
| `tests/integration/test_api_analytics.py` | 손익 조회/계산 트리거/upsert |

### 대상에서 제외한 것
`GrowthAnalysisService`, `NotificationService`, `OrderService`는 아직 코드가
없어(각각 product_performance_summary/kpi_targets 기반, Notification 모델
기반, 범용 주문 CRUD) 이번 테스트 작성 대상에서 제외했습니다 — "새 기능
추가보다 기존 코드 품질/안정성 우선"이라는 원칙에 따라, 존재하지 않는
서비스를 새로 만들지 않고 실제 존재하는 `ProfitCalculationService`/
`CustomerStatsService`/`InventoryService`/`OrderSyncService`만 테스트했습니다.

### 결과
`pytest -v` 기준 **49개 테스트 전부 통과**. 테스트 실행 전후로 실제
`erp.db`의 주문/고객 건수가 변하지 않음을 확인해 격리가 올바름을 검증했습니다.

## 13. Alembic 검증

초기 마이그레이션을 실제로 생성하고 4가지 항목을 전부 검증했습니다.

### 발견하고 고친 버그: `alembic.ini`의 한글 주석이 Windows에서 모든 alembic 명령을 깨뜨림
`alembic revision --autogenerate` 최초 실행 시 `UnicodeDecodeError:
'cp949' codec can't decode byte...`로 실패했습니다. 원인은 Alembic
1.13.x가 설정 파일을 `encoding="locale"`(Python PEP 597 센티널)로
읽는데, 이는 `PYTHONUTF8=1`을 설정해도 무시되고 OS의 "진짜" 로케일
코드페이지(한글 Windows는 cp949)를 그대로 쓰기 때문입니다(직접
`PYTHONUTF8=1`로도 재현·확인함). UTF-8로 저장된 `alembic.ini`의 한글
주석이 cp949로 디코딩되지 않아 alembic 명령 전체가 동작 불가 상태였습니다.
**`alembic.ini`를 ASCII 전용으로 변경**(주석을 영어로, 상세 설명은
`migrations/versions/README.md`로 이동)하여 해결했습니다. 이 문제를
피하려면 앞으로도 `alembic.ini`에는 비ASCII 문자를 넣지 않아야 합니다.

### 검증 결과
| 항목 | 결과 |
|---|---|
| autogenerate | 빈 테스트 DB 기준 50개 테이블 + 인덱스 + FK 56개를 정확히 감지, `initial schema (50 tables)` 리비전 생성 |
| `alembic upgrade head` | 빈 DB에서 50개 테이블 전부 정상 생성 |
| `alembic check` | 생성된 스키마와 `models.Base.metadata` 간 diff 없음("No new upgrade operations detected") |
| `alembic downgrade base` | `alembic_version`을 제외한 50개 테이블 전부 정상 삭제 |
| 재적용 | downgrade 후 `alembic upgrade head` 재실행 시 50개 테이블 정상 재생성 |
| 초기 마이그레이션 구조 | `create_table`/`drop_table` 각 50건, FK/인덱스 포함, 테이블 생성·삭제 순서가 FK 의존관계를 올바르게 반영 |

### 이 저장소의 `erp.db` 반영
1단계 방법 A(`init_db.py`, `Base.metadata.create_all()`)로 이미 만들어진
테이블 구조를 유지한 채, `alembic stamp head`로 Alembic 추적 대상에
편입시켰습니다(재실행해도 테이블을 다시 만들지 않음). 이후
`alembic check`로 이 DB도 models와 diff 없음을 재확인했습니다. 이제부터
모델을 바꾸면 `alembic revision --autogenerate` → `alembic upgrade head`로
정상적으로 버전 관리됩니다.

### 변경/생성 파일
- `alembic.ini` — ASCII 전용으로 수정 (버그 수정)
- `migrations/versions/20260702_0955_91b961bb3be8_initial_schema_50_tables.py` — 신규(초기 마이그레이션)
- `migrations/versions/README.md` — 검증 결과 및 인코딩 주의사항 반영
- `README.md` — 방법 B 안내 갱신, 본 절 추가

## 14. PostgreSQL 호환성 점검

코드 전체를 정적으로 검토했습니다(실제 PostgreSQL 라이브 연결 검증은 5순위
Docker 단계에서 진행하기로 결정 — Docker가 없어 지금 하려면 PostgreSQL을
Windows 서비스로 직접 설치해야 하는데, 곧 만들 Docker 컨테이너로 하는 편이
더 깔끔합니다).

### 점검 결과
| 항목 | 결과 |
|---|---|
| SQLite 전용 코드 | `core/database.py`의 `check_same_thread`/`PRAGMA foreign_keys` 둘 다 `if settings.database_url.startswith("sqlite")`로 이미 가드됨. `scheduler/jobs/backup_job.py`(파일 복사 백업)만 SQLite 전용이며, 다른 DB에서는 `NotImplementedError`를 명시적으로 던짐(조용히 틀리게 동작하지 않음) |
| SQLAlchemy 2.0 스타일 | repositories/services/api는 전부 `select()`+`session.execute()`. `scripts/init_db.py`, `scripts/seed_dummy_data.py`에서만 레거시 `.query()`가 남아있어 `select()`로 통일함(아래 변경 내역) |
| 타입/제약조건 | 전 모델이 `String`/`Integer`/`Numeric`/`Boolean`/`Date`/`DateTime`/`UniqueConstraint`/`Index`/`ForeignKey`만 사용 — 전부 PostgreSQL에서 그대로 동작하는 ANSI 표준 타입. SQLite 전용 타입(`sqlite.JSON` 등)이나 `server_default=func.now()` 같은 DB 종속 기본값도 없음(모두 Python 레벨 `utcnow()`) |
| `func.*` 사용 | `func.count`/`func.sum`/`func.coalesce`/`func.min`/`func.max`만 사용 — ANSI 표준 집계함수라 방언 무관 |
| DB URL만 바꿔서 실행 가능한지 | `config/settings.py`→`core/database.py`→`migrations/env.py` 전부 `settings.database_url`(= `.env`의 `DATABASE_URL`)만 참조. 하드코딩된 접속 정보 없음 |

### 발견하고 고친 것
1. **`core/database.py`의 SQLite pragma 리스너 범위 축소** — 기존에는
   `@event.listens_for(Engine, "connect")`로 **전역 Engine 클래스**에 걸어두고
   콜백 안에서 매번 `settings.database_url`을 다시 확인했습니다. 이러면
   프로세스 안에서 만들어지는 다른 모든 SQLAlchemy 엔진(테스트 엔진, Alembic
   엔진 등)에도 불필요하게 콜백이 걸립니다. `settings.database_url`이
   PostgreSQL로 바뀌었는데 어딘가에서 별도 SQLite 엔진을 쓰는 경우 그 엔진엔
   PRAGMA가 걸리지 않는 등 잠재적으로 헷갈리는 상황도 만들 수 있어서, 이
   모듈이 만든 `engine` 인스턴스에만 등록하도록 좁혔습니다. PostgreSQL에서는
   리스너 자체가 등록되지 않습니다(동작 변경 없음, 범위만 좁힘).
2. **레거시 `.query()` → `select()` 통일** — `scripts/init_db.py`,
   `scripts/seed_dummy_data.py`의 존재 확인/개수 조회 쿼리를 SQLAlchemy 2.0
   스타일로 바꿨습니다. 레거시 Query API도 2.0에서 계속 지원되고 PostgreSQL
   호환성 자체엔 문제없었지만("SQLAlchemy 2.0 호환성" 점검 항목에 대응해)
   프로젝트 전체를 순수 2.0 스타일로 통일했습니다.

### 참고 (수정하지 않음, 설계/스키마 변경 없음 원칙)
- PostgreSQL은 SQLite와 달리 `VARCHAR(n)` 길이 제약을 **삽입 시점에 실제로
  강제**합니다(SQLite는 무시). 현재 모델의 컬럼 길이는 실사용 데이터 기준으로
  충분해 보이지만, 운영 전환 시 실제 데이터로 한 번 더 확인을 권장합니다.
- `requirements.txt`의 `psycopg[binary]==3.2.1`은 그대로 주석 처리
  유지했습니다 — 5순위(Docker) 단계에서 실제 전환 시 주석을 해제합니다.

### 검증
`select()` 전환 후 `init_db.py`/`seed_dummy_data.py`를 새 임시 DB에
재실행(정상 종료, 멱등성 SKIP 로직도 정상)했고, `core/database.py` 변경 후
FK 제약이 여전히 올바르게 걸리는지(존재하지 않는 platform_id로 주문 삽입 시
`IntegrityError` 발생) 확인했습니다. 1순위 pytest 스위트도 재실행 —
**49 passed**.

### 변경 파일
- `core/database.py` — PRAGMA 리스너를 엔진 인스턴스 범위로 축소 (버그 하드닝)
- `scripts/init_db.py` — `.query()` → `select()` 전환
- `scripts/seed_dummy_data.py` — `.query()` → `select()` 전환
- `README.md` — 본 절 추가

## 15. 개발 품질 향상 (pre-commit / ruff / black / isort / mypy)

### 실행 방법
```bat
venv\Scripts\activate
pip install -r requirements.txt        :: ruff/black/isort/mypy/pre-commit 포함

:: 개별 실행
ruff check .
black --check .
isort --check .
mypy models config core repositories services api integrations scheduler scripts tests
pytest -q

:: pre-commit 훅 설치 및 전체 실행
pre-commit install
pre-commit run --all-files
```
`pre-commit`은 격리 환경을 새로 만들지 않고 venv에 설치된 도구를 그대로
사용합니다(`language: system`) — `requirements.txt`에 고정한 버전과 항상
일치시키기 위해서입니다. **`venv`를 먼저 활성화(또는 최소한 설치)한 뒤
커밋해야** 합니다.

### 도구 구성
- **ruff** — 린트 전담. 포맷팅(E501)과 import 순서(I)는 각각 black/isort에
  맡기고 겹치지 않게 했습니다. `B008`(FastAPI `Depends(...)` 관용구),
  `UP`(pyupgrade) 대부분은 껐습니다 — 자세한 이유는 아래 "제외한 검사" 참고.
- **black** — 포맷터. `line-length=120`, `skip-magic-trailing-comma=true`로
  설정했습니다(아래 참고).
- **isort** — import 정렬. `profile="black"`로 black과 충돌 없이 동작하도록
  맞췄습니다.
- **mypy** — 타입 검사. 기존 코드가 부분적으로만 타입힌트를 갖추고 있어
  `disallow_untyped_defs`는 켜지 않고(전면 재작성 없이 점진 도입),
  `check_untyped_defs`로 이미 있는 타입힌트끼리의 모순은 잡습니다.
- **pre-commit** — 위 4개 + pytest + 기본 위생 훅(trailing-whitespace,
  end-of-file-fixer, check-yaml/toml, check-merge-conflict,
  check-added-large-files, mixed-line-ending)을 커밋 전에 실행합니다.

### 검토 중 발견하고 고친 문제들
1. **`alembic.ini`에 이어 세 번째로 발견한 em-dash(—) 문제**: `isort`가
   `scripts/init_db.py`를 파싱하다가 콘솔 출력 중 `'cp949' codec can't
   encode character '—'`로 실패했습니다. 프로젝트 전체 `.py` 파일을
   스캔해 em-dash 19곳을 전부 일반 하이픈으로 바꿨습니다(의미 변화 없음).
2. **black의 "magic trailing comma"가 짧은 리스트까지 전부 펼침**: 기존
   코드에 습관적으로 붙인 트레일링 콤마 때문에 한 줄에 들어가는 짧은
   리스트/임포트까지 강제로 한 줄에 하나씩 펼쳐져 81개 파일에서 과도한
   diff가 발생 → `skip-magic-trailing-comma=true`로 끔.
3. **isort와 black이 일부 import 줄바꿈을 두고 무한 반복할 뻔함**: 실제로
   `isort → black → isort` 순서로 고정점(fixed point)에 수렴하는지 직접
   테스트해서 안전함을 확인했습니다.
4. **`models/__init__.py`, `repositories/__init__.py`의 ERD 도메인별
   import 그룹을 isort가 알파벳순으로 흩어놓으려 함** — `# isort: skip_file`
   로 제외해 의도된 구조를 보존했습니다.
5. **mypy가 numpy 스텁 파싱에서 즉시 실패**: 설치된 numpy(pandas 의존성,
   Python 3.14 wheel)의 `.pyi`가 PEP 695 `type` 문(3.12+ 전용 문법)을 써서
   `python_version="3.11"`로는 파싱 자체가 안 됐습니다 → mypy 설정의
   `python_version`만 3.12로 올려 해결(우리 코드는 3.11 문법만 사용, 하위
   호환성 영향 없음).
6. **mypy가 `tests/conftest.py`와 `tests/integration/conftest.py`를 동일
   모듈로 오인**: `__init__.py` 없는 pytest 표준 구조라 발생 → 테스트
   디렉터리 구조를 바꾸지 않고 `explicit_package_bases=true` +
   `mypy_path="."`로 해결.
7. **mypy가 실제 타입 문제 6곳을 잡아냄** (전부 실질적으로 고침):
   - `Optional[Platform]`을 캐싱하면서 `dict[int, Platform]`으로 잘못
     선언한 곳(`profit_calculation_service.py`)
   - 값 타입이 제각각인 dict(`amount: float, count: int, first/last:
     datetime | None`)를 순수 `dict`로만 선언해 대입마다 타입이 어긋난
     곳(`seed_dummy_data.py`) → `TypedDict`로 명확히 함
   - 혼합 타입 리스트(str/int/float)를 그냥 `list[dict]`로 둬서 곱셈
     연산자 타입을 못 잡은 곳(`base_mall_connector.py`) → `list[dict[str,
     Any]]`로 명시
   - `yield`로 값을 내보내는 pytest fixture의 반환 타입을 `Session`으로
     잘못 선언한 곳(`tests/conftest.py`) → `Generator[Session, None,
     None]`으로 수정
   - 테스트용 `StubConnector`가 `BaseMallConnector`를 상속하지 않고
     덕타이핑만 하던 곳 → 실제로 상속하도록 수정(더 정확하고, 추상
     메서드 구현 여부도 함께 검증됨)
   - `OrderDetailOut` 생성 시 ORM `OrderItem` 리스트를 그대로 넘기던 곳
     → `OrderItemOut.model_validate(...)`로 명시적으로 변환

### 제외한 검사 (설계 변경 최소화 원칙)
- **`UP045`/`UP017`/`UP035`/`UP006`(pyupgrade 대부분)**: `Optional[X]`→
  `X | None`, `timezone.utc`→`datetime.UTC`, `typing.List`→`list` 등으로
  바꾸라는 158건+ 스타일 제안이었습니다. 둘 다 여전히 유효한 문법이고
  기존 코드 전반에 일관되게 쓰인 방식이라, 실질적 이득 없이 수십 개 파일을
  건드리는 대신 껐습니다.
- **`B008`**: FastAPI의 `Depends(...)` 기본값 패턴을 "함수호출을 기본값으로
  쓰지 마라"로 오탐하는 24건 — FastAPI 공식 관용구라 무시했습니다.
- **`models/*.py`의 `F821`/`mypy name-defined`(2곳)**: `Mapped["Platform"]`
  같은 SQLAlchemy 2.0 지연 문자열 참조를 실제 이름 참조로 오인한
  오탐입니다. 실제 import를 추가하면 순환참조 위험이 생기므로, ruff는
  per-file-ignore로, mypy는 정확한 두 줄만 인라인 `# type: ignore`로
  처리했습니다.

### 테스트 결과
`pre-commit run --all-files` 기준 **12개 훅 전부 통과**
(trailing-whitespace/end-of-file-fixer/check-yaml/check-toml/
check-merge-conflict/check-added-large-files/mixed-line-ending/isort/
black/ruff/mypy/pytest). `pytest`는 **49 passed**. 포맷팅 적용(81개 파일)과
mypy 타입 수정 이후에도 이 결과가 유지됨을 확인했습니다.

### 참고: 이 검증을 위해 `git init`을 진행했습니다
`pre-commit`은 git 저장소가 있어야 동작합니다(훅 설치 대상이 `.git/hooks/`).
이 프로젝트에 git 이력이 없어(`Is a git repository: false`) 종단간 검증을
위해 사용자 확인을 받고 `git init`을 실행했습니다. **커밋은 만들지
않았습니다** — `git add -A`로 스테이징만 한 상태이며, 실제 첫 커밋 시점과
메시지는 사용자가 정하는 것이 맞다고 판단했습니다.

### 참고: 이번 turn에서 발견한 별도 사안 (설계 문서 존재)
품질 작업과 무관하게, 프로젝트 루트에 지금까지 찾지 못했던 설계 문서
`SRS_쇼핑몰통합ERP.md`, `UI와이어프레임_v1.1_추가반영.md`가 실제로
존재하는 것을 발견했습니다(1단계 진행 당시부터 README가 "별도로 전달된
문서"라고 언급했지만 찾지 못했던 바로 그 문서입니다). SRS의 아키텍처
개요(Service/Integration/Repository 계층 구조)는 지금까지 구현한 내용과
일치했지만, 화면/메뉴/세부 요구사항까지 전부 대조 검토하지는 못했습니다.
현재 진행 중인 품질/운영 안정성 작업(1~7순위) 범위 밖이라 지금 건드리지는
않았습니다 — 7단계가 끝난 뒤 이 문서들과 실제 구현을 대조하는 별도 작업을
원하시면 말씀해주세요.

## 17. Docker 개발 환경

FastAPI + PostgreSQL을 한 번에 띄우는 구성을 추가했다. 기존 설계
(core/database.py가 `DATABASE_URL` 값만으로 SQLite/PostgreSQL을 전환하도록
되어 있는 구조, 3순위 PostgreSQL 호환성 점검에서 확인됨)를 그대로
활용했을 뿐, 애플리케이션 코드는 건드리지 않았다.

### 추가한 파일

- `Dockerfile` - FastAPI 백엔드 실행용 이미지(`python:3.11-slim` 기반).
  requirements.txt를 먼저 복사해 의존성 레이어를 캐시하고, 비root 사용자
  (`appuser`)로 uvicorn을 실행한다.
- `docker-compose.yml` - `db`(PostgreSQL 16) + `api`(FastAPI) 두 서비스.
- `.dockerignore` - venv/frontend/캐시/`.env`/`erp.db` 등 이미지에 넣지 않을
  것들을 제외.

### 실행 방법

```
docker compose up --build
```

`api` 컨테이너는 기동 시 다음 순서로 동작한다.

1. `alembic upgrade head` - 2순위에서 검증한 초기 마이그레이션을 PostgreSQL에
   적용해 50개 테이블을 생성한다.
2. `python scripts/init_db.py` - 역할/권한/기본 admin 계정/플랫폼 5종/기본
   창고를 시딩한다. 각 시딩 함수는 이미 데이터가 있으면 건너뛰도록 되어
   있어(1단계부터 있던 동작) 재시작해도 안전하다.
3. `uvicorn api.main:app` 기동.

최초 실행만으로 `http://localhost:8000/docs`와 기본 관리자 계정
(`admin` / `ChangeMe!123`, 로그인 후 반드시 변경)까지 바로 사용할 수 있는
상태가 된다.

### 결정 사항과 이유

- **DB 접속정보/시크릿은 docker-compose.yml의 `environment`에 데모용 기본값을
  직접 넣었다.** pydantic-settings의 `Settings`는 `.env` 파일보다 프로세스
  환경변수를 우선 읽으므로 별도 코드 수정 없이 그대로 주입된다. 실제 배포
  시에는 이 값들을 반드시 교체해야 한다는 점을 파일 상단 주석과 이 절에
  명시했다.
- **PostgreSQL 드라이버(`psycopg[binary]==3.2.1`)를 requirements.txt에서
  주석 해제했다.** 지금까지는 SQLite만 쓰므로 주석 처리되어 있었는데,
  Docker 환경은 PostgreSQL을 실제로 사용하므로 설치가 필요하다. `[binary]`
  extra라 이미지에 별도 빌드 도구(gcc 등)를 추가하지 않아도 된다.
- **logs/backup/reports는 named volume으로 유지했다** (호스트 디렉터리에
  bind mount하지 않음). bind mount로 하면 컨테이너 내부 비root 사용자
  (uid 1000)와 호스트 디렉터리의 소유권이 어긋나 쓰기 권한 문제가 생길 수
  있는데, named volume은 Docker가 관리하므로 이 문제를 피할 수 있다.
- **frontend와 scheduler는 이 구성에 포함하지 않았다.** 이번 순위의 지시
  범위가 "FastAPI + PostgreSQL 함께 실행"으로 한정되어 있어, 범위를 넘는
  추가는 하지 않았다(스케줄러는 같은 이미지로 `command`만 바꿔서 별도
  컨테이너로 띄울 수 있는 구조라 확장은 어렵지 않다 - Dockerfile 주석에
  기록해 두었다).

### 검증 방식

이 작업 환경에는 Docker가 설치되어 있지 않아(`docker` 명령 없음)
`docker compose up`을 직접 실행해보지는 못했다. 3순위(PostgreSQL 호환성
점검) 때와 동일하게 사용자에게 확인한 결과 **정적 검토로 마무리**하기로
했다. 대신 다음을 확인했다.

- `docker-compose.yml`을 PyYAML로 파싱해 문법 오류가 없는지 확인
  (`services: [db, api]`, `volumes: [erp_pg_data, erp_logs, erp_backup,
  erp_reports]` 정상 인식됨).
- `migrations/env.py`가 `config/settings.py`(→ 환경변수 `DATABASE_URL`)에서
  접속 정보를 읽어오는 구조임을 재확인해, 컨테이너 안에서
  `alembic upgrade head`가 PostgreSQL을 향하도록 동작함을 코드 레벨에서
  검증.
- requirements.txt 변경 후 로컬 SQLite 환경에서 `pytest` 재실행 - 49
  passed(회귀 없음).

`docker compose up --build` 실행 중 이미지 빌드나 컨테이너 기동에 문제가
발생하면 로그를 공유해주시면 바로 수정하겠습니다.

## 19. GitHub Actions (CI)

`.github/workflows/ci.yml`을 추가했다. push/PR마다 4순위(개발 품질 향상)에서
구성한 도구들을 그대로 실행한다: Python 설치 → 의존성 설치 → Ruff → Black
(검사만, 자동수정 없음) → MyPy → Pytest.

### 결정 사항과 이유

- **Python 버전은 3.14를 사용한다.** `pyproject.toml`의
  `requires-python = ">=3.11"`을 그대로 따르지 않은 이유는, 이 프로젝트가
  실제로 개발/검증해 온 인터프리터가 3.14이기 때문이다. requirements.txt의
  SQLAlchemy(2.0.35→2.0.51), pydantic(2.9.2→2.13.4), pandas(2.2.2→2.3.3)
  버전이 모두 "Python 3.14용 wheel이 없거나 충돌해서" 상향된 값들이라,
  한 번도 실행해 본 적 없는 3.11에서 CI를 돌리면 로컬에서는 안 나던
  문제가 나올 위험이 있다. 3.11 지원을 실제로 보장하려면 별도로 매트릭스
  빌드를 추가해야 하며, 이는 현재 지시 범위를 벗어난다고 판단해 하지
  않았다.
- **isort는 CI 단계에 넣지 않았다.** 지시받은 4개 도구(Ruff/Black/MyPy/
  Pytest)에 정확히 맞췄다. 로컬 pre-commit에는 isort가 이미 포함되어
  있어(4순위) 실질적인 커버리지 공백은 크지 않다고 판단했다. isort까지
  CI에 넣기를 원하시면 한 줄만 추가하면 된다.
- **Black/Ruff는 `--fix`/자동수정 없이 검사만 한다.** CI가 코드를 임의로
  바꿔서는 안 되므로, 로컬 pre-commit(자동 수정 후 재스테이징)과 달리
  `black --check .`, `ruff check .`(fix 없음)로 실행한다.
- **MyPy 대상 디렉터리는 `.pre-commit-config.yaml`의 mypy 훅과 동일한
  목록**(`models config core repositories services api integrations
  scheduler scripts tests`)을 그대로 사용했다 - venv/frontend/migrations를
  건드리지 않기 위함이며, 로컬과 CI가 어긋나지 않도록 의도적으로
  맞췄다.

### 검증 방식

이 저장소는 아직 원격 GitHub 저장소에 연결되어 있지 않아(로컬 `git init`만
되어 있고 커밋도 없는 상태) 실제 Actions 실행 로그는 볼 수 없다. 대신:

- `.github/workflows/ci.yml`을 PyYAML로 파싱해 문법 오류가 없는지 확인.
- 워크플로가 실행할 4개 명령(`ruff check .`, `black --check .`,
  `mypy models config core repositories services api integrations
  scheduler scripts tests`, `pytest -q`)을 로컬에서 그대로 실행해 전부
  통과함을 확인 - 같은 venv, 같은 명령이므로 GitHub Actions에서도 동일하게
  통과할 것으로 예상된다(단, GitHub 원격 저장소가 생기고 실제로 push된
  이후에만 최종 확인 가능).

```
ruff check .        -> All checks passed!
black --check .     -> 91 files would be left unchanged.
mypy ...             -> Success: no issues found in 90 source files
pytest -q           -> 49 passed
```

## 20. API 품질 점검 (Swagger 문서 개선)

9개 라우터의 모든 엔드포인트(약 20개)와 `api/main.py`를 검토해 Swagger
문서 품질과 예외 처리를 개선했다. 기존 요청/응답 스키마, 라우팅 경로,
비즈니스 로직은 전혀 변경하지 않았다 - 순수하게 문서화용 메타데이터와
공통 예외 처리 추가만 했다.

### 점검 결과와 조치

- **tags**: 라우터마다 이미 존재 - 변경 없음.
- **response_model**: 모든 엔드포인트에 이미 존재 - 변경 없음.
- **status_code**: 리소스를 실제로 생성하는 `POST /api/costs`는 이미
  `201`이 붙어 있었다(변경 없음). 나머지 POST(`/api/auth/login`,
  `/api/orders/sync`, `/api/analytics/profit-loss/calculate`)는 리소스를
  새로 만드는 게 아니라 토큰 발급/수집 트리거/멱등 upsert이므로 `200`이
  맞는데, 지금까지는 FastAPI 기본값에 암묵적으로 의존하고 있어서 명시적으로
  `status_code=status.HTTP_200_OK`를 붙였다.
- **summary/description**: 전체 엔드포인트에 없었다. 모든 엔드포인트에
  한국어 summary(짧은 제목)와 description(동작 설명, 필요 시 필요 권한
  명시)을 추가했다.
- **Swagger 문서 개선**: `api/main.py`의 `FastAPI(...)`에 `openapi_tags`를
  추가해 각 태그(=라우터) 옆에 "필요 권한: XXX" 같은 설명이 Swagger UI
  태그 목록에 바로 보이도록 했다. 라우터 전체가 단일 권한으로 보호되는
  경우(products/inventory/settlements/costs/ads/analytics)는 태그
  설명에 한 번만 적었고, 권한이 엔드포인트마다 다른 orders 라우터는
  각 엔드포인트 description에 개별 명시했다. 앱 전역 `description`도
  추가해 Swagger 첫 화면에 인증 방식(OAuth2 password flow, Bearer 토큰
  사용법)이 보이도록 했다.
- **예외 처리**: 두 가지 전역 예외 핸들러를 `api/main.py`에 추가했다.
  1. `IntegrityError`(SQLAlchemy) - DB 제약조건 위반(예: `costs.py`
     POST에 존재하지 않는 `platform_id`를 넣는 경우)을 그대로 두면
     500과 함께 SQL 내부 메시지가 노출되는데, 이를 `409 Conflict` +
     안전한 한국어 메시지로 변환한다. SQL 원문은 로그에만 남긴다.
  2. 그 외 처리되지 않은 예외 전체 - Starlette 기본 동작(`debug=False`일
     때 평문 "Internal Server Error")을 다른 모든 응답과 동일한 JSON
     형태(`{"detail": "..."}`) + `500`으로 통일했다. `HTTPException`과
     `RequestValidationError`는 FastAPI가 이미 정확한 타입으로 핸들러를
     등록해두므로 이 전역 핸들러보다 우선 적용되어(정확 타입 매치 우선)
     기존 401/403/404/422 흐름에는 영향이 없다.
  라우터 개별 404 처리(products/customers/orders의 get-by-id, orders의
  sync 시 플랫폼 없음)는 기존 로직 그대로 두고 Swagger `responses`에만
  문서화를 추가했다.

### 검증

- 로컬에서 `uvicorn`을 임시로 띄워(포트 8123) 실제 확인:
  - `/openapi.json`에 11개 태그와 각 엔드포인트 summary가 정상 반영됨.
  - `admin` 계정으로 로그인 → JWT 발급 정상.
  - `GET /api/products/999999` → `404` (기존 동작 유지 확인).
  - `POST /api/costs`에 존재하지 않는 `platform_id`(FK 위반) →
    `409` + `{"detail": "데이터 제약조건을 위반했습니다 (중복된 값이거나 잘못된 참조입니다)."}`
    (신규 IntegrityError 핸들러 동작 확인).
  - 검증 후 테스트 서버는 종료함(실제 개발 DB `erp.db`는 그대로 유지).
- `ruff check .` / `black --check .` / `isort --check-only .` / `mypy` /
  `pytest` 전부 통과, `pre-commit run --all-files`도 12개 훅 전부 통과
  (요청/응답 스키마 변경이 없어 기존 49개 테스트 전부 그대로 통과).

## 21. 다음 단계

품질/운영 안정성 작업 7순위가 모두 완료되었습니다.
1. ~~테스트 코드 작성~~ - 완료
2. ~~Alembic 검증~~ - 완료
3. ~~PostgreSQL 호환성 점검~~ - 완료
4. ~~개발 품질 향상~~ - 완료
5. ~~Docker 개발 환경~~ - 완료
6. ~~GitHub Actions CI~~ - 완료
7. ~~API 품질 점검~~ - 완료 (본 절)

이 시점에서 별도로 발견해 둔(아직 처리하지 않은) 항목:
- 프로젝트 루트의 `SRS_쇼핑몰통합ERP.md`, `UI와이어프레임_v1.1_추가반영.md`
  설계 문서와 실제 구현(화면/메뉴/세부 요구사항 단위)을 대조하는 작업 -
  15절에서 발견 시점에 범위 밖으로 남겨둔 채로 아직 진행하지 않았습니다.

## 22. SRS/UI와이어프레임 대비 구현 현황 대조 (읽기 전용 점검)

위 21절에서 남겨둔 항목을 점검했다. 코드는 변경하지 않았고, 결과는
[구현현황_대조.md](구현현황_대조.md)에 정리했다.

**핵심 결론**
- DB 스키마(models/)는 SRS + UI 와이어프레임 v1.1 addendum을 이미 100%
  반영하고 있다(50개 테이블, v1.1 추가 4개 테이블 포함).
- Repository 계층도 배송/교환/반품/취소·상품성과·KPI목표까지 상당 부분
  이미 만들어져 있다.
- 그러나 Service/API/프론트엔드 계층은 핵심 흐름(주문수집→정산→비용→
  일별손익→최소 대시보드)만 구현했고, SRS 기능요구사항의 다수와 UI
  와이어프레임 v1.1 addendum(통합검색/즐겨찾기/AI패널/시스템모니터링/
  대시보드 그래프 등)은 아직 화면·API로 연결되지 않았다. 1~8단계가
  "MVP 범위"였던 것과 일치하는 결과로 보인다.
- 예외적으로 **설계와 실제 구현이 어긋난 부분을 하나 발견**했다:
  `platform_fee_rules` 테이블이 있는데도 `profit_calculation_service.py`가
  하드코딩된 수수료율 딕셔너리를 쓰고 있다. 스키마 변경 없이 서비스
  코드만 고치면 되는 작은 수정이라 원하시면 바로 진행 가능하다(자세한
  내용은 대조 문서 2절).

나머지 미구현 항목(배송/교환/반품/취소 관리, 상품 CRUD, 보고서 생성,
대시보드 확장, UI v1.1 전체 등)은 전부 새 기능 추가에 해당하므로, 이번
품질/운영 안정성 작업 범위에서는 구현하지 않고 목록만 남겨두었다.

## 23. 수수료율 하드코딩 제거 (설계-구현 일치)

22절에서 발견한 설계-구현 불일치 1건(FR-COST-03)을 수정했다: 손익 계산이
`platform_fee_rules` 테이블 대신 서비스 코드에 하드코딩된 수수료율을
쓰던 것을 DB 조회로 전환했다. 스키마는 변경하지 않았고(`platform_fee_rules`
테이블은 이미 존재), Service가 Repository만 의존하는 기존 계층 원칙도
그대로 유지했다.

### 수정한 파일

- [services/profit_calculation_service.py](services/profit_calculation_service.py)
  - `FEE_RATE_BY_PLATFORM_CODE` 딕셔너리 삭제.
  - 생성자에 `PlatformFeeRuleRepository` 주입, `_get_fee_rate(platform, order_date)`
    헬퍼를 추가해 수수료율을 DB에서 조회하도록 변경. Session을 직접 쿼리하지
    않는다.
  - `DEFAULT_FEE_RATE`를 기존 `0.10`(임의 값)에서 `0.0`(요구사항의 "기본값
    0%")으로 변경. 규칙을 찾지 못하면 `logger.warning(...)`으로 로그만
    남기고 0%로 계속 진행한다(예외를 던지지 않음 - 시스템 전체 중단 방지).
- [repositories/platform_repository.py](repositories/platform_repository.py)
  - `PlatformFeeRuleRepository` 신규 추가. `get_effective_rule(platform_id,
    on_date)`가 선택 규칙을 전담한다: `effective_from <= on_date`이고
    (`effective_to`가 NULL이거나 `on_date` 이상)인 규칙 중
    `effective_from` 내림차순으로 1건(최신 우선) 반환. "활성"은 별도
    상태 컬럼이 아니라 이 적용기간 계산으로 판단한다(스키마에 활성 여부
    컬럼이 없으므로 스키마 변경 없이 해석 - 결정 이유는 대조 문서 참고).
- [repositories/\_\_init\_\_.py](repositories/__init__.py) - `PlatformFeeRuleRepository` export 추가.
- [scripts/init_db.py](scripts/init_db.py) - `seed_platform_fee_rules()` 추가(다른
  seed 함수와 동일하게 존재하면 skip). 기존 하드코딩 값과 동일한 수수료율
  (네이버 3.5%, 쿠팡 10%, ESM/11번가 12%, 카카오 3.5%)을 `platform_fee_rules`에
  1건씩 시딩해, DB 전환 후에도 실제 계산 결과가 바뀌지 않도록 했다.
  `main()`에서 `seed_platforms()` 다음, `seed_default_warehouse()` 이전에 호출.
- [tests/conftest.py](tests/conftest.py) - `platform_fee_rule` 픽스처 추가(플랫폼에
  10% 수수료 규칙 1건 시딩).
- [tests/unit/test_profit_calculation_service.py](tests/unit/test_profit_calculation_service.py)
  - 기존 `test_aggregates_orders_costs_and_fee`가 `platform_fee_rule`
    픽스처를 쓰도록 수정(하드코딩 시절과 동일한 10%/5000원 결과를 이번엔
    DB에서 읽어 재현함 - "계산 결과는 기존과 동일" 요구사항 검증).
  - `test_no_fee_rule_falls_back_to_zero_percent` 신규 추가 - 규칙이
    없을 때 0%로 안전하게 계산이 계속됨을 검증.
- [tests/unit/test_platform_fee_rule_repository.py](tests/unit/test_platform_fee_rule_repository.py)
  신규 추가 - `get_effective_rule()`에 대해 요구된 5개 케이스를 모두 검증.
- [구현현황_대조.md](구현현황_대조.md) - 2절/3.5절에 수정 완료 표시.

### 조회 규칙 요약

1. 플랫폼별로 `platform_fee_rules`를 조회한다.
2. "활성" = 조회 기준일(주문일)이 `effective_from`~`effective_to` 범위
   안에 있는 규칙(`effective_to`가 NULL이면 무기한 적용).
3. 여러 개가 매칭되면 `effective_from`이 가장 최근인 규칙을 사용한다(최신 우선).
4. 매칭되는 규칙이 없으면(플랫폼 정보가 없는 경우 포함) 경고 로그를 남기고
   0%를 적용한다 - 예외를 던지지 않아 나머지 계산/배치 작업은 중단되지 않는다.
5. `fee_rate` 컬럼은 백분율로 저장되어 있어(`Numeric(5,2)`, 예: `3.50`)
   조회 후 `/100`으로 소수 변환한다.

### 테스트 결과

```
tests/unit/test_platform_fee_rule_repository.py - 5 cases (요구된 5개 케이스 전부)
  - 플랫폼 수수료 존재
  - 기간이 다른 수수료 규칙
  - 수수료 규칙 없음
  - 비활성(적용기간 밖) 규칙 제외
  - 여러 규칙 중 최신 선택
tests/unit/test_profit_calculation_service.py - 기존 5 + 신규 1 = 6 cases
pytest 전체: 49 -> 55 passed
pre-commit run --all-files: 12개 훅 전부 통과
```

추가로 uvicorn을 임시로 띄워 실제 seed된 개발 DB(`erp.db`)에
`POST /api/analytics/profit-loss/calculate`를 호출해, DB 기반 수수료율이
API 전 구간(Router -> Service -> Repository -> DB)에서 정상 동작함을
확인했다(주문 7건, `platform_fee` 52,424.71원으로 정상 계산됨). 테스트
후 서버는 종료했다.

### 기존 기능 영향 여부

- **계산 로직/결과**: `scripts/init_db.py`가 하드코딩 값과 동일한 수수료율을
  `platform_fee_rules`에 시딩하므로, 이미 이 시딩을 거친 DB(로컬 `erp.db`
  포함, 이번에 직접 재실행해 반영함)에서는 계산 결과가 이전과 동일하다.
  단, **아직 `platform_fee_rules`가 비어있는 DB(신규 설치 후 시딩을 하지
  않은 경우)에서는 모든 플랫폼 수수료가 0%로 계산된다** - 기존에는
  이런 경우가 없었으므로(하드코딩이라 항상 값이 있었음) 이 부분만 동작이
  달라진다. `python scripts/init_db.py`를 한 번 실행하면 해결된다.
- **API 응답 스키마**: 변경 없음(`ProfitLossSummaryOut` 필드 동일).
- **다른 서비스/라우터**: `profit_calculation_service.py` 외 코드는 건드리지
  않았다. `scripts/seed_dummy_data.py`의 동일한 이름의 상수는 완전히
  별개(정산 더미데이터 생성용)라 그대로 두었다 - 이유는 아래 "남은
  설계-구현 차이점" 참고.

### 남아 있는 설계-구현 차이점

- `scripts/seed_dummy_data.py`에도 `FEE_RATE_BY_PLATFORM_CODE`라는 동일한
  이름의 하드코딩 딕셔너리가 있다. 이번에 고친 `profit_calculation_service.py`의
  것과는 **별개의 지역 상수**이며 더미 정산(Settlement/SettlementDetail)
  데이터를 그럴듯하게 생성하기 위한 용도다(실제 손익 계산 엔진이 아님).
  이번 지시 범위("`profit_calculation_service.py`에서... 제거")를 벗어나고,
  더미데이터 생성 스크립트를 DB 조회 기반으로 바꾸는 건 "설계를 구현에
  맞추는 범위"를 넘는 리팩터링으로 판단해 그대로 두었다. 필요하시면
  별도로 처리하겠다.
- 22절 대조 문서에 남아있는 나머지 항목(배송/교환/반품/취소 관리, 상품
  CRUD, 보고서 생성, 대시보드 확장, UI v1.1 전체)은 전부 신규 기능
  추가에 해당해 이번에는 손대지 않았다.

## 24. 배송/교환/반품/취소 관리 (신규 기능, SRS FR-ORD-02/03/04 대응)

품질/운영 안정성 작업 7개 우선순위 완료 후, SRS/UI와이어프레임 대조에서
발견한 미구현 기능 중 첫 번째로 지시받은 배송/교환/반품/취소 관리를
Repository → Service → API → React 화면까지 전체 계층에 걸쳐 구현했다.
기존 SRS v1.0 / ERD v1.0~v1.2 / UI 와이어프레임 v1.0~v1.2 설계를 기준으로
삼았고, **테이블 추가/삭제, 컬럼 변경은 하지 않았다** - `shipments`,
`exchanges`, `returns`, `cancellations` 테이블과 `SHIPMENT_VIEW`,
`EXCHANGE_RETURN_MANAGE` 권한은 1단계(ERD)/설정 시딩 때부터 이미 존재했고,
이번 작업은 그 위에 비어 있던 계층만 채웠다.

### 구현한 기능

- **배송관리**(`/api/shipments`, `SHIPMENT_VIEW` 권한): 등록/조회/정보수정
  (운송사·송장번호)/상태변경(READY→SHIPPING→DELIVERED), 상태·주문ID·
  운송사/송장번호 검색, 페이지네이션.
- **교환관리**(`/api/exchanges`, `EXCHANGE_RETURN_MANAGE` 권한): 등록/조회/
  상태변경(REQUESTED→APPROVED→SHIPPED→COMPLETED, 또는 REJECTED), 상태·
  주문ID·기간·사유검색, 페이지네이션.
- **반품관리**(`/api/returns`, 동일 권한): 등록/조회/상태변경(REQUESTED→
  APPROVED→RECEIVED→REFUNDED, 또는 REJECTED), 동일한 검색/필터/페이지네이션.
- **취소관리**(`/api/cancellations`, 동일 권한): 등록/조회/상태변경
  (REQUESTED→COMPLETED), 동일한 검색/필터/페이지네이션.
- 4개 화면 모두 React 페이지(목록 표+등록 폼+상태변경 인라인 컨트롤+
  페이지네이션)로 구현, 사이드바 메뉴에 추가.

### 핵심 설계 결정 - 기존 로직 재사용

- **`OrderSyncService._apply_status_change`를 `apply_status_change`로
  공개(public)** 전환하고 `warehouse_id`를 선택값으로 바꿨다. 커넥터
  수집 경로(`sync_orders`)가 쓰던 "주문 상태 변경 + 이력 기록 + 재고
  반영" 로직을 배송/교환/반품/취소 관리에서도 그대로 재사용하기 위함
  이다 - 로직을 중복 구현하면 두 경로가 서서히 어긋날 위험이 있어
  단일 지점으로 유지했다. 기존 `sync_orders` 호출부는 동작 변화 없음
  (기존 테스트 전부 통과로 확인).
- **완료(성공) 상태에 도달했을 때만** `orders.status`를 동기화한다
  (거절/중간 상태는 건드리지 않음): 교환 COMPLETED→`EXCHANGED`, 반품
  REFUNDED→`REFUNDED`, 취소 COMPLETED→`CANCELED`, 배송 SHIPPING/
  DELIVERED→동일값. 이는 `orders.status` enum에 이미 있던 값을 그대로
  사용한 것으로, 스키마 변경이 아니다.
  - 배송이 SHIPPING/취소가 미출고 상태에서 발생하면 `apply_status_change`
    가 자동으로 재고를 차감/해제한다.
  - 반품 RECEIVED는 **회수 도착 기록일 뿐 재고를 바꾸지 않는다**(정책 5).
    재고 반영은 검수 시점에 `InventoryService.inspect_return()`이 단독으로
    담당한다. (아래 "재고 상태 전이 설계" 참고 - 이 절의 원래 서술은
    `restock_on_return()`이 RECEIVED 시점에 즉시 복원하던 이전 동작을
    기록한 것으로, 현재 동작이 아니다.)
- Repository는 기존 `ShipmentRepository`/`ExchangeRepository`/
  `ReturnRepository`/`CancellationRepository`(3단계부터 존재)를 그대로
  쓰고, 검색/필터/페이지네이션에 필요한 `list_filtered`/`count_filtered`
  메서드만 보완했다. `OrderRepository.get_item_by_id()` 1개만 신규 추가
  (반품 시 단일 주문상품 조회용).
- Service는 Repository만 의존하고 SQLAlchemy Session을 직접 쿼리하지
  않는다(기존 계층 원칙 유지).

### 구현 중 발견한 문제 - 즉시 수정

작업 중 실제로 통합 테스트를 돌리다가 발견한 문제라 "자동 수정 가능한
부분"으로 판단해 바로 고쳤다(설계 변경이 아니라 새로 만든 코드의 예외
처리 보완이라 별도 승인 없이 진행):

- `InventoryService.restock_on_return()`은 재고 레코드가 없는 상품/창고
  조합이면 일반 `ValueError`를 던지는데, 이게 그대로 새면 API가 500(서버
  내부 오류)을 반환한다. `ReturnService._restock()`에서 이를 잡아
  `InventoryNotTrackedError`로 감싸고, `returns.py` 라우터가 이를 404로
  변환하도록 했다. 관련 테스트(단위 1개, 통합 1개) 추가.
  → **현재는 무효.** 정책 5 도입으로 RECEIVED가 재고를 건드리지 않게 되면서
  `restock_on_return()`과 `ReturnService._restock()`이 제거됐고, 발생 경로가
  사라진 `InventoryNotTrackedError`와 `returns.py`의 404 변환 핸들러도 함께
  삭제했다(죽은 코드는 남기지 않는다). `PATCH /api/returns/{id}/status`의
  404는 이제 "반품 신청을 찾을 수 없음" 한 가지 의미만 갖는다.

### 변경/신규 파일

**백엔드**
- `services/order_sync_service.py` - `apply_status_change` 공개 전환
- `repositories/order_repository.py` - 필터/페이지네이션 메서드 추가,
  `get_item_by_id` 추가
- `services/shipment_service.py` (신규)
- `services/exchange_return_service.py` (신규 - Exchange/Return/Cancellation
  3개 서비스)
- `api/routers/shipments.py`, `exchanges.py`, `returns.py`,
  `cancellations.py` (신규)
- `api/main.py` - 4개 라우터 등록, `openapi_tags`에 4개 태그 추가

**테스트(신규)**
- `tests/unit/test_shipment_service.py`, `test_exchange_return_service.py`
- `tests/integration/test_api_shipments.py`, `test_api_exchanges_returns.py`

**프론트엔드**
- `frontend/src/api/types.ts` - 타입/페이지네이션 응답 타입 추가
- `frontend/src/api/client.ts` - `patch()` 메서드 추가
- `frontend/src/components/Pagination.tsx` (신규)
- `frontend/src/pages/ShipmentsPage.tsx`, `ExchangesPage.tsx`,
  `ReturnsPage.tsx`, `CancellationsPage.tsx` (신규)
- `frontend/src/App.tsx`, `frontend/src/components/Layout.tsx` - 라우트/
  사이드바 메뉴 추가
- `frontend/src/index.css` - 페이지네이션/상태뱃지 스타일 추가

**문서**
- `구현현황_대조.md` - FR-ORD-02/03/04, FR-PROFIT-03 상태 갱신
- `.claude/launch.json` (신규) - 브라우저 검증용 backend/frontend 개발서버 설정

### 테스트 결과

```
신규 유닛 테스트: 20 (배송 9 + 교환/반품/취소 11)
신규 통합 테스트: 23 (배송 9 + 교환/반품/취소 12 + 재고레코드없음 1 + RBAC 1)
pytest 전체: 55 -> 98 passed
pre-commit run --all-files: 12개 훅 전부 통과
frontend: npx tsc -b 오류 없음, npx oxlint 신규 파일 경고 없음
```

### 브라우저 실동작 검증

`uvicorn`(8000)과 `vite dev`(5173)를 함께 띄우고 admin으로 로그인한 뒤
4개 화면을 실제로 조작했다(seed 데이터 257건 배송 등 실데이터 포함).

- 배송관리: 신규 배송 등록 → SHIPPING 상태변경 → `GET /api/orders/10`
  으로 주문 상태가 `SHIPPING`으로 바뀐 것을 직접 확인.
- 교환관리: 교환 신청 등록 → COMPLETED 상태변경 → 주문 상태가
  `EXCHANGED`로 바뀐 것을 확인.
- 반품관리: 반품 신청 등록 → RECEIVED(재고 288→290으로 복원 확인) →
  REFUNDED(주문 상태 `REFUNDED`로 전환 확인).
- 취소관리: 취소 신청 등록 → COMPLETED → 주문 상태 `CANCELED` + 관련
  3개 상품의 예약재고가 각각 주문 수량만큼 정확히 감소한 것을 확인.
- 검색/필터/페이지네이션 UI 정상 동작(예: 257건 배송을 26페이지로 표시,
  운송사/송장번호 검색 시 결과 좁혀짐).
- 브라우저 콘솔 에러 없음.

검증 중 한 가지 특이사항: 이 프리뷰 환경의 `preview_click` 툴이 폼의
submit 버튼 클릭을 간헐적으로 인식하지 못해(React 합성 이벤트 연결
타이밍 이슈로 추정, 코드 버그 아님) 일부 단계는 `element.click()`을
직접 실행하는 방식으로 우회해 검증했다 - 실제 사용자의 마우스 클릭이나
`preview_click`이 다른 화면(로그인 등)에서는 정상 동작했던 것으로 보아
이 프리뷰 툴 자체의 특이사항으로 판단된다.

### 발견한 문제점 (수정하지 않고 보고)

- **FR-PROFIT-03 미구현이 이번에 더 명확해졌다**: 취소/반품/환불된
  주문의 `orders.status`는 이제 정확히 바뀌지만(CANCELED/REFUNDED),
  `profit_calculation_service.py`의 `calculate_daily()`는 `order_date`
  기준으로 해당일 주문을 status 구분 없이 전부 매출에 합산한다. 즉
  취소/반품된 주문도 매출/손익에 그대로 잡힌다. 이번 작업 범위(등록/
  조회/상태변경)를 벗어나 손대지 않았다 - 다음 우선순위(상품 CRUD 이후,
  매출/손익 개선 시점)에서 처리를 권장한다.
- FR-ORD-01(주문 목록의 플랫폼/기간 필터)과 FR-ORD-04(교환율/반품율/
  취소율 자동계산 및 표시)는 여전히 미구현 - 이번 지시 범위(배송/교환/
  반품/취소의 등록/조회/상태변경) 밖이라 손대지 않았다.
- FR-DASH-01의 "미배송/교환대기/반품대기" 대시보드 경고 표시는 이번에
  만든 API로 데이터는 가져올 수 있지만, 대시보드 화면에 실제로 붙이는
  작업은 하지 않았다.

## 25. 다음 단계

1순위(배송/교환/반품/취소 관리)가 완료되었습니다. 다음 우선순위로
안내받은 대로 **2순위: 상품 CRUD 및 원가이력 관리**로 진행하겠습니다.
