# 상용 ERP 확장 로드맵 (사방넷/플레이오토/셀메이트/샵링커 수준)

이 문서는 `shopping_erp`를 다채널 상용 ERP 수준으로 확장하기 위한 6단계 로드맵과,
각 단계의 완료 기준·채널별 지원 현황(Capability Matrix)을 정리한다.
브랜치 `feature/commercial-erp-stage1-shipment-sync`에서 1단계를 구현했다.

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

### 1단계 - 송장/상태 양방향 동기화 (구현 완료)

- **범위**: 네이버 스마트스토어/쿠팡에 대해 (a) 발송 처리 시 채널로 송장(운송사+
  송장번호) 전송, (b) 채널 주문상태를 수집해 내부 주문상태와 비교 후 허용된 전이면
  반영, 아니면 충돌 기록.
- **완료 기준**:
  - `ShipmentDispatchService.submit()`이 outbox 경유로 멱등하게 송장을 전송하고,
    실패/거부 시 성공으로 위장하지 않는다. (충족)
  - `OrderChannelSyncService.sync_channel_status()`가 허용되지 않는 전이를
    `OrderStatusConflict`로 남기고, 운영자가 `POST /api/order-conflicts/{id}/resolve`로
    해소할 수 있다. (충족 - API/화면 모두 구현)
  - MockTransport 기반 계약 테스트로 요청 스키마/실패 처리/PII 비기록을 검증한다.
    (충족 - `test_naver_smartstore_connector.py`/`test_coupang_connector.py`의
    `TestSubmitShipment`)
  - 실계정 검증은 별도 단계(아래 "실계정 검증 전 필요 조건" 참고, 아직 미충족).

### 2단계 - 취소/반품/교환/정산 채널 연동

- **범위**: 이미 존재하는 내부 취소/반품/교환/정산 모델을, 각 채널의 공식
  취소/반품/교환/정산 API(조회 및 승인/거부 처리)와 연동. 채널에서 발생한
  취소/반품 요청을 수집해 내부 워크플로우로 유입시키는 것을 포함.
- **의존성**: 1단계의 outbox/상태전이/충돌 처리 패턴을 그대로 재사용.
  `BaseMallConnector`에 `supports_cancellation_sync`류 캐패빌리티 플래그를
  이미 정의해 둔 것을 채운다.
- **완료 기준**: 채널별 취소/반품/교환 API 스펙을 공식 문서로 확인 후 MockTransport
  계약 테스트 통과, 상태 충돌 시 자동 덮어쓰기 금지, 정산 데이터는 기존
  `fetch_settlements()` 캐패빌리티를 실제 스케줄 작업으로 연결.

### 3단계 - 상품/재고 동기화

- **범위**: 내부 상품/옵션/재고를 채널별 상품 등록/수정/재고 동기화 API와 연동.
  품절/가격 변경 시 다채널 반영, 채널 상품코드-내부 SKU 매칭 규칙 정비.
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

## Capability Matrix (1단계 기준 현황)

| 캐패빌리티 | 네이버 스마트스토어 | 쿠팡 | ESM | 11번가 | 카카오쇼핑 |
|---|---|---|---|---|---|
| 주문 수집 (`fetch_orders`) | O (기존 구현) | O (기존 구현) | X (`CapabilityUnsupported`) | X (`CapabilityUnsupported`) | X (`CapabilityUnsupported`) |
| 송장 전송 (`submit_shipment`, 1단계 신규) | O | O | X | X | X |
| 채널 상태 동기화 (`OrderChannelSyncService` 경유) | O (전송 성공 후 상태값 흐름은 후속 폴링 작업 필요) | O | X | X | X |
| 취소/반품 동기화 (2단계 예정) | X | X | X | X | X |
| 정산 조회 (`fetch_settlements`) | O (기존 구현, 1단계에서 변경 없음) | O (기존 구현, 1단계에서 변경 없음) | X | X | X |
| 상품/재고 동기화 (3단계 예정) | X | X | X | X | X |

- O = 실제 구현 + MockTransport 계약 테스트로 검증됨 (실계정 검증은 별도).
- X = 미구현. 호출 시 `MarketplaceCapabilityUnsupportedError`를 명시적으로 발생시키며,
  성공을 가장하지 않는다 (`tests/unit/test_marketplace_safety.py::TestShipmentSubmitCapabilityNeverFakesSuccess`로 회귀 검증).

## 실계정 검증 전 필요 조건 (1단계)

- 네이버/쿠팡 실 판매자 계정의 유효한 API 자격증명 (client_id/secret, vendor_id 등).
- 실 주문 데이터에 대한 발송 처리 승인(테스트 발송이 실제 구매자에게 알림/문자로
  나갈 수 있음 - 운영 담당자 승인 필요).
- 쿠팡의 경우 `shipmentBoxId`가 분할배송 시 여러 개일 수 있는데, 현재는 최초 수집된
  박스ID 하나만 저장한다 - 분할배송 계정으로 검증 시 이 제약을 먼저 확인해야 한다.
