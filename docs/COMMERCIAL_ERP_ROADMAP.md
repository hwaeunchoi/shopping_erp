# 상용 ERP 확장 로드맵 (사방넷/플레이오토/셀메이트/샵링커 수준)

이 문서는 `shopping_erp`를 다채널 상용 ERP 수준으로 확장하기 위한 6단계 로드맵과,
각 단계의 완료 기준·채널별 지원 현황(Capability Matrix)을 정리한다.

- **1단계**(`feature/commercial-erp-stage1-shipment-sync`): 구현·격리 검증
  완료(pytest/Ruff/MyPy, 격리 PostgreSQL, 클린 워크트리 재검증). **운영 실전송은
  비활성**(`shipment_channel_submit_enabled`/`channel_status_sync_enabled` 모두 기본
  False) - 활성화 전 필요 조건은 "실전송 활성화 체크리스트" 참고.
- **2단계**(`feature/commercial-erp-stage2-claims-settlements`, 1단계 완료 커밋
  `d25ea73`에서 분기): 취소/반품/교환/정산 "수집·상태 갱신·금액 대사·조회 UI"
  구현·격리 검증 완료. **승인/거부/환불실행/반품완료처리/교환재발송 등 채널
  상태를 실제로 바꾸는 조치는 이번 단계에 없음** - 아래 "2-B단계"로 분리.
  운영 자동/수동 수집 모두 신규 플래그 `claims_settlement_sync_enabled`(기본
  False)로 차단.

## 공통 원칙 (모든 단계에 적용)

- **공식 API 문서 기반**: 채널 연동 세부사항(엔드포인트/스키마/인증/코드값)은 반드시
  해당 채널의 공식 개발자 문서로 확인한 것만 구현한다. 추측으로 필드명/코드값을
  채우지 않는다. 확인 불가 항목은 `MarketplaceCapabilityUnsupportedError`로 명시적
  실패 처리하고, 성공한 것처럼 위장하지 않는다.
- **Outbox + Idempotency**: 채널로 나가는 모든 쓰기(write) 호출은 `ExternalCommand`
  아웃박스를 통해 `idempotency_key` 기준으로 중복 호출을 막고, 상태(PENDING/RUNNING/
  SUCCESS/FAILED/RETRY_WAIT/CANCELLED)와 재시도 가능 여부를 기록한다.
- **상태 전이 검증**: 주문/배송 등 상태 변경은 `services/order_state_machine.py`류의
  허용 전이 테이블로 검증한다. 허용되지 않는 전이는 자동으로 덮어쓰지 않고
  `OrderStatusConflict`처럼 운영자 확인 대상으로 남긴다.
- **PII/Secret 비기록**: Secret, 고객 개인정보(주소/연락처), 원본 API 응답 전체를
  로그에 남기지 않는다. 아웃박스의 `request_summary`/`response_summary`는 안전한
  요약만 담는다.
- **운영 코드 경로 보호**: 기존에 운영 중인 흐름(`scheduler/jobs/order_collect_job.py`,
  `OrderSyncService.apply_status_change()` 등)은 새 아키텍처로 강제 이관하지 않고,
  신규 기능은 애디티브(additive)한 새 서비스/API 경로로 추가한다. 기존 흐름을
  새 경로로 이관할지는 각 단계별로 별도 결정한다.

## 단계별 로드맵

### 1단계 - 송장/상태 양방향 동기화 (구현 완료, 완결 검토 반영)

- **범위**: 네이버 스마트스토어/쿠팡에 대해 (a) 발송 처리 시 채널로 송장(운송사+
  송장번호) 전송, (b) 채널 주문상태를 수집해 내부 주문상태와 비교 후 허용된 전이면
  반영, 아니면 충돌 기록.
