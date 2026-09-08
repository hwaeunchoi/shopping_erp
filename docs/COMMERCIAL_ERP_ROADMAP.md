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

### 5단계(잔여) - CS 연동/택배사 실시간 연동

- **범위**: CS 문의-주문 연결, 택배사 API를 통한 실시간 배송추적, 물류센터
  연동(WMS 트리거) - 5-A단계에서 명시적으로 제외한 항목.
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
