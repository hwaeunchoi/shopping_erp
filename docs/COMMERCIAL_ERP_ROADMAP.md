# 상용 ERP 확장 로드맵 (사방넷/플레이오토/셀메이트/샵링커 수준)

이 문서는 `shopping_erp`를 다채널 상용 ERP 수준으로 확장하기 위한 6단계 로드맵과,
각 단계의 완료 기준·채널별 지원 현황(Capability Matrix)을 정리한다.

- **1단계**(`feature/commercial-erp-stage1-shipment-sync`): 구현·격리 검증
  완료(pytest/Ruff/MyPy, 격리 PostgreSQL, 클린 워크트리 재검증). **운영 실전송은
  비활성**(`shipment_channel_submit_enabled`/`channel_status_sync_enabled` 모두 기본
  False) - 활성화 전 필요 조건은 "실전송 활성화 체크리스트" 참고.
- **2-A단계**(`feature/commercial-erp-stage2-claims-settlements`, 1단계 완료 커밋
  `d25ea73`에서 분기): 취소/반품/교환/정산 "수집·상태 갱신·금액 대사·조회 UI".
  **`072facb`는 2-A단계의 일부 구현**이다(전체 2단계 완료 아님) - 네이버
  클레임/정산은 공식 문서 확인이 막혀 있었고, 쿠팡 취소는 "기간 대량조회
  불가"로만 결론짓고 주문ID 단건 조회 가능성을 더 조사하지 않은 채 종료했다.
  이후 커밋(`67754b4ac7fb` 스키마 반영분)에서 쿠팡 취소의 "후보 주문 단건 조회"
  경로를 보완했다 - 아래 "2-A단계 보완 사항" 참고. **승인/거부/환불실행/
  반품완료처리/교환재발송 등 채널 상태를 실제로 바꾸는 조치는 2-A단계 범위에
  없음** - 아래 "2-B단계"로 분리. 운영 자동/수동 수집 모두
  `claims_settlement_sync_enabled`(기본 False)로 차단.

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

### 2-A단계 - 취소/반품/교환/정산 채널 연동 (일부 구현 - 진행 중)

- **범위**: 이미 존재하는 내부 취소/반품/교환/정산 모델을 각 채널의 공식
  조회 API와 연동해 **수집·상태 갱신·금액 대사·조회 UI**만 구현한다. 승인/거부/
  환불실행/반품완료처리/교환재발송 등 채널 상태를 실제로 바꾸는 조치는 범위
  밖이며 "2-B단계"로 분리한다(아래 참고).
- **의존성**: 1단계의 outbox/상태전이/SAVEPOINT 격리 패턴을 재사용. 기존
  `ClaimSyncService`/`services/exchange_return_service.py`를 확장(중복 모델
  생성 없음).
- **`072facb`에서 구현된 것(전체 완료 아님)**:
  - 쿠팡 반품(returnRequests v6 상태코드 순회)/교환(exchangeRequests v4)/
    정산 회차(settlement-histories v1)/정산 상세(revenue-history v1) - 기간
    기반 대량 수집.
  - 채널 claim ID 기준 dedup + 재수집 시 상태 갱신(과거 상태로 되돌리지 않음),
    claim ID가 없는 경우의 보수적 폴백, 아직 수집되지 않은 주문에 걸린 클레임은
    `ClaimUnmatched`에 보관 후 주문 수집 시 자동 승격.
  - 정산 회차/상세는 전부 `Decimal`로 계산하고, 회차-상세 매칭 실패/금액 불일치는
    `SettlementDiscrepancy`로 분리 기록(자동 추정으로 덮어쓰지 않음), 재수집 시
    중복 생성 방지.