- **완료 기준**:
  - 분할배송/부분출고를 정확히 표현한다: 쿠팡 배송묶음(shipmentBoxId)은 주문
    단위가 아니라 라인(`OrderItem`) 단위로 저장하고(`OrderItem.platform_shipment_box_id`),
    `ShipmentDispatchService._dispatch_all_lines()`는 `shipment_items.order_item_id`를
    거쳐 그 배송에 실제로 연결된 라인만 전송한다(합포장/분할배송/부분출고 모두
    모델 변경 없이 이미 지원되던 `Shipment`↔`Order`/`OrderItem` N:M 구조를 그대로
    사용). (충족 - 최초 구현에서 주문 전체 대표값 1개로 잘못 설계했던 것을
    완결 검토에서 발견/수정.)
  - 채널로 나가는 쓰기는 outbox(`ExternalCommand`)를 실제로 실행하는 별도 경로가
    있다: API(`POST /api/shipments/{id}/submit`)는 `enqueue()`만 호출해 PENDING
    명령을 만들고 202를 즉시 반환하며, 실제 채널 호출은
    `scheduler.jobs.outbox_dispatch_job`이 주기적으로 `execute_command()`를 호출해
    수행한다. 재시도 가능한 실패는 RETRY_WAIT(+ 백오프)로, 재시도 불가능하거나
    소진된 실패는 FAILED로 확정한다(무한 재시도 방지, `MAX_ATTEMPTS`). RUNNING으로
    너무 오래 머무는 명령(worker 크래시 추정)은 `recover_stale_running()`이 회수한다.
    (충족 - 최초 구현은 API 요청 스레드에서 동기 실행했던 것을 완결 검토에서
    비동기 outbox 실행으로 재설계.)
  - `OrderChannelSyncService.sync_channel_status()`가 실제 실행 경로를 갖는다:
    (a) 송장 전송 성공 직후 연결된 주문에 "SHIPPING"을 반영(허용된 전이만), (b)
    `scheduler.jobs.channel_status_sync_job`이 주기적으로 채널 상태를 읽기전용
    재조회해 반영한다(운영 중인 `order_collect_job`은 변경하지 않는다). 허용되지
    않는 전이는 `OrderStatusConflict`로 남기며, 같은 채널상태로 반복 감지돼도
    미해소 충돌 행을 중복 생성하지 않는다. 운영자는
    `POST /api/order-conflicts/{id}/resolve`로 해소한다. (충족 - 최초 구현은
    서비스만 만들고 아무 경로도 호출하지 않는 dead code였던 것을 완결 검토에서
    실행 경로 2개로 연결.)
  - MockTransport 기반 계약 테스트로 요청 스키마/실패 처리/PII 비기록을 검증한다.
    (충족 - `test_naver_smartstore_connector.py`/`test_coupang_connector.py`의
    `TestSubmitShipment`, `test_shipment_dispatch_service.py`의 분할배송/재시도/
    stale RUNNING 회수 테스트)
  - 실계정 검증은 별도 단계(아래 "실계정 검증 전 필요 조건" 참고, 아직 미충족).

### 2단계 - 취소/반품/교환/정산 채널 연동 (구현·격리 검증 완료)

- **범위**: 이미 존재하는 내부 취소/반품/교환/정산 모델을 각 채널의 공식
  조회 API와 연동해 **수집·상태 갱신·금액 대사·조회 UI**만 구현한다. 승인/거부/
  환불실행/반품완료처리/교환재발송 등 채널 상태를 실제로 바꾸는 조치는 범위
  밖이며 "2-B단계"로 분리한다(아래 참고).
- **의존성**: 1단계의 outbox/상태전이/SAVEPOINT 격리 패턴을 재사용. 기존
  `ClaimSyncService`/`services/exchange_return_service.py`를 확장(중복 모델
  생성 없음).
- **완료 기준(충족)**:
  - 채널별 실제 지원 캐패빌리티는 공식 문서로 확인한 것만 True로 켠다(아래
    Capability Matrix 참고) - 미확인 API는 구현을 지어내지 않고 capability만
    False로 유지.
  - 채널 claim ID 기준 dedup + 재수집 시 상태 갱신(과거 상태로 되돌리지 않음),
    claim ID가 없는 경우의 보수적 폴백, 아직 수집되지 않은 주문에 걸린 클레임은
    `ClaimUnmatched`에 보관 후 주문 수집 시 자동 승격.
  - 정산 회차/상세는 전부 `Decimal`로 계산하고, 회차-상세 매칭 실패/금액 불일치는
    `SettlementDiscrepancy`로 분리 기록(자동 추정으로 덮어쓰지 않음), 재수집 시
    중복 생성 방지.
  - 자동/수동 수집 모두 `claims_settlement_sync_enabled`(기본 False)로 차단 -
    OFF일 때 외부 호출 0건(세션도 열지 않음).
  - MockTransport 기반 단위/통합 테스트, 격리 PostgreSQL 마이그레이션 검증,
    클린 워크트리 재검증 완료(아래 "2단계 완료 요약" 참고).
  - 실계정 검증은 별도 승인 필요(아직 미실시).