- **`67754b4ac7fb`에서 보완된 것(2-A단계 후속 - 쿠팡 취소 재조사)**:
  - `072facb` 시점에는 "쿠팡 취소는 기간만으로 대량조회가 안 된다"에서 조사를
    멈췄으나, 재조사 결과 orderId+cancelType=CANCEL 조합의 **단건 조회**는
    공식 파라미터 표가 명시적으로 허용함을 확인했다(`integrations/malls/
    coupang_connector.py`의 `CANCEL_LOOKUP_WINDOW_DAYS` 주석 - 공식 문서
    파라미터 표 원문 인용 포함, 같은 채널의 별도 FAQ와는 "날짜range만으로 대량
    조회가 되는지"에서만 모순되고 이 좁은 기능 자체는 부정하지 않음도 함께
    기록). 이를 근거로 "후보 주문(배송 전 상태) 단건 조회 + 회전식 체크포인트
    + 요청 수 예산 제한"을 구현했다(`ClaimSyncService.
    sync_cancellations_by_candidate_orders`, `ClaimCollectionCursor`).
  - **한계(명시)**: ERP에 아직 수집되지 않은 주문의 취소, 그리고 후보 주문이
    CANCEL_LOOKUP_WINDOW_DAYS(31일)보다 오래전에 발생한 취소는 이 방식으로
    감지되지 않는다(services/claim_sync_service.py의 메서드 docstring 참고).
- **여전히 미구현/차단(2-A단계 잔여)**:
  - 네이버 취소/반품/교환: 상품주문 상세 조회(`POST /external/v1/pay-order/
    seller/product-orders/query`) 응답에 `currentClaim`/`beforeClaim`/
    `completedClaims`(claimId 포함) 필드가 존재함을 GitHub 공식 저장소 릴리즈
    노트로 확인했고, 변경상태 조회(`GET .../product-orders/last-changed-statuses`)
    로 클레임 관련 변경 후보를 저비용으로 찾을 수 있음도 확인했다 - 그러나
    claim 하위 필드의 정확한 JSON 경로/이름과 claimStatus enum 값은 공식
    레퍼런스 문서(apicenter.commerce.naver.com)에서만 확인 가능한데 이
    사이트는 WebFetch로 접근 불가(JS 렌더링 사이트로 추정)했다. 확인되지
    않은 필드 경로로 파싱 코드를 작성하는 것은 추측 구현이 되므로 보류했다 -
    **필요한 것**: apicenter.commerce.naver.com의 "상품주문 상세 내역 조회"
    API 레퍼런스 페이지 원문(또는 동등한 OpenAPI/Postman 스펙) 접근.
  - 네이버 정산: "일별 정산 내역 조회" API(정산예정일 기준 집계)가 존재함을
    GitHub discussion으로 간접 확인했으나, 정확한 엔드포인트 경로/응답
    필드/커머스API 앱에 정산 조회 권한(스코프)이 부여돼 있는지는 확인하지
    못했다(동일하게 apicenter 접근 차단). **필요한 것**: 위와 동일하게
    apicenter의 정산 API 레퍼런스 페이지 접근, 그리고 등록된 커머스API
    앱(App)에 정산 조회 권한이 부여돼 있는지 확인.
  - 위 두 항목은 이번 보완에서도 추측으로 구현하지 않고 capability False를
    유지했다(`supports_cancellation_sync`/`supports_return_sync`/
    `supports_exchange_sync`/`supports_settlement_sync`는 네이버 전부 False).
  - MockTransport 기반 단위/통합 테스트, 격리 PostgreSQL 마이그레이션 검증,
    클린 워크트리 재검증은 구현된 범위 안에서 완료(아래 "2-A단계 진행 요약"
    참고). 실계정 검증은 별도 승인 필요(아직 미실시).

#### 2-B단계 - 클레임/정산 상태 변경(승인/환불/처리) 액션 (미구현, 향후 분리 진행)

2-A단계에서 명시적으로 **구현하지 않은** 항목들 - 채널에 실제 쓰기 요청을 보내
상태를 바꾸는 조치이므로, 1단계의 outbox(멱등키+atomic lease+UNKNOWN 처리)
패턴과 동일한 안전장치를 갖춘 뒤 별도 단계로 진행해야 한다.

- 취소 승인/거부(채널로 실제 승인·거부 요청 전송)
- 반품 환불 실행(채널/PG 환불 API 호출)
- 반품 완료 처리(수거확인 등 채널 상태 갱신 쓰기)
- 교환 재발송 처리(교환 상품 재출고 확정 채널 쓰기)
- 위 각 항목은 1단계에서 확립한 "결과 불명(timeout/connection-drop/parse
  실패/외부성공-로컬커밋실패)은 절대 자동 재시도하지 않고 UNKNOWN/RECONCILIATION_REQUIRED로
  전환" 원칙과 DB-atomic claim/lease 동시성 제어를 그대로 적용해야 한다.

### 3단계 - 상품/재고 동기화 (구현 완료 - 네이버·쿠팡 MockTransport 계약 검증, 실계정 미검증)

- **실제 구현된 범위**: 내부 상품/옵션 → 채널 "쓰기" 4종을 outbox(`ExternalCommand`)
  기반으로 구현했다 - 재고 전송(`INVENTORY_UPDATE`)·판매상태 전송
  (`SALE_STATUS_UPDATE`)·단일 상품 등록(`PRODUCT_CREATE`)·옵션조합 상품 등록
  (`PRODUCT_OPTION_CREATE`), 그리고 상품정보(이름/판매가/설명) 수정
  (`PRODUCT_INFO_UPDATE`, **네이버만** - 쿠팡은 공식 계약 미확인으로
  `supports_product_info_update`가 base 기본값 False를 그대로 상속). 네이버
  상품 "수집"(`fetch_products`/`product_sync_job`)은 1단계 이전부터 운영 중이던
  기능을 그대로 재사용했다(이번 단계에서 변경 없음).
- **서비스/대량처리**: `services/product_sync_dispatch_service.py`(재고·판매상태·
  정보수정)/`product_publish_service.py`(단일 등록)/
  `product_option_publish_service.py`(옵션조합 등록) + 이 넷을 대량 접수·재처리로
  묶는 `services/product_bulk_service.py`(항목별 독립 커밋/롤백, UNKNOWN 대량
  재처리 차단, `/products-bulk` 화면).
- **검증**: MockTransport 기반 커넥터 계약 테스트 + 서비스 단위테스트 + 격리
  PostgreSQL 동시성 테스트(대상 잠금 경합, `acquire_target_lock`)로 확인했다 -
  **실제 네이버·쿠팡 판매자 계정으로는 검증하지 않았다**(아래 "실계정 검증 전
  필요 조건" 그대로 적용, 관련 기능 플래그
  `product_channel_sync_enabled`/`product_publish_enabled`/
  `product_info_update_enabled`/`product_option_publish_enabled` 모두 기본
  False 유지).
- **완료 기준(재확인)**: 재고 변경이 outbox를 통해 채널로 전파되고 채널 응답
  실패 시 `RETRY_WAIT`/`FAILED`/`UNKNOWN`으로 안전하게 분류되는 것까지 확인했다 -
  이 기준은 충족됐다(mock 계약 기준).

### 4단계 - ESM/11번가/카카오 채널 확장 (부분 구현 - capability 투명성 화면만 완료, 실연동 전부 보류)

- **실제 구현된 범위**: `GET /api/platforms/capability-matrix` + 설정 화면의
  "채널 연동 현황" 탭 - 채널별 활성상태·공식 계약 확인 여부·확인된 기능
  목록·마지막 성공/실패 시각을 있는 그대로 보여주는 **조회 전용** 기능이다.
  11번가는 이 화면에서 비활성 + "공식 계약 미확인"으로 표시되고 어떤 capability도
  True로 주장하지 않는다.
  API Credential 등록 화면의 플랫폼 목록도 이 matrix를 쓰도록 바꿔 11번가가
  선택은 가능해졌지만, 기존 "이 플랫폼은 아직 지원하지 않습니다" 가드가 그대로
  걸려 Credential 저장 자체는 여전히 막혀 있다(11번가용 필드 정의를 추측해
  만들지 않았다).
- **11번가/ESM/카카오쇼핑 실연동은 전혀 구현하지 않았다**: `EsmConnector`/
  `ElevenstConnector`/`KakaoShoppingConnector`는 여전히 `BaseMallConnector`의
  더미 구현(`fetch_orders`가 고정된 가짜 데이터를 반환)뿐이고, `supports_*`
  capability 플래그를 하나도 override하지 않아(전부 base 기본값 False 상속)
  주문 수집을 포함한 어떤 기능도 실제로 채널을 호출하지 않는다 - 호출을
  시도하면 `MarketplaceCapabilityUnsupportedError`로 명시적으로 거부된다.
  `scripts/init_db.py`의 `DEFAULT_PLATFORMS`도 이 세 채널을 `is_active=False`로
  시딩한다. 공식 판매자 API 문서가 확보되기 전까지는 이 상태를 유지한다.
- **완료 기준(원안 대비)**: "각 채널 공식 문서 기준 MockTransport 계약 테스트"는
  세 채널 모두 아직 시작하지 못했다 - 공식 문서 확보가 선행 조건이며 이번
  범위에서 임의로 계약을 추정해 구현하지 않았다.

### 5-A단계 - 출고·택배 처리 통합 (구현 완료 - 창고 내부 워크플로우 한정)

- **범위**: 주문을 실제 창고 업무 순서(출고대기 -> 피킹 -> 검수 -> 포장완료 ->
  송장등록 -> 채널전송 대기 -> 전송완료)로 처리하는 출고 배치 기능. **택배사
  API를 통한 예약/라벨 출력/실시간 배송추적 조회는 이 단계 범위가 아니다** -
  공식 계약이 확인되지 않아 추측 구현하지 않았다(아래 "미구현/차단" 참고).
- **새 도메인**(`models/fulfillment.py`): `FulfillmentBatch`(출고 배치) /
  `FulfillmentBatchItem`(배치에 속한 주문라인 1건 - 항상 정확히 하나의
  `OrderItem`에 대응) / `FulfillmentBatchItemHistory`(작업 이력). 상태값은
  `READY -> PICKING -> PICKED -> VERIFYING -> VERIFIED -> PACKED ->
  SUBMIT_PENDING -> SUBMITTED`(+ `BLOCKED`/`CANCELLED`)이며
  `services/fulfillment_state_machine.py`가 서버 측 전이 검증을 전담한다 -
  검수 전 포장완료, 취소 주문의 신규 출고, 이미 SUBMITTED인 항목의 취소 등은
  전이표 자체에서 차단된다.
- **부분출고/분할배송 데이터 모델**: 기존 `ShipmentItem.order_item_id` 구조를
  그대로 재사용한다 - 한 주문라인을 여러 `FulfillmentBatchItem`/`Shipment`로
  나눠 담을 수 있고, 누적 출고수량은 항상 `ShipmentItem` 실측 수량 기준으로
  계산한다(`FulfillmentBatchItemRepository.sum_committed_quantity_by_order_items`
  등). 라인의 일부만 출고돼도 주문 전체가 즉시 배송완료로 바뀌지 않으며, 기존
  `ShipmentDispatchService._is_order_fully_dispatched()`가 모든 라인이 채널
  전송까지 완료된 뒤에만 주문 상태를 전환한다(이 로직은 1단계에서 이미 구현된
  것을 그대로 재사용했고 이번 단계에서 수정하지 않았다).
- **재고 차감 시점과 롤백 경계**: 검수완료가 아니라 **포장완료
  (`FulfillmentService.pack_and_register_tracking`) 시점에 검수 확정수량만큼
  정확히 한 번** 차감한다(`FulfillmentBatchItem.inventory_deducted_at`으로
  멱등 표시). 이는 기존 단건 배송 흐름(`ShipmentService.change_status()` ->
  `apply_status_change()`가 운영자의 수동 `warehouse_id` 지정 시 주문 전체
  수량을 차감하는 방식)과는 다른 별도의 추가 경로다 - 기존 정책을 변경하지
  않고 배치 흐름 전용으로 새로 추가했다. 채널 송장 전송이 이후에 실패해도
  이미 물리적으로 창고를 떠난 것으로 간주해 **재고 차감은 되돌리지 않는다** -
  되돌리려면 운영자가 `cancel_item()`을 명시적으로 호출해야 하며, 이 경우
  같은 트랜잭션에서 배치항목 상태 전환과 재고 복원이 함께 처리된다.
- **동시 초과출고/중복차감 방지**: (1) 같은 주문라인에 대한 배치 생성 경합은
  `ExternalCommandRepository.acquire_target_lock()`(PostgreSQL advisory lock,
  `target_type="FULFILLMENT_ORDER_ITEM"`)로 직렬화해 잔여수량 초과 배정을
  막는다. (2) 같은 배치항목의 상태 전이는
  `FulfillmentBatchItemRepository.claim_transition()`의 원자적 조건부
  UPDATE(`WHERE id=? AND status=?`)로 정확히 하나만 성공한다(낙관적 동시성
  체크도 겸함 - API가 요청받은 `expected_status`를 그대로 전달). (3) 같은
  (창고,옵션) 재고 행에 대한 동시 포장 확정은 별도 advisory lock
  (`target_type="FULFILLMENT_INVENTORY"`)으로 감싸는데, **잠금을 기다리는
  동안 다른 트랜잭션이 먼저 포장을 커밋했을 가능성**이 있어 잠금 획득 직후
  배치항목을 다시 조회(refresh)해 여전히 `VERIFIED` 상태인지 재확인한다 -
  아니면 재고를 다시 차감하지 않고 `ALREADY_PROCESSED`로 결과만 남긴다. 이
  재확인 로직은 최초 구현에서 누락돼 있었고(잠금만 걸고 상태 재확인 없이
  바로 차감), 격리 PostgreSQL 두 커넥션 동시성 테스트
  (`tests/integration/test_fulfillment_concurrency_pg.py::TestConcurrentPackSameBatchItem`)로
  실제 이중차감이 재현되는 것을 확인한 뒤 발견·수정했다(단순 애플리케이션
  SELECT 검사만으로는 이 경합을 막을 수 없다는 것이 실제로 증명된 사례).
  서로 다른 주문라인/서로 다른 (창고,옵션) 조합은 서로 다른 잠금 키를 쓰므로
  불필요하게 직렬화되지 않는다.
- **송장 중복 방지**: 택배사 코드는 창고 기록용 내부 목록
  (`services/fulfillment_carrier.py`의 `INTERNAL_CARRIERS` - CJ대한통운/
  한진택배/롯데택배/로젠택배/우체국택배/기타)으로 관리하고, 채널 실제 전송
  시점에는 기존 `integrations/malls/carrier_codes.py`(공식 확인된
  CJ대한통운만 매핑, 나머지는 `UnknownCarrierError`)를 그대로 통과해야 한다 -
  두 목록을 섞지 않는다. 송장번호는 공백/하이픈 제거 후 문자열로 정규화하고
  (앞자리 0 보존), 같은 택배사+정규화된 송장번호 조합이 이미 등록돼 있으면
  거부한다(의도된 분할배송은 각기 다른 배치항목이 같은 송장 1건에 묶이는
  것이므로 이 검사에 걸리지 않는다).
- **채널 송장 outbox 접수와 UNKNOWN 처리**: API는 외부 채널을 직접 호출하지
  않는다 - `submit_to_channel()`이 기존 `ShipmentDispatchService.enqueue()`를
  그대로 호출해 PENDING `ExternalCommand`만 만들고 202를 반환하며, 실제 전송은
  기존 `outbox_dispatch_job`이 수행한다. 채널 응답이 불명확한 경우
  `ExternalCommand`는 기존 1단계 정책 그대로 `UNKNOWN`으로 표시되고 **자동
  재처리되지 않는다** - 화면(진행상태 표)에 "확인 필요" 안내만 노출하고,
  운영자가 기존 `POST /api/shipments/commands/{id}/resolve`로 직접 확인 후
  해소해야 한다(`retry_failed_items()`도 `command.status == "UNKNOWN"`이면
  `BLOCKED`만 반환하고 재시도하지 않는다). `FAILED`로 확정된 명령만 선택
  재처리할 수 있고, 이미 성공(SUCCESS)한 라인은 같은 배치를 다시 처리해도
  재전송 대상에서 제외된다(기존 `ExternalCommandLineResultRepository`의
  라인별 성공 기록 재사용).
- **내부 물리 출고 상태와 외부 채널 송장 전송 상태는 분리돼 있다**:
  `FulfillmentBatchItem.status`(피킹/검수/포장 등 창고 내부 작업)와
  `ExternalCommand.status`(채널 전송 성공/실패/UNKNOWN)는 서로 다른 테이블의
  서로 다른 상태값이며 하나로 합치지 않았다 - 포장완료는 재고가 실제로
  줄었다는 사실만 의미하고, 채널이 그 송장을 실제로 접수했는지는
  `ExternalCommand.status == "SUCCESS"`를 확인해야만 알 수 있다. 화면의
  진행상태 표는 이 둘을 나란히 보여주되 절대 하나로 합쳐 표시하지 않는다.
- **UI**(`frontend/src/pages/FulfillmentPage.tsx`, `/fulfillment`, 기존
  `SHIPMENT_VIEW` 권한 재사용): 출고 배치 생성(검색/선택/수량 입력/서버
  재검증 전 클라이언트 사전 안내), 배치 목록/상세, 피킹/검수/포장 처리,
  택배사 선택+송장번호 일괄입력, 채널 전송 접수, 항목별
  접수/실패/UNKNOWN/이미처리됨 결과 표시, FAILED만 선택 재처리, 작업 이력
  조회. 기존 단건 배송 화면(`ShipmentsPage.tsx`, `/shipments`)은 변경하지
  않았고 서로 다른 API를 쓴다(겹치는 도메인 로직 없음 - 단건 화면은 이미
  운영 중인 `/api/shipments`를, 배치 화면은 이번에 추가한 `/api/fulfillment/*`를
  각각 사용). 수취인 전화번호/주소는 목록 어디에도 표시하지 않는다.
- **미구현/차단(5-A단계 범위 밖)**:
  - 택배사 API 연동(예약 접수, 라벨 출력, 실시간 배송추적 조회) - 공식 계약
    미확인. 이번 단계의 "택배 통합"은 택배사 코드/송장번호를 안전하게
    관리하고 기존 outbox로 네이버/쿠팡에 전송 접수하는 단계까지다.
  - 네이버/쿠팡으로의 **실제** 송장 전송은 기존 `shipment_channel_submit_enabled`
    기능 플래그(기본 False, 이번 단계에서 값을 바꾸지 않음)로 여전히
    차단돼 있고, 활성화 여부와 별개로 위 "실계정 검증 전 필요 조건"/"실전송
    활성화 체크리스트"의 운영 승인 절차를 그대로 따라야 한다. 창고 내부
    워크플로우(배치 생성/피킹/검수/포장) 자체는 외부 호출이 없어 별도 플래그가
    없다.
  - 11번가/ESM/카카오쇼핑: 4단계와 동일하게 공식 계약이 확인되지 않아 이번
    단계에서도 미지원(해당 채널의 커넥터는 여전히 `MarketplaceCapabilityUnsupportedError`).
- **검증**: 신규 단위 테스트(상태머신/택배사·송장 검증/서비스) +
  API 통합 테스트(`tests/integration/test_api_fulfillment.py`, 권한/생성/
  전체출고/부분출고/상태충돌 409/실패 케이스) + 격리 PostgreSQL 2건 동시성
  테스트(`tests/integration/test_fulfillment_concurrency_pg.py` - 동일
  주문라인 동시 배치생성 초과배정 방지, 동일 배치항목 동시 포장 중복차감
  방지) 전부 통과. 신규 Alembic 마이그레이션은 SQLite/격리 PostgreSQL 양쪽에서
  upgrade/downgrade/upgrade 및 기존 샘플 데이터 보존 확인. 전체 회귀
  (1261 tests) / Ruff / MyPy / 프론트 `tsc -b`+`oxlint`+`vite build` 전부 통과.
  실계정 검증은 이번 단계에서도 수행하지 않았다(위 채널 전송 플래그가 그대로
  꺼져 있으므로 실제 전송 자체가 발생하지 않는다).

### 5-B단계 - 문의·CS 통합 관리 (구현 완료 - 내부 CS 워크스페이스 + 쿠팡 콜센터 문의 조회 한정)

- **범위**: CS(고객문의) 케이스를 생성·배정·상태전이·메모·답변초안·이력·대량처리·
  대시보드까지 다루는 통합 업무공간. **외부 채널로의 실제 답변 전송은 이번
  단계에 없다** - 공식 계약상 안전하게 확정할 수 없는 필드가 있어(아래 "채널
  문의 연동 조사 결과" 참고) 답변 초안 저장까지만 지원하고 UNSUPPORTED로
  표시한다.
- **내부 CS만 구현된 부분**(모든 채널 공통): 케이스 생성/조회/검색/필터, 담당자
  배정(동시성 보장), 상태전이(OPEN/IN_PROGRESS/WAITING_CUSTOMER/
  WAITING_CHANNEL/RESOLVED/CLOSED), 내부 메모, 답변 초안 저장, 처리기한·지연
  표시, 태그, 작업 이력, 대량 배정/대량 상태변경(CLOSED 대상 제외 - 건별
  확인 필요), 중복 문의 감지(같은 주문+문의유형, 24시간 이내 미종결 건 경고),
  대시보드 집계(상태별/미배정/지연 건수), 첨부파일 메타데이터(재사용, 아래
  참고).
- **채널 조회까지 구현된 부분**: 쿠팡 콜센터 문의(callCenterInquiries)만
  - 공식 문서(developers.coupang.com/hc/en-us/articles/
    360033645354-Query-of-Coupang-Contact-Center-Inquiries, 2026-09 조회)의
    요청 파라미터(vendorId/partnerCounselingStatus 4종 필수 순회/
    inquiryStartAt·inquiryEndAt 최대 7일/pageNum·pageSize)와 응답 스키마
    (inquiryId/content/inquiryAt/inquiryStatus/csPartnerCounselingStatus/
    buyerPhone/orderId/pagination)를 전부 확인하고 그대로 구현했다
    (`integrations/malls/coupang_connector.py`의 `fetch_inquiries()`,
    `supports_inquiry_sync=True`).
  - `services/cs_channel_sync_service.py`가 (platform_id,
    external_inquiry_id) 유니크 제약으로 멱등 저장하고, 재수집 시 담당자/
    우선순위/상태/태그/내부메모/답변초안은 절대 덮어쓰지 않으며(원본
    상태(external_raw_status)와 마지막 고객 메시지 시각만 갱신), 항목 하나
    실패해도 SAVEPOINT로 흡수해 나머지 항목·이전 성공은 유지한다(전체 성공
    위장 없음).
  - 자동 실행은 `scheduler/jobs/cs_inquiry_sync_job.py`(15분 주기, 기본 OFF),
    수동 실행은 `POST /api/cs-cases/sync` - 둘 다
    `settings.cs_inquiry_sync_enabled`(기본 False)가 꺼져 있으면 커넥터를
    만들지도 외부 요청을 보내지도 않는다(세션도 열지 않는 자동 잡과 달리 수동
    API는 플랫폼 조회 정도는 하지만 채널 HTTP 호출은 0건).
- **채널 조회는 확인됐지만 이번 단계에서 구현하지 않은 것**: 쿠팡 상품별
  문의(onlineInquiries) - GET 조회 계약(엔드포인트/파라미터/응답 스키마)은
  공식 문서로 확인됐으나, 콜센터 문의와 스키마가 달라 한 라운드에 두 종류를
  같이 다루지 않기 위해 범위 관리 차원에서 다음 단계로 미뤘다(계약 미확인이
  이유가 아니다 - 명확히 구분해 기록한다).
- **실제 답변 전송을 구현하지 않은 이유(명시)**: 쿠팡 콜센터 답변 API
  (`POST .../callCenterInquiries/{id}/replies`)는 필드 이름(vendorId/
  inquiryId/content/replyBy/parentAnswerId) 존재는 공식 문서로 확인되지만,
  `parentAnswerId`가 "신규 답변(채널 이관이 아닌 일반적인 경우)"에 어떤 값을
  가져야 하는지는 확보한 문서 범위에서 확정할 수 없었다. 실제 판매자 계정에
  잘못된 값으로 답변을 시도하면 고객에게 나가는 실제 커뮤니케이션에 영향을
  줄 수 있어, 필드 이름 확인만으로는 부족하다고 보고(요구사항의 "공식 문서로
  확인된 기능만 구현" 원칙을 필드 존재가 아니라 실사용 의미 확정 수준으로
  적용) 이번 단계에서는 조회만 구현했다. 네이버는 문의 목록 조회 API
  (`GET /v1/pay-user/inquiries`)의 존재와 요청 파라미터(startSearchDate/
  endSearchDate/page/size/answered)는 GitHub 공식 기술지원 저장소
  (commerce-api-naver/commerce-api) 메인테이너 답변으로 확인했으나, 정확한
  응답 스키마 필드명은 apicenter.commerce.naver.com이 이 환경에서 접근 불가라
  확인하지 못해(1단계·2-A단계와 동일한 한계) 커넥터 구현 자체가 없다
  (`supports_inquiry_sync`는 `NaverSmartstoreConnector`에서 오버라이드하지
  않아 base 기본값 False를 그대로 상속).
- **11번가/ESM/카카오쇼핑**: 4단계와 동일하게 공식 셀러 계약이 확인되지 않아
  미지원(`MarketplaceCapabilityUnsupportedError`).
- **데이터 모델**(`models/cs_case.py`): `CsCase`/`CsCaseHistory` 신규 테이블.
  클레임(교환/반품/취소) 연결은 세 테이블에 FK를 각각 두지 않고 기존
  `Memo`/`Attachment`와 동일한 다형성(claim_type/claim_id) 패턴을 재사용했다.
  내부 메모/첨부파일 메타데이터는 새 테이블을 만들지 않고 기존
  `models.extra.Memo`/`Attachment`를 `target_type="CS_CASE"`로 재사용한다 -
  `Attachment`는 이전까지 스키마만 있고 어떤 라우터도 쓰지 않던 테이블이라
  이번 단계에서 `AttachmentRepository`를 처음 추가했다. **실제 파일 업로드
  저장소는 이 코드베이스 어디에도 없으므로 새로 만들지 않았다** - 첨부파일은
  메타데이터 조회만 가능하고, 업로드는 미지원으로 남긴다(요구사항의 "기존
  파일 저장 정책이 있을 때만 재사용, 없으면 안전한 메타데이터 모델까지만"
  원칙 그대로).
- **상태전이**(`services/cs_state_machine.py`): 최소 6개 상태(OPEN/
  IN_PROGRESS/WAITING_CUSTOMER/WAITING_CHANNEL/RESOLVED/CLOSED). CLOSED는
  RESOLVED에서만 도달 가능하고, 생성 시 직접 지정 불가(API 스키마 자체에
  status 필드가 없음), 재오픈(CLOSED->OPEN)은 상태표 자체는 허용하지만
  `change_status()`(일반 경로)가 아니라 `reopen_case()` 전용 메서드로만
  실행되도록 서비스 계층에서 이중으로 막는다(상태표만 믿지 않음). IN_PROGRESS
  전환은 담당자가 먼저 배정돼 있어야 한다.
- **동시 초과배정 방지**: 담당자 배정은 상태(status)가 아니라 담당자
  자체(assignee_id)를 낙관적 동시성 기준으로 쓴다
  (`CsCaseRepository.claim_assignee()`, `UPDATE ... WHERE assignee_id = 기대값`)
  - 배정은 status를 바꾸지 않으므로 status 기준 가드로는 "두 사람이 같은
    미배정 건에 서로 다른 담당자를 동시에 배정"하는 경합을 잡지 못한다는
    것을 격리 PostgreSQL 두 커넥션 테스트로 먼저 확인한 뒤 이 방식으로
    설계했다.
- **송장 중복 방지에 대응하는 "동일 채널 문의 중복 생성 방지"**: `cs_cases`의
  `(platform_id, external_inquiry_id)` 유니크 제약이 재수집 경합에서도 실제로
  하나만 통과시키는지 격리 PostgreSQL 두 커넥션 테스트로 확인했다 - 유니크
  제약 위반은 `IntegrityError`로 발생하고 `CsChannelSyncService.
  sync_inquiries()`의 항목별 SAVEPOINT가 이를 흡수해 그 항목만 실패로
  기록하고 전체 동기화는 계속 진행한다.
- **UNKNOWN 처리**: 이번 단계는 외부 답변 전송 자체가 없으므로 CS 케이스
  전용 UNKNOWN 명령이 존재하지 않는다 - 화면에는 "실제 채널 답변 전송
  미지원(UNSUPPORTED)" 안내만 표시한다. 기존 배송(Shipment) outbox의
  UNKNOWN 정책(자동 재전송 금지, 운영자 수동 해소)은 이번 단계에서 전혀
  건드리지 않았다.
- **개인정보**: 목록/상세 모두 이름은 첫 글자만(`services/pii_mask.py`
  `mask_name`), 전화번호는 마지막 4자리만(`mask_phone`) 노출한다. 전체
  전화번호/주소는 `CS_PII_DETAIL` 권한이 있는 사용자에게만 API 응답에
  추가로 포함된다(`api/routers/cs_cases.py`의 `_has_permission` 검사 -
  권한이 없으면 항상 `null`). 문의 본문/내부 메모/답변 내용 자체는 로그에
  출력하지 않는다(`CsCaseHistory.note`는 상태 코드 등 안전한 요약만 담고
  본문을 복사하지 않는다).
- **권한**(`scripts/init_db.py` `DEFAULT_PERMISSIONS`에 6종 신규 추가):
  `CS_VIEW`(조회) / `CS_MANAGE`(생성·수정·메모·답변초안) / `CS_ASSIGN`(담당자
  배정) / `CS_CLOSE`(종결·재오픈) / `CS_REPLY_SUBMIT`(외부 답변 접수 - 실제
  구현이 없어 아직 어디서도 검사되지 않는다, 향후 채널 답변 계약이 확인되면
  사용할 자리만 예약) / `CS_PII_DETAIL`(개인정보 상세 조회 - 의도적으로
  `*_VIEW` 접미사를 피해 명명했다, `Viewer` 역할에 자동 부여되는 기존
  네이밍 관례(`code.endswith("_VIEW")`)에 실수로 걸리지 않게 하기 위함).
  종결(POST .../close)과 재오픈(POST .../reopen)은 일반 상태변경
  (POST .../status)과 다른 권한(`CS_CLOSE`)을 검사한다 - 일반 상태변경
  엔드포인트는 `new_status="CLOSED"` 요청 자체를 400으로 거부해 종결 전용
  경로로만 가도록 강제한다.
- **UI**(`frontend/src/pages/CsCasesPage.tsx`, `/cs-cases`, 신규 권한
  `CS_VIEW` 사이드바 게이팅): 대시보드 집계 타일, 상태/채널/문의유형/우선순위/
  담당자/기한 필터 + 미배정·지연 빠른 필터 + 검색, 목록(체크박스 다중선택) +
  상세 패널, 연결된 주문 링크, 담당자 배정/상태변경/종결/재오픈, 내부메모와
  답변초안을 화면에서도 완전히 분리된 영역으로 표시, 대량 배정/대량 상태변경,
  채널 문의 수동 동기화 트리거(연결된 케이스에서만), 개인정보 상세 권한 안내
  문구, 처리중 버튼 비활성화로 중복 클릭 방지. 375/768/1280px 확인 완료 -
  확인 중 `.bulk-bar` 안의 이름없는 래퍼 `<div>`가 `min-width:0`이 없어
  다중 ID 입력칸이 있는 확인 패널에서 페이지 폭을 밀어내는 사전 버그를
  발견해 `frontend/src/index.css`에 `.bulk-bar > div { min-width: 0; }`
  규칙을 추가했다 - 이 규칙은 CS 화면 전용이 아니라 같은 구조를 쓰는 기존
  확인 패널(상품 대량처리/출고관리)에도 적용돼 동일한 잠재 결함을 함께
  막는다.
- **검증**: 신규 단위 테스트(상태머신 21개, 서비스 38개, 채널 동기화
  서비스 11개, PII 마스킹 8개, 쿠팡 문의 커넥터 5개) + API 통합 테스트
  (`tests/integration/test_api_cs_cases.py` 18개, 권한/생성·중복감지/PII
  마스킹/전체 생명주기/대량처리/대시보드/동기화) + 격리 PostgreSQL 2건
  동시성 테스트(`tests/integration/test_cs_case_concurrency_pg.py` - 동일
  케이스 동시 담당자배정 정확히 하나만 성공, 동일 외부문의ID 동시 최초수집
  시 정확히 하나만 케이스 생성) 전부 통과. 신규 Alembic 마이그레이션은
  SQLite/격리 PostgreSQL 양쪽에서 upgrade/downgrade/upgrade, 유니크 제약
  (NULL 다중 허용 포함) 실제 동작, 기존 샘플 데이터 보존 확인. 실계정
  검증은 수행하지 않았다(채널 조회 자체는 기능 플래그로 막혀 있고, 답변
  전송은 구현 자체가 없다).

### 5-C단계(잔여) - CS-주문 자동 연결 고도화/택배사 실시간 연동

- **범위**: 5-B단계가 다루지 않은 나머지 - 채널 문의를 수집 시점에 주문라인
  단위까지 자동 매칭하는 고도화(현재는 orderId 매칭까지만, order_item_id는
  수기 연결), 쿠팡 상품별 문의(onlineInquiries) 조회, 네이버 문의 연동
  (응답 스키마 확인 필요), 실제 채널 답변 전송(`parentAnswerId` 등 필드
  의미 확정 필요), 택배사 API를 통한 실시간 배송추적, 물류센터 연동(WMS
  트리거) - 5-A/5-B단계에서 명시적으로 제외한 항목.
- **의존성**: 1단계의 `carrier_codes.py` 정규화 테이블 확장 필요(현재
  `CJ_LOGISTICS`만 등록, 나머지는 `UnknownCarrierError`로 명시적 거부 중).
  택배사 추가 시 반드시 공식 문서 교차 확인 후 등록. CS 답변 전송은
  apicenter.commerce.naver.com 접근 확보 또는 쿠팡 `parentAnswerId` 의미
  확인이 선행돼야 한다.
- **완료 기준**: 택배사 실시간 조회 API 연동, 검증된 채널 답변 전송(outbox
  기반), CS 문의-주문라인 자동 매칭 화면.

### 6단계 - 통합 운영 대시보드·실패 재처리·통계 (구현 완료 - 조회·안전 재처리 한정)

- **범위**: 채널별 연동 상태/주문 수집/상품·재고 동기화/송장 전송/클레임·정산
  수집/출고 진행/CS 처리/scheduler 잡 상태를 한 화면(`/operations`)에서 보고,
  실패·재시도대기·UNKNOWN 건을 한 목록(통합 실패 작업함)에서 조회·안전하게
  재처리한다. 기존 화면(출고관리/상품 대량처리/CS 관리/설정)은 그대로 두고
  이 화면은 요약 + 진입점 역할만 한다 - 어떤 기존 기능도 재설계하지 않았다.
- **새 집계 테이블을 만들지 않았다**: 전부 기존 테이블(`ExternalCommand`/
  `IntegrationStatus`/`TaskExecutionHistory`/`OrderStatusConflict`/`CsCase`/
  `FulfillmentBatchItem`/`Settlement`) 조회·`GROUP BY`로 계산한다 -
  `services/operations_dashboard_service.py` 참고. 스키마 변경/마이그레이션
  없음.
- **명령 단위 이력이 있는 도메인 vs 없는 도메인**: 송장 전송/상품 등록/옵션조합
  등록/재고 전송/판매상태 전송/상품정보 수정(`ExternalCommand` 6종)만 매
  시도가 명령 행으로 남아 시도횟수·성공·실패·재시도대기·UNKNOWN을 명령
  단위로 정확히 집계할 수 있다 - "최근 24시간/7일 명령 성공률"은 이 6종만
  대상이다. 주문 수집/클레임 수집/정산 수집/채널 상태 재조회/CS 문의 수집은
  읽기 전용 배치라 명령 단위 이력이 없고, 대신 플랫폼당 하나의 스냅샷
  (`IntegrationStatus`)만 있다 - 이번 단계에서 `claim_sync_job`/
  `settlement_sync_job`/`channel_status_sync_job`/`cs_inquiry_sync_job` 4개
  잡에 `IntegrationStatus` 기록을 추가해(`order_collect_job`/`ad_collect_job`/
  `product_sync_job`은 이미 기록 중이었다) 7개 integration_type(MALL/AD/
  MALL_PRODUCT/CLAIM/SETTLEMENT/ORDER_STATUS_SYNC/CS_INQUIRY) 전체에서
  "플랫폼별 마지막 성공·실패 시각"을 볼 수 있게 했다 - 그 잡들의 수집 로직
  자체(중복방지/미매칭 보존/SAVEPOINT 격리 등)는 전혀 바꾸지 않았다.
  "성공/부분성공/실패"의 "부분성공"은 CLAIM(취소/반품/교환 3종 중 일부만
  성공)과 SETTLEMENT(회차 요약/상세 중 일부만 성공)에서만 실제로 관측되고,
  ORDER_STATUS_SYNC/CS_INQUIRY 중 채널상태 재조회는 성공/실패 이분법이다
  (한 플랫폼 처리 중 예외가 나면 그 플랫폼의 나머지 주문 전체를 건너뛰는
  구조라 부분성공을 세분화할 근거가 없다 - 억지로 만들어내지 않았다).
- **성공률 계산 기준**: `[since, now)` 구간에 **생성**(`created_at`)된 명령 중
  SUCCESS/FAILED로 **확정**된 것만 분자·분모에 넣는다 - PENDING/RUNNING/
  RETRY_WAIT/UNKNOWN/CANCELLED는 둘 다에서 제외한다(아직 처리 중이거나
  최종 결과가 아님). 분모(성공+실패)가 0이면 0%가 아니라 `null`(화면
  "N/A")을 반환한다. `completed_at`은 FAILED/RETRY_WAIT 경로에서 기록되지
  않아(SUCCESS 확정 시에만 채워짐) 창구 경계 판정 기준으로 쓸 수 없어
  `created_at`을 썼다 - `services/operations_dashboard_service.py` 모듈
  docstring 및 `success_rate_window()` 참고.
- **시간 기준**: 이 앱은 사용자별 timezone 설정이 없다(`users.theme_preference`는
  있어도 timezone 컬럼은 없다) - 임의로 새 설정 화면을 만들지 않고, API는
  항상 UTC 기준으로만 계산·응답한다(`utc_day_bounds()`). "오늘"은 UTC
  캘린더일(자정~다음 자정, KST는 UTC+9라 KST 자정과 다르다 - 화면에 "UTC
  기준"으로 명시), "최근 24시간/7일"은 now 기준 rolling window다. "오늘
  수집된 주문"은 채널 주문일자(`order_date`)가 아니라 우리 DB 적재 시각
  (`created_at`) 기준이다 - 늦게 수집된 주문은 채널 주문일자가 오늘이 아닐
  수 있고 그 반대도 마찬가지라, "수집"의 의미에는 적재 시각이 맞다.
  `end_date` 필터가 미래 시각으로 들어오면 서버가 현재 시각으로 자른다.
- **통합 실패 작업함**: `GET /api/operations/failures`가 `ExternalCommand`
  6종만 대상으로(향후 추가될 무관한 command_type이 섞이지 않게) `status`를
  지정하지 않으면 FAILED/RETRY_WAIT/UNKNOWN/RUNNING만(정상 대기/성공/취소는
  "실패"가 아니므로 기본 목록에서 제외) `limit`/`offset` 페이지네이션과
  `id desc` 안정 정렬로 반환한다(limit 상한 200 - 무제한 전체 조회 없음).
  통계(summary/timeseries)와 목록(failures)은 분리된 API라 자동 새로고침이
  실패 행 전체를 다시 가져오지 않는다. 행에는 기능유형/채널/대상(내부 ID)/
  상태/시도횟수/마지막 시도/다음 재시도/안전한 오류코드/상세화면 링크만
  담는다 - 전화번호·주소·문의본문·답변초안·Authorization·자격증명·DB
  연결정보·stack trace는 `ExternalCommand`에 애초에 저장되지 않는 값들이라
  이 라우터의 어떤 응답에도 나타나지 않는다(모델 설계 자체가 안전한 요약만
  저장 - `models/integration_sync.py` 참고).
- **재처리 정책(기능별 상태로 다르게)**: FAILED만 `POST
  /api/operations/failures/bulk-retry`로 선택 재처리할 수 있다 - 새 검증
  로직을 만들지 않고 command_type별 기존 서비스(`ShipmentDispatchService`/
  `ProductPublishService`/`ProductOptionPublishService`/
  `ProductSyncDispatchService`)의 `retry_failed_command()`를 그대로 호출하는
  라우팅 계층(`services/operations_retry_service.py`)만 새로 만들었다(검증을
  우회하는 범용 DB 상태변경 없음). RETRY_WAIT은 예약된 `next_retry_at`만
  표시하고 이 API로 조기 재시도할 수 없다(이번 단계 범위 밖 - 필요해지면
  기존 outbox worker의 claim 경로를 통해서만 허용해야 한다). UNKNOWN은 이
  API로 절대 재처리하지 않는다(outcome=`UNKNOWN_REQUIRES_RESOLUTION`) -
  `POST /api/operations/failures/{id}/resolve-unknown`으로 운영자가 세 가지
  해소값(CONFIRMED_SUCCESS/CONFIRMED_NOT_SENT/CONFIRMED_FAILED) 중 하나를
  골라야만 벗어날 수 있고, 5자 이상의 확인 근거(evidence_note)를 반드시
  입력해야 하며 이 근거는 `AuditLog`(command=`operations.resolve_unknown`)에
  그대로 남는다(각 도메인 서비스 자체의 감사로그는 상태 전이만 기록하므로,
  "왜 그렇게 판단했는가"는 이 서비스가 별도로 남긴다). RUNNING(정상이든
  stale이든)은 이 API로 재처리하지 않는다 - stale 회수는 기존
  `recover_stale_running()`(각 도메인 서비스, outbox worker가 매 실행 시작
  시 호출)만의 몫이고, 대시보드는 읽기 전용으로 stale 여부만 표시할 뿐 회수
  자체를 트리거하지 않는다.
- **대량 재처리 트랜잭션·멱등 정책**: 항목마다 진짜
  `commit()`/`rollback()`을 쓴다(SAVEPOINT 아님) - 뒤 항목의 rollback이 앞서
  커밋된 항목의 결과를 지우지 않는다(이 절 상단에 있던 "알려진 한계" 그대로
  반영 - `ShipmentDispatchService.submit_many()`의 SAVEPOINT 방식은 쓰지
  않았다). 이미 처리 로직이 다루는 예상된 실패(`ValueError` - 대상 없음,
  상태가 이미 바뀜 등)는 그 항목만 실패로 기록하고 배치를 계속 진행한다.
  그 외 예외(DB 오류 등 세션 상태를 신뢰할 수 없는 경우)는 배치를 즉시
  중단하고 아직 시도하지 않은 나머지 항목을 전부
  `FAILED_TO_ENQUEUE(ABORTED_DUE_TO_PRIOR_ERROR)`로 명시적으로 표시한다 -
  부분성공을 전체성공으로 위장하지 않는다. 같은 command_id를 중복 선택해도
  한 번만 처리한다(요청 내 de-dup). 이미 PENDING으로 바뀐 명령을 다시
  재처리 요청하면(중복 클릭/경합) 두 번째 시도는 `NOT_RETRYABLE
  (CURRENT_STATUS_PENDING)`로 안전하게 거부된다(재처리 자체가 FAILED
  상태만 대상으로 하는 조건부 검사이기 때문 - 별도 원자적 UPDATE...WHERE는
  없지만, 두 번째 호출의 사전 상태 확인이 이미 막는다). 한 번에 최대
  50건(`MAX_BULK_RETRY_ITEMS`), 선택 항목이 없거나 초과하면 400. 지원하지
  않는 command_type은 `NOT_RETRYABLE(UNSUPPORTED_COMMAND_TYPE)`로 명시적으로
  막는다. 작업 이력은 성공 시 `AuditLog`(command=`operations.bulk_retry`)에
  수행자(`changed_by`)와 함께 남는다.
- **권한**(`scripts/init_db.py` `DEFAULT_PERMISSIONS`): 조회는 새 권한을
  만들지 않고 기존 `DASHBOARD_VIEW`(요약/시계열/연동상태/실패목록/실패상세)
  와 `SYSTEM_MONITOR_VIEW`(scheduler 잡 상태)를 재사용한다(`api/routers/
  tasks.py`가 기존 권한을 재사용하는 관례와 동일 - 조회 권한 신설 최소화).
  상태를 바꾸는 두 동작만 신규 권한으로 분리했다: `OPERATIONS_RETRY`(대량
  재처리), `OPERATIONS_UNKNOWN_RESOLVE`(UNKNOWN 해소) - CS_ASSIGN/CS_CLOSE를
  CS_MANAGE와 분리한 관례와 동일하다. 둘 다 `_VIEW`로 끝나지 않아 Viewer
  역할에 자동 부여되지 않는다(`code.endswith("_VIEW")` 휴리스틱 - CS_PII_DETAIL과
  동일 관례). 조회 권한만 있고 이 두 권한이 없는 사용자는 실제로 403을
  받는다(`tests/integration/test_api_operations.py`로 고정).
- **심각도(INFO/WARNING/ERROR/CRITICAL)**: 오류 문자열 검색이 아니라
  구조화된 필드(상태값·경과시간)와 `config/settings.py`의 숫자 임계값
  (`ops_unknown_critical_after_hours`=24, `ops_stale_running_warning_after_minutes`=15,
  `ops_integration_down_after_days`=3)만으로 분류한다
  (`classify_integration_severity`/`_retry_one` 주변 로직 참고) - 이 값들은
  화면 표시 전용이고 실제 재시도/회수 동작에는 영향을 주지 않는다.
- **기능 플래그**: 이번 단계는 새 외부 채널 호출을 추가하지 않았다(조회
  전용 + 기존에 이미 안전장치가 있는 재처리/해소 라우팅) - 그래서 새
  기능 플래그가 없다. 4개 잡에 추가한 `IntegrationStatus` 기록도 그 잡들의
  기존 기능 플래그(`claims_settlement_sync_enabled`/
  `channel_status_sync_enabled`/`cs_inquiry_sync_enabled`, 전부 기본 False)
  안에서만 실행되며, 플래그가 꺼져 있으면 그 잡들은 여전히 세션도 열지
  않고 즉시 반환한다(이번 단계에서 그 가드 자체는 건드리지 않았다).
- **프론트엔드**(`frontend/src/pages/OperationsDashboardPage.tsx`, `/operations`,
  나비게이션 권한 `DASHBOARD_VIEW`): KPI 카드, 24시간/7일 성공률, 채널별
  연동 상태 표, scheduler 잡 상태 표(권한 없으면 섹션 자체를 숨김), 7일
  성공/실패 추이(기존 `MiniBarChart` 재사용 - 새 차트 라이브러리 추가 안
  함), 통합 실패 작업함(필터/페이지네이션/대량선택/UNKNOWN 경고와 근거
  입력/상세화면 링크). 새 훅 `useAutoRefreshData`(30초 간격)를 만들어
  기존 `useApiData`는 건드리지 않았다(다른 화면 영향 없음) - 자동
  새로고침 중 이전 요청보다 늦게 도착한 응답은 요청 ID 비교로 무시하고,
  언마운트 후에는 `setState`를 호출하지 않으며, interval은 언마운트/자동
  새로고침 끄기 시 정리된다. 실제 API(격리된 스크래치 SQLite)를 띄워
  FAILED/UNKNOWN/SUCCESS 명령을 심어 라이브로 확인했다: 요약·목록이 실제
  DB 값을 반영하고, UNKNOWN 해소와 대량 재처리 둘 다 실제로 상태를 바꾸고
  대시보드가 즉시 갱신되며, 페이지를 떠난 뒤 폴링이 멈춘다. 375/768/1280px
  확인 완료 - 375px의 사이드바 고정폭 오버플로는 다른 모든 화면과 공유하는
  기존 전역 문제라 이번 커밋에서 손대지 않았다.
- **알려진 제한사항(구현 안 함, 완료로 표현하지 않음)**: (1) 실패 행에서
  해당 대상까지의 딥링크는 없다 - 기능유형에 맞는 화면(출고관리/상품
  대량처리)으로만 이동하고 그 화면이 해당 행을 자동으로 열어주지는 않는다
  (그 화면들이 URL 쿼리 파라미터로 초기 필터를 받지 않기 때문 -
  `OrdersPage`만 지원). (2) RETRY_WAIT 조기 재시도(권한자 수동 트리거)는
  만들지 않았다. (3) 클레임/정산의 "부분성공"은 잡 실행 단위로만 구분되고
  플랫폼의 어느 하위 항목이 실패했는지는 이 대시보드에서 알 수 없다(작업
  이력의 `result_summary` 원문을 봐야 한다 - 문자열 파싱으로 세분화하지
  않았다). (4) scheduler "다음 실행 예정 시각"은 제공하지 않는다 - 그
  값은 스케줄러 프로세스 내부 상태(APScheduler)이고 이 API를 서비스하는
  프로세스에서 조회할 수 없다.
- **릴리스 준비 상태(release candidate 통합 검증)**: `release/commercial-erp-candidate`
  브랜치에서 1~6단계 전체를 대상으로 격리 환경(운영 Compose project와 완전히
  분리된 고유 이름의 컨테이너·네트워크·PostgreSQL/Redis volume·호스트 포트)에
  API/scheduler/web 이미지를 새 고유 태그로 빌드해 기동까지 확인했다 -
  `alembic upgrade head`(단일 head)와 `scripts/init_db.py`가 성공하고, 신규
  권한(`OPERATIONS_RETRY` 등) 백필 마이그레이션이 정상 동작하며, API/web
  `/health`가 200을 반환하고, 로그인·운영 대시보드 요약·출고·CS API 기본
  조회가 실제 DB 값을 반영함을 확인했다. 플랫폼 5개를 전부 비활성으로 두고
  모든 외부 쓰기·동기화 기능 플래그를 기본값(False)으로 유지한 채 scheduler
  잡이 실제로 도는 것까지 지켜봤고, 2분 주기 잡들이 전부
  `{"skipped_disabled": 1}`로 종료되는 것을 로그로 직접 확인했다 - 이번
  검증 동안 외부 채널로 나간 HTTP 요청은 0건이다. 검증 후 격리 자원은 전부
  제거했고 운영 컨테이너 5개는 컨테이너ID/이미지ID/시작시각/재시작횟수가
  검증 전후 동일함을 확인했다. **네이버·쿠팡 실계정 검증과 ESM·11번가·
  카카오쇼핑 공식 계약 확인은 여전히 남아있다** - 이 문서의 각 단계 절에
  명시된 그대로다.

## Capability Matrix (2-A단계 진행 현황)

| 캐패빌리티 | 네이버 스마트스토어 | 쿠팡 | ESM | 11번가 | 카카오쇼핑 |
|---|---|---|---|---|---|
| 주문 수집 (`fetch_orders`) | O (기존 구현) | O (기존 구현) | X (`CapabilityUnsupported`) | X (`CapabilityUnsupported`) | X (`CapabilityUnsupported`) |
| 송장 전송 (`submit_shipment`, 1단계, outbox 비동기 실행) | O | O | X | X | X |
| 채널 상태 동기화 (전송성공 반영 + 주기적 읽기전용 재조회) | O | O | X | X | X |
| 취소 기간 대량 수집 (`fetch_cancellations`) | X (문서 미확인 - 아래 참고) | X (`cancelType=CANCEL` 조회 시 status를 못 써 `orderId`가 필수가 되는 공식 제약 - 날짜range만으로의 대량조회는 여전히 불가) | X | X | X |
| 취소 후보 주문 단건 조회 (`fetch_cancellation_status`, 2-A단계 보완 신규) | X (base 기본 미지원 상속) | **O (orderId+cancelType=CANCEL, 회전식 체크포인트로 매 실행 요청 수 제한 - 한계: 미수집 주문/31일 초과 지연 취소는 감지 불가)** | X | X | X |
| 반품 수집 (`fetch_returns`) | X (문서 미확인 - 아래 참고) | **O (returnRequests v6, 상태코드 4종 순회로 전체 수집)** | X | X | X |
| 교환 수집 (`fetch_exchanges`) | X (문서 미확인 - 아래 참고) | **O (exchangeRequests v4, 최대 7일 range)** | X | X | X |
| 정산 회차 수집 (`fetch_settlements`) | X (문서 미확인 - 아래 참고) | **O (settlement-histories v1)** | X | X | X |
| 정산 상세 수집 (`fetch_settlement_details`) | X | **O (revenue-history v1, 주문/라인 단위)** | X | X | X |
| 클레임 승인/거부/환불실행/처리(2-B단계) | X | X | X | X | X |
| 상품 동기화 (`fetch_products`) | **O (기존 구현, 1단계 이전부터 운영 중 - `product_sync_job` 20분 주기, 1단계에서 변경 없음)** | X | X | X | X |
| 재고 동기화 (3단계 예정) | X | X | X | X | X |

- 네이버 취소/반품/교환/정산의 "문서 미확인"은 API 존재 자체가 아니라 **정확한
  필드 스펙 확인이 막힌 상태**를 뜻한다 - "2-A단계 - 취소/반품/교환/정산 채널
  연동" 절의 "여전히 미구현/차단" 항목에 근거와 필요한 문서/권한을 구체적으로
  적어 뒀다.

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

## 실전송 활성화 체크리스트 (1·2-A단계 공통 - 아직 미충족, 재검토 없이 그대로 유지)

두 기능 플래그(`shipment_channel_submit_enabled`/`channel_status_sync_enabled`,
`claims_settlement_sync_enabled`)를 운영에서 켜기 전 반드시 확인해야 하는 항목.
1단계 완결 검토에서 식별된 뒤 2-A단계에서도 재검증하지 않고 그대로 이월했다.

- **재시도 분류 근거 검증**: `ShipmentDispatchService._classify_write_outcome()`의
  SAFE_RETRY/CONFIRMED_FAILED/UNKNOWN 판정 기준(어떤 HTTP상태/예외를 어느
  범주로 분류하는지)이 실제 네이버/쿠팡 API의 실패 응답 패턴과 일치하는지
  실계정으로 확인 필요 - MockTransport로는 "우리가 가정한 실패 패턴"만 검증됨.
- **UNKNOWN 해소 권한**: `POST /api/shipments/commands/{id}/resolve`로 UNKNOWN
  상태를 사람이 수동 해소하는 절차 - 실제로 이 권한을 누가 갖고 어떤 확인
  절차(채널 관리자센터에서 실제 접수 여부 확인 등)를 거쳐 해소할지 운영 프로세스
  합의 필요(코드는 상태 전이만 제공, 운영 절차는 별도 승인 대상).
- 위 두 항목 모두 아직 실계정 검증이 이뤄지지 않았으므로, 이번(2-A단계) 작업에서도
  다시 조사/변경하지 않고 이 체크리스트로만 유지한다.
- 2-A단계 신규: `claims_settlement_sync_enabled` 활성화 전에도 위와 동일하게, 쿠팡
  returnRequests/exchangeRequests/settlement-histories/revenue-history 각 API의
  실제 실패 응답(레이트리밋/일시 오류) 패턴을 실계정으로 먼저 확인해야 한다.

## 2-A단계 진행 요약 (일부 구현 - 완료 아님, 운영 실전송 비활성)

- **브랜치/커밋**: `feature/commercial-erp-stage2-claims-settlements`
  (1단계 완료 커밋 `d25ea73`에서 분기 확인). `072facb`(2-A단계 최초 구현분,
  일부) → `67754b4ac7fb`(쿠팡 취소 후보 주문 단건 조회 보완, 스키마 반영분).
- **테스트**: 신규/확장 단위 테스트(클레임 dedup·미매칭 보존, 쿠팡 반품/교환/정산/
  취소 후보조회 정규화, 정산 동기화 서비스, 클레임/정산 스케줄러 잡) + 통합 테스트
  (`test_api_orders.py`, `test_api_settlements.py`) 전부 통과, 전체 회귀 그린.
  Ruff/MyPy 전체 통과.
- **마이그레이션**: 두 건 모두 격리 SQLite 스크래치 DB에서 autogenerate 후
  스키마 변경만 남도록 무관한 드리프트(orders.assignee_id/confirmed_by,
  order_items.channel_product_id 미명명 FK, product_options 등 컬럼 길이 차이 -
  2-A단계 이전부터 있던 기존 드리프트)를 수동으로 제외. `0dcbe421ba67`은 SQLite
  제약 추가에 batch mode가 필요해 `cancellations.order_item_id` FK만
  `batch_alter_table`로 처리. `67754b4ac7fb`(체크포인트 테이블만 추가)은 격리
  PostgreSQL에서 기존 샘플 데이터가 있는 상태로 upgrade/downgrade 왕복 검증
  완료(체크포인트 테이블은 진행 상태일 뿐이라 downgrade로 사라져도 업무 데이터
  손실 아님). upgrade/downgrade 모두 격리 환경에서 확인, 운영 컨테이너
  (`erp-postgres`/`erp-api`/`erp-web`/`erp-scheduler`) ID·StartedAt 불변 확인.
- **기본 차단**: `claims_settlement_sync_enabled` 기본 False 확인
  (`tests/unit/test_claim_sync_job.py`, `tests/unit/test_settlement_sync_job.py`,
  `tests/integration/test_api_orders.py::TestSyncClaimsDisabledByDefault`,
  `tests/integration/test_api_settlements.py::TestSyncDisabledByDefault`).