#### 2-B단계 - 클레임/정산 상태 변경(승인/환불/처리) 액션 (미구현, 향후 분리 진행)

2단계에서 명시적으로 **구현하지 않은** 항목들 - 채널에 실제 쓰기 요청을 보내
상태를 바꾸는 조치이므로, 1단계의 outbox(멱등키+atomic lease+UNKNOWN 처리)
패턴과 동일한 안전장치를 갖춘 뒤 별도 단계로 진행해야 한다.

- 취소 승인/거부(채널로 실제 승인·거부 요청 전송)
- 반품 환불 실행(채널/PG 환불 API 호출)
- 반품 완료 처리(수거확인 등 채널 상태 갱신 쓰기)
- 교환 재발송 처리(교환 상품 재출고 확정 채널 쓰기)
- 위 각 항목은 1단계에서 확립한 "결과 불명(timeout/connection-drop/parse
  실패/외부성공-로컬커밋실패)은 절대 자동 재시도하지 않고 UNKNOWN/RECONCILIATION_REQUIRED로
  전환" 원칙과 DB-atomic claim/lease 동시성 제어를 그대로 적용해야 한다.

### 3단계 - 상품/재고 동기화

- **범위**: 네이버 상품 "수집"(`fetch_products`/`product_sync_job`)은 이미 운영 중이나
  (capability matrix 참고, 1단계에서 변경 없음), 내부→채널 방향 "쓰기"(재고/가격/
  품절 반영)와 나머지 채널의 상품 연동은 아직 없다. 이 단계에서 내부 상품/옵션/
  재고를 채널별 상품 등록/수정/재고 동기화 API와 연동하고, 품절/가격 변경 시
  다채널 반영, 채널 상품코드-내부 SKU 매칭 규칙을 정비한다.
- **의존성**: `models/product.py`의 기존 `ProductOption`/`platform_option_id` 매칭
  구조를 확장. 재고 동기화는 다건 배치 처리가 필요하므로 1단계의
  `session.begin_nested()` 부분성공/격리 패턴을 재사용.
- **완료 기준**: 재고 변경 이벤트가 outbox를 통해 각 채널로 전파되고, 채널 응답
  실패 시 내부 재고와 채널 표시 재고 간 불일치를 감지/알림할 수 있어야 한다.

### 4단계 - ESM/11번가/카카오 채널 확장

- **범위**: 이미 `BaseMallConnector`를 상속한 `EsmConnector`/`ElevenstConnector`/
  `KakaoShoppingConnector` 스텁에 실제 연동(주문 수집, 1~3단계 기능)을 채운다.
- **의존성**: 1~3단계 패턴이 안정화된 이후 진행 - 신규 채널마다 동일한 outbox/
  상태전이/캐패빌리티 설계를 반복 적용하면 되므로, 반드로 1~3단계 완료 후 시작.
- **완료 기준**: 각 채널 공식 문서 기준 MockTransport 계약 테스트, capability
  matrix의 해당 열이 실제 구현 항목만 True로 표시.

### 5단계 - CS/풀필먼트/택배사 연동

- **범위**: CS 문의-주문 연결, 택배사 API를 통한 실시간 배송추적, 물류센터
  연동(WMS 트리거).
- **의존성**: 1단계의 `carrier_codes.py` 정규화 테이블 확장 필요(현재
  `CJ_LOGISTICS`만 등록, 나머지는 `UnknownCarrierError`로 명시적 거부 중).
  택배사 추가 시 반드시 공식 문서 교차 확인 후 등록.
- **완료 기준**: 택배사 실시간 조회 API 연동, CS 문의-주문-배송 상태 연결 화면.

### 6단계 - 대량처리/대시보드 고도화

- **범위**: 대량 주문/송장/재고 일괄 처리 UI, 채널 통합 대시보드(동기화 실패율,
  충돌 건수, 재시도 대기 건수 등 운영 지표).
- **의존성**: `ExternalCommand` 아웃박스에 이미 쌓이는 상태/재시도 데이터를
  집계하는 것이 핵심이므로, 1~5단계에서 쌓인 데이터가 있어야 의미 있는
  대시보드가 된다.
- **완료 기준**: 실패/재시도 대기 건수 대시보드, 대량 재처리(`submit_many`류) UI.
- **알려진 한계(완결 검토에서 발견, 6단계에서 반드시 재검토)**:
  `ShipmentDispatchService.submit_many()`는 건별로 SAVEPOINT(`session.begin_nested()`)로
  격리하는데, 실패한 건은 `savepoint.rollback()`으로 그 건의 `ExternalCommand` 행
  자체(INSERT 포함)까지 되돌아간다 - 즉 대량 처리에서 실패한 건은 outbox에 이력이
  남지 않는다(반대로 API 엔드포인트가 쓰는 `enqueue()`+`outbox_dispatch_job` 경로는
  건별로 독립 커밋하므로 이 문제가 없다). 대량처리 API를 만들 때는 SAVEPOINT
  방식 대신 건별 독립 커밋(또는 실패해도 outbox 행은 보존하고 도메인 변경만
  롤백하는 방식)으로 재설계해야 한다.

## Capability Matrix (2단계 기준 현황)

| 캐패빌리티 | 네이버 스마트스토어 | 쿠팡 | ESM | 11번가 | 카카오쇼핑 |
|---|---|---|---|---|---|
| 주문 수집 (`fetch_orders`) | O (기존 구현) | O (기존 구현) | X (`CapabilityUnsupported`) | X (`CapabilityUnsupported`) | X (`CapabilityUnsupported`) |
| 송장 전송 (`submit_shipment`, 1단계, outbox 비동기 실행) | O | O | X | X | X |
| 채널 상태 동기화 (전송성공 반영 + 주기적 읽기전용 재조회) | O | O | X | X | X |
| 취소 수집 (`fetch_cancellations`, 2단계) | X (일괄조회 API 미확인) | X (`cancelType=CANCEL` 조회 시 `orderId` 필수가 되어 날짜range 일괄조회 불가 - 공식 문서로 확인된 구조적 한계) | X | X | X |
| 반품 수집 (`fetch_returns`, 2단계) | X (일괄조회 API 미확인) | **O (returnRequests v6, 상태코드 4종 순회로 전체 수집)** | X | X | X |
| 교환 수집 (`fetch_exchanges`, 2단계) | X (일괄조회 API 미확인) | **O (exchangeRequests v4, 최대 7일 range)** | X | X | X |
| 정산 회차 수집 (`fetch_settlements`) | X (공식 API 미확인) | **O (settlement-histories v1, 2단계에서 실구현으로 교체)** | X | X | X |
| 정산 상세 수집 (`fetch_settlement_details`, 2단계 신규) | X | **O (revenue-history v1, 주문/라인 단위)** | X | X | X |
| 클레임 승인/거부/환불실행/처리(2-B단계) | X | X | X | X | X |
| 상품 동기화 (`fetch_products`) | **O (기존 구현, 1단계 이전부터 운영 중 - `product_sync_job` 20분 주기, 1단계에서 변경 없음)** | X | X | X | X |
| 재고 동기화 (3단계 예정) | X | X | X | X | X |

- O = 실제 구현 + MockTransport 계약 테스트로 검증됨 (실계정 검증은 별도).
- X = 미구현/미확인. 호출 시 `MarketplaceCapabilityUnsupportedError`를 명시적으로
  발생시키며, 성공을 가장하지 않는다
  (`tests/unit/test_marketplace_safety.py::TestShipmentSubmitCapabilityNeverFakesSuccess`,
  `tests/unit/test_base_mall_connector.py::TestRealConnectorsClaimCapabilitiesMatchConfirmedContract`로 회귀 검증).
- 쿠팡 반품/교환/정산 API 확인 근거: `integrations/malls/coupang_connector.py` 상단 및
  각 fetch 메서드 주석의 "2026-09 조회" 표기(개발자센터 공식 문서, 응답 예시 포함).

## 실계정 검증 전 필요 조건 (1단계)

- 네이버/쿠팡 실 판매자 계정의 유효한 API 자격증명 (client_id/secret, vendor_id 등).
- 실 주문 데이터에 대한 발송 처리 승인(테스트 발송이 실제 구매자에게 알림/문자로
  나갈 수 있음 - 운영 담당자 승인 필요, 특히 outbox worker가 자동으로 실행하므로
  실계정 자격증명을 등록하는 순간부터 대기 중인 PENDING 명령이 있으면 곧바로
  실제 발송 요청이 나간다는 점을 운영 담당자가 인지해야 한다).
- `scheduler.jobs.outbox_dispatch_job`/`channel_status_sync_job` 자체를 실 계정으로
  실행해 본 적은 없다(MockTransport로만 검증) - 실 배포 전 스테이징 환경에서
  스케줄러 프로세스를 먼저 띄워 두 잡이 정상 동작하는지 확인이 필요하다.
- outbox의 "동일 (shipment_id, tracking_no) 재실행은 안전하다"는 가정(stale RUNNING
  회수 시 근거)은 채널이 같은 송장번호 재제출을 upsert로 처리한다는 전제다 - 실
  계정으로 중복 제출 시 채널이 오류를 내는지 확인이 필요하다.

## 실전송 활성화 체크리스트 (1·2단계 공통 - 아직 미충족, 재검토 없이 그대로 유지)

두 기능 플래그(`shipment_channel_submit_enabled`/`channel_status_sync_enabled`,
`claims_settlement_sync_enabled`)를 운영에서 켜기 전 반드시 확인해야 하는 항목.
1단계 완결 검토에서 식별된 뒤 2단계에서도 재검증하지 않고 그대로 이월했다.

- **재시도 분류 근거 검증**: `ShipmentDispatchService._classify_write_outcome()`의
  SAFE_RETRY/CONFIRMED_FAILED/UNKNOWN 판정 기준(어떤 HTTP상태/예외를 어느
  범주로 분류하는지)이 실제 네이버/쿠팡 API의 실패 응답 패턴과 일치하는지
  실계정으로 확인 필요 - MockTransport로는 "우리가 가정한 실패 패턴"만 검증됨.
- **UNKNOWN 해소 권한**: `POST /api/shipments/commands/{id}/resolve`로 UNKNOWN
  상태를 사람이 수동 해소하는 절차 - 실제로 이 권한을 누가 갖고 어떤 확인
  절차(채널 관리자센터에서 실제 접수 여부 확인 등)를 거쳐 해소할지 운영 프로세스
  합의 필요(코드는 상태 전이만 제공, 운영 절차는 별도 승인 대상).
- 위 두 항목 모두 아직 실계정 검증이 이뤄지지 않았으므로, 이번(2단계) 작업에서도
  다시 조사/변경하지 않고 이 체크리스트로만 유지한다.
- 2단계 신규: `claims_settlement_sync_enabled` 활성화 전에도 위와 동일하게, 쿠팡
  returnRequests/exchangeRequests/settlement-histories/revenue-history 각 API의
  실제 실패 응답(레이트리밋/일시 오류) 패턴을 실계정으로 먼저 확인해야 한다.

## 2단계 완료 요약 (구현·격리 검증, 운영 실전송 비활성)

- **브랜치/커밋**: `feature/commercial-erp-stage2-claims-settlements`
  (1단계 완료 커밋 `d25ea73`에서 분기 확인).
- **테스트**: 신규/확장 단위 테스트(클레임 dedup·미매칭 보존, 쿠팡 반품/교환/정산
  정규화, 정산 동기화 서비스, 클레임/정산 스케줄러 잡) + 통합 테스트
  (`test_api_orders.py`, 신규 `test_api_settlements.py`) 전부 통과, 전체 회귀
  867 passed. Ruff/MyPy 전체 통과.
- **마이그레이션**: 격리 SQLite 스크래치 DB에서 autogenerate 후 2단계 스키마
  변경만 남도록 무관한 드리프트(orders.assignee_id/confirmed_by,
  order_items.channel_product_id 미명명 FK, product_options 등 컬럼 길이 차이 -
  전부 2단계 이전부터 있던 기존 드리프트)를 수동으로 제외. SQLite는 제약 추가에
  batch mode가 필요해 `cancellations.order_item_id` FK만 `batch_alter_table`로
  처리. upgrade/downgrade 모두 격리 환경에서 확인.
- **기본 차단**: `claims_settlement_sync_enabled` 기본 False 확인
  (`tests/unit/test_claim_sync_job.py`, `tests/unit/test_settlement_sync_job.py`,
  `tests/integration/test_api_orders.py::TestSyncClaimsDisabledByDefault`,
  `tests/integration/test_api_settlements.py::TestSyncDisabledByDefault`).
