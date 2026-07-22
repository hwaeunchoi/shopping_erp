# 상품 데이터 재설계 및 병합 작업 기록 (2026-07-07 ~ 2026-07-08)

이 문서는 상품 자동등록 구조 사고의 원인, 재설계 내용, 병합(정리) 절차, 검증
방법을 다음 작업자가 그대로 재현할 수 있도록 기록한다.

## 1. 왜 문제가 발생했는가

1. **스키마 오해 (2026-07-07)**: 네이버 상품 검색 API(`/external/v1/products/search`)
   응답 구조를 실제 계정으로 검증하지 않고 `detailAttribute.optionInfo.optionCombinations`
   구조라고 가정한 채 `ProductSyncService`(당시 코드)를 작성했다. 실제 응답은
   `{groupProductNo, originProductNo, channelProducts:[...]}` 형태였고, 색상/사이즈
   변형은 서로 다른 `channelProductNo`를 가지되 같은 `groupProductNo`를 공유한다.
2. **그룹핑 실패**: 위 오해로 인해 그룹핑이 전혀 되지 않아, 실제로는 하나의 물리적
   상품(예: 궁중팬 34cm/36cm/38cm 등 사이즈 옵션 모음)에 속하는 여러 채널상품이
   전부 "1개 옵션짜리 별개 상품"으로 등록됐다. 2026-07-07 07:18:59~07:19:04 사이
   약 5초 동안 2,434건이 이렇게 생성됐다(`products.created_at` 타임스탬프로 확인).
3. **SKU 정책 문제**: 이때 생성된 옵션들은 `sku_code = f"AUTO-{platform_id}-{platform_product_code}"`
   형식으로 채번되어, ERP 내부 식별자여야 할 SKU가 플랫폼 코드에 종속됐다.
4. **스키마 수정 후에도 잔존 데이터는 그대로**: 2026-07-08 그룹핑/SKU 정책을
   수정한 뒤 정상 동기화를 다시 실행하면, 대부분의 채널상품이 **실제로는 예전에
   등록된 적이 없는 코드**였기 때문에(구 배치가 잘못된 파싱으로 엉뚱한 값을 저장했거나,
   그 코드 자체가 라이브 카탈로그에 존재하지 않았기 때문에) 새로 올바르게
   그룹핑된 Product가 또 생성됐다. 그 결과 **같은 groupProductNo가 "07-07에 만든
   깨진 Product"와 "07-08에 새로 만든 정상 Product" 두 개로 쪼개진 상태**가
   대량으로 남았다 — 이것이 이번 작업의 핵심 정리 대상이다.

## 2. 기존 구조 vs 변경된 구조

| 항목 | 기존(문제 있던 상태) | 변경 후 |
|---|---|---|
| Product : ProductOption : PlatformMap 관계 | 1:N:N (스키마 자체는 문제 없었음) | 동일 (변경 없음) |
| 매핑 키 | `platform_product_code`(옵션번호)와 `platform_option_id`가 항상 같은 값으로 중복 저장 | `platform_product_code` 컬럼 폐기. `platform_option_id`(옵션 단위, 유니크)와 `platform_product_id`(상품/그룹 단위, 비유니크, 매칭 2순위) 두 컬럼으로 명확히 분리 |
| SKU | `AUTO-{platform_id}-{platform_product_code}` (플랫폼 코드에 종속) | `SKU-{option.id:06d}` (ERP 내부 채번, 플랫폼 데이터와 완전 분리) |
| 자동매칭 순서 | 판매자상품코드 → SKU → 옵션번호(전체 플랫폼) → 이름/옵션명 유사도(RapidFuzz) | `platform_option_id` 정확 일치 → `platform_product_id` 정확 일치(옵션 1개로 특정될 때만) → `seller_product_code` 정확 일치(플랫폼 무관). **이름/유사도 매칭 완전 제거** |
| 판매가 | 저장 안 됨(커넥터가 필드 자체를 안 가져옴) | `product_options.sale_price`로 저장, 네이버 `salePrice` 동기화 |
| 상품 생성 권한 | 주문 API에서도 생성 가능했던 적이 있었음(과거 구조) | `ProductSyncService.sync_products_from_naver()` 한 경로만 생성 가능. 주문 동기화(`OrderSyncService`)는 매칭만 시도, 실패 시 `product_unmatched_items`에 기록하고 끝(관련 코드: `services/order_sync_service.py`, `services/product_sync_service.py`) |

관련 마이그레이션: `migrations/versions/20260708_1600_7d4a1f9c3b2e_platform_map_and_sku_redesign.py`.

## 3. 병합 기준 (그룹 단위 정리 원칙)

같은 `groupProductNo`(ERP 컬럼명 `platform_product_id`)를 공유하는 옵션들이
서로 다른 Product에 흩어져 있을 때:

1. **유지(Keep) 대상 선정**: 그 그룹의 옵션을 가장 많이/정상적으로 보유한
   Product를 유지 대상으로 선정한다(대개 최신 정상 동기화가 만든 Product).
2. **병합 대상 옵션 식별**: 나머지 Product에 남아있는 옵션은, 유지 대상 Product
   산하에 "정확히 같은 `platform_option_id`를 가진 옵션"이 이미 있는지 반드시
   확인한다(있으면 진짜 중복, 없으면 그룹 내에서 아직 등록 안 된 진짜 다른
   variant이므로 옵션 자체를 이동).
3. **절대 이름/유사도로 병합 판단하지 않는다** — 반드시 아래 중 하나의 실제
   고유 ID 일치로만 결정한다: `seller_product_code` 동일, `channelProductNo`
   (`platform_option_id`) 동일, `platform_product_id`(groupProductNo) 동일.
4. **SKU는 절대 같이 바꾸지 않는다** — 병합은 Product/Option 소속만 바꾸는
   작업이고, SKU 정책 변경은 별도 승인 대상이다.

## 4. 검증 절차 (그룹 1개 처리 시 항상 이 순서)

```
1. 조회
   - 대상 Product/Option/PlatformMap/OrderItem 현재 상태를 표로 정리
   - 유지 대상 Product가 is_deleted=false인지 확인
   - 이동 대상 옵션들의 platform_option_id가 서로/기존 옵션과 중복이 없는지 확인
   - 각 이동 대상 옵션의 platform_map 개수(legacy 자기참조 + 신규 실데이터) 확인
   - 관련 order_item이 정확히 어떤 product_option_id를 참조하는지 확인
2. 옵션 이동
   - UPDATE product_options SET product_id = <유지 대상> WHERE id IN (...)
   - product_option_id(PK)는 절대 변경하지 않는다 -> order_items가 자동으로 유지됨
3. 브라우저 확인
   - 유지 대상 Product 상세 화면에서 옵션 수/이름/플랫폼매핑이 기대한 대로인지 확인
   - 콘솔 에러 없는지 확인
4. Soft Delete
   - UPDATE products SET is_deleted = true WHERE id IN (<빈 껍데기들>)
   - 병합 대상이 아닌 다른 Product는 절대 건드리지 않는다
5. Legacy PlatformMap 삭제
   - 삭제 직전 재조회: id가 정확히 일치하는지, seller_product_code가
     비어있는지, platform_option_id == platform_product_id인지,
     동일 옵션에 대한 신규 정상 매핑이 이미 존재하는지 확인
   - 조건이 전부 맞을 때만 DELETE FROM product_platform_map WHERE id IN (...)
     (반드시 id 기준, 시간/조건식 기준 삭제 금지)
6. 최종 검증
   - 삭제 대상 id COUNT(*) = 0 확인
   - 각 옵션의 platform_map이 정확히 1개인지 확인
   - order_item이 무손상인지(수량/옵션ID 그대로) 확인
   - 브라우저 새로고침 후 재확인
```

## 5. 실제 수행한 SQL (그룹 1~6, 예시로 그룹1 전체 기록)

```sql
-- 조회(생략, 위 절차 2번 참고)
UPDATE product_options SET product_id = 133 WHERE id IN (66, 98, 139, 143, 156);
UPDATE products SET is_deleted = true WHERE id IN (64, 96, 137, 141, 154);
DELETE FROM product_platform_map WHERE id IN (137, 141, 154);
```
그룹 2~6도 동일한 3단계(UPDATE product_options → UPDATE products is_deleted →
DELETE legacy map by id)를 각 그룹의 실제 조회 결과에 맞는 id로 반복 수행했다.
전체 실행 이력은 이 세션의 대화 로그에 SQL과 결과가 전부 남아있다.

## 6. 데이터 보호 방법

- 스키마 변경(migration) 직전 `pg_dump -F c`로 전체 DB 풀백업을 남겼다
  (`erp_full_backup_20260708.dump`).
- 모든 DELETE는 **반드시 `WHERE id IN (구체적 숫자 목록)`** 형태로만 실행했다 —
  시간 조건(`created_at > ...`)이나 이름 유사도 조건으로 삭제한 적이 없다.
- 삭제 직전에 항상 재조회로 대상 행의 내용(특히 `seller_product_code` 공란 여부와
  `platform_option_id == platform_product_id`)을 재확인한 뒤에만 DELETE를 실행했다.
- Soft Delete(`products.is_deleted = true`)만 사용했고, 실제 row를 지운 적이
  없다 — 문제가 있으면 `is_deleted = false`로 되돌리기만 하면 복구된다.

## 7. 주문 데이터가 손상되지 않는 이유

`order_items.product_option_id`는 `product_options.id`(PK)를 참조한다. 병합
작업은 **`product_options.product_id`(어느 상품에 속하는지)만 변경**했을 뿐,
`product_options.id` 자체는 절대 바꾸지 않았다. 따라서 `order_items`는 수정할
필요가 전혀 없었고, 실제로 매 그룹마다 병합 전후 `order_items` 행을 조회해
수량/`product_option_id`가 완전히 동일함을 확인했다.

## 8. 재발 방지 방법

1. **코드 레벨**: `ProductSyncService._find_or_create_product()`가 `groupProductNo`
   (`platform_product_id`) 기준으로 그룹핑하고, `match_unmapped_item()`이
   실제 고유 ID(옵션번호→상품번호→판매자코드)만으로 매칭하도록 재설계했다
   (`services/product_sync_service.py`). 이름/유사도 매칭은 코드에서 완전히
   제거했다. 추가로 `_find_or_create_product()`에 2차 방어 확인(같은
   groupProductNo로 이미 등록된 상품이 있으면 새로 만들지 않고 재사용,
   경고 로그 남김)을 넣어 페이지네이션 등으로 같은 그룹이 두 배치로 나뉘어
   들어와도 Product가 다시 쪼개지지 않게 했다(10.2절 참고).
2. **스키마 레벨**: `product_platform_map`에 `(platform_id, platform_option_id)`
   유니크 제약을 걸어, 같은 옵션이 같은 플랫폼에 두 번 매핑될 수 없다.
3. **운영 레벨**: `scripts/verify_product_integrity.py`를 만들어 아래 7가지를
   자동 점검한다(자세한 내용은 다음 섹션). 네이버 상품 동기화 직후 정기적으로
   실행해 사람이 매번 SQL로 조사하지 않아도 문제를 즉시 알 수 있게 했다.

## 9. `scripts/verify_product_integrity.py` — 자동 정합성 검증

### 9.1 목적

사람이 매번 SQL을 직접 짜서 조사하지 않아도, 상품/옵션/플랫폼매핑 데이터가
이번 사고(2026-07-07~08)와 같은 패턴으로 다시 깨졌는지 한 번의 실행으로 알 수
있게 한다. 운영자는 이 스크립트의 출력(과 종료 코드)만 보고도 "지금 배포해도
되는 상태인지", "남아있는 문제가 당장 조치가 필요한지 아니면 여유 있게
처리해도 되는지"를 판단할 수 있어야 한다는 목표로 설계했다.

### 9.2 실행 방법과 종료 코드

```
python scripts/verify_product_integrity.py
echo $?
# 0 = ERROR 없음 -> 배포/운영 계속 진행 가능 (WARNING이 있어도 0)
# 1 = ERROR 1건 이상 존재 -> 배포 중단, 즉시 조사 필요
```

CI/배포 파이프라인에서는 이 스크립트를 배포 단계 직전에 실행해 종료 코드로
게이트를 걸면 된다(예: GitHub Actions/서버 배포 스크립트에서
`python scripts/verify_product_integrity.py || exit 1` 형태로 연결).
**WARNING은 종료 코드에 영향을 주지 않는다** — 운영 장애가 아닌 기술부채와,
사람이 판단해야 하는 알려진 케이스를 놓치지 않고 계속 보여주기 위한 것이지,
배포를 막기 위한 것이 아니다.

### 9.3 PASS / WARNING / ERROR 판정 기준

- **ERROR**: 구조적으로 절대 있어서는 안 되는 상태. 유니크 제약 위반, 같은
  groupProductNo가 여러 Product로 분리, 참조 무결성 깨짐 등 — 이번 사고의
  핵심 증상과 직결된다. 1건이라도 있으면 종료 코드 1.
- **WARNING**: 지금 당장 장애는 아니지만 성격이 다른 두 부류로 나뉘고,
  출력의 "분류" 줄에 어느 쪽인지 항상 표시된다.
  - **기술부채**: 운영에 영향 없음. 별도 승인 후 여유 있게 정리하면 되는 것.
  - **수동검토필요**: 자동 탐지/자동 병합의 사각지대에 있는 알려진 문제 —
    방치하면 다음 동기화 때 실제로 다시 문제가 생길 수 있어 사람이 판단해야
    하는 것.

### 9.4 점검 항목 7가지

| # | 항목 | 판정 | 분류(WARNING인 경우) | 의미 |
|---|---|---|---|---|
| 1 | 동일 `platform_option_id` 중복 | ERROR | - | 유니크 제약이 있어야 정상적으로 0건. 0건이 아니면 제약이 깨진 것이므로 즉시 조사 |
| 2 | 동일 `groupProductNo`가 여러 Product로 분리 | ERROR | - | 이번 사고의 핵심 증상. 0건이어야 함 |
| 3 | Soft Delete Product에 Option 잔존 | WARNING | 기술부채 | 병합 시 옵션을 다 옮기지 않고 삭제만 한 실수 감지. 완전히 고립된(참조 0건) 테스트 데이터라면 삭제해서 WARNING 자체를 없앨 수 있다(2026-07-09 실제로 그렇게 정리함 - 9.6절 참고) |
| 4 | Legacy 자기참조 PlatformMap | WARNING | **기술부채**(단, 정리 시 반드시 검증 필요) | `platform_option_id == platform_product_id`이고 `seller_product_code`가 빈 행. 지금은 운영 장애가 없지만, 이 패턴의 행을 지우는 로직 자체에 검증이 부족했던 사고(10.3절)가 있었으므로 향후 정리 시 "삭제 후에도 해당 옵션에 정상 매핑이 최소 1개 남는지"를 SQL로 반드시 재확인한 뒤에만 삭제해야 한다 |
| 5 | AUTO SKU 존재 | WARNING | 기술부채 | SKU는 매칭 로직에 전혀 사용되지 않는 ERP 내부 표시값일 뿐이라 운영 영향 없음 |
| 6 | PlatformMap 없는 ProductOption | WARNING | **수동검토필요(가장 중요)** | groupProductNo 기반 자동 병합 탐지는 platform_map이 있는 옵션만 대상으로 하므로, 이 항목은 자동 병합의 사각지대에 있는 **known duplicate 후보**다. 방치하면 다음 네이버 상품 동기화 때 같은 물리 상품이 새 ProductOption으로 또 생성될 수 있다 - 상품관리 화면에서 어느 기존 옵션과 동일 상품인지 사람이 확인 후 수동 병합해야 한다 |
| 7 | ProductOption을 참조하지 않는 PlatformMap(고아 행) | ERROR | - | FK 제약상 구조적으로 불가능해야 함 |

### 9.5 향후 운영자가 알아야 할 것

이 스크립트만 실행하면(코드나 DB를 직접 들여다볼 필요 없이) 지금 데이터
상태가 배포 가능한지, 남은 문제가 당장 조치해야 하는지 여유 있게 처리해도
되는지 전부 판단할 수 있다. 정기 실행(네이버 상품 동기화 직후, 또는 배포
전)을 권장한다.

### 9.6 2026-07-09 후속 정리: Soft Delete Product 옵션 잔존 건 해소

항목 3에서 걸리던 `product_id=97`(`[검증용] 삭제 테스트 상품`, 이번 세션 중
삭제 기능 테스트용으로 만든 데이터)의 옵션을 정리 전 반드시 확인해야 할
3가지(platform_map, order_items, product_cost_history/inventory)를 전부 SQL로
재확인해 **참조 0건**임을 확인한 뒤, 해당 옵션 1건만 id 기준으로 삭제했다.
재실행 결과 이 항목은 `PASS`로 전환됐다(WARNING 4건 -> 3건).

## 10. 2026-07-08 대규모 분리 그룹 발견과 해결 (최종)

### 10.1 발견 경위

**최초 실행 결과, 이전에 완료했다고 보고한 "6개 그룹 병합"이 문제의 전체 범위가
아니었음이 밝혀졌다.** `verify_product_integrity.py`가 **항목 2(그룹 분리)에서
1,360건의 ERROR**를 발견했다. 원인을 SQL로 직접 확인한 결과:

- 1,360건 중 1,354건(99.6%)이 "2026-07-07(구 AUTO 잔여 배치)에 생성된 Product +
  2026-07-08(재설계 후 정상 동기화)에 생성된 Product" 쌍이었다 — 6개 그룹에서
  발견했던 것과 **완전히 동일한 패턴**이 훨씬 큰 규모로 존재했다.
- 이전 조사(6개 그룹)는 사용자가 제공한 CSV의 152개 그룹으로 범위를 한정해서
  찾은 것이었고, 그 CSV는 전체 라이브 카탈로그(약 1,518개 그룹) 중 일부(판매중만)만
  포함하는 스냅샷이었기 때문에 나머지 약 1,354개 그룹의 분리는 그때 발견되지
  않았다.
- 나머지 6건 중 5건은 "구 배치끼리(03:22/03:30 vs 07:19)"의 중복, **1건
  (groupProductNo=51839130)은 2026-07-08 재설계 이후 코드가 같은 실행에서
  만든 신규+신규 중복**이었다 — 이것만은 레거시 데이터 문제가 아니라 현재
  코드의 실제 결함이었다.

### 10.2 groupProductNo=51839130 근본 원인 (재현·증명 완료)

실제 네이버 상품 API를 다시 조회해 이 그룹의 5개 채널상품(20호/15호/10호/5호/3호)을
전수 확인한 결과, **5개 전부 실제로 동일한 groupProductNo=51839130을 가지고
있었다**(타입도 전부 `int`로 일관됨 - 타입 불일치는 원인이 아니었다). 다만 이
5개가 상품 검색 API의 페이지네이션 응답에서 **1페이지와 11페이지로 나뉘어**
내려왔다(`originProductNo` 차이가 매우 커서 등록 시점이 크게 다른 변형이었기
때문으로 추정).

`ProductSyncService._find_or_create_product()`는 원래 "이번에 받은 raw 묶음
(그룹) 자체의 items 중 이미 매핑된 옵션이 있는가"만 확인했다. `_normalize_live_products`가
전체 페이지를 다 합친 뒤 한 번에 그룹핑하므로 이론상 한 그룹은 하나의 raw
묶음으로만 나와야 하지만, 페이지네이션 중 일시적 응답 저하·재시도·중복 실행
등으로 같은 groupProductNo의 항목이 서로 다른 배치로 나뉘어 처리될 가능성이
남아있었고, 실제로 이 경로로 Product가 2개(4개 옵션짜리 하나, 1개 옵션짜리
하나) 생성되는 사고가 있었다.

**수정**: `_find_or_create_product()`에 2차 방어 확인을 추가했다 - 현재 raw
묶음의 items에서 기존 매핑을 못 찾더라도, **같은 platform_product_id
(groupProductNo)로 이미 등록된 옵션이 DB에 있는지** 한 번 더 확인하고, 있으면
그 Product를 재사용한다(새 Product를 만들지 않는다). 이 경로가 실제로 사용될
때는 반드시 `logger.warning()`으로 남겨, 이 경고가 반복되면 페이지네이션/응답
안정성을 재점검해야 함을 알 수 있게 했다(`services/product_sync_service.py`
`_find_or_create_product`).

**검증**:
1. 이 정확한 시나리오(같은 platform_product_id의 항목이 서로 다른 두 배치로
   나뉘어 `sync_products_from_naver()`가 두 번 호출되는 상황)를 재현하는 단위
   테스트를 추가했다(`tests/unit/test_product_sync_service.py::TestSyncProductsFromNaver::
   test_same_group_product_id_split_across_two_raw_batches_does_not_duplicate_product`) -
   수정 전 코드로는 이 테스트가 실패했고(2개 Product 생성), 수정 후 통과함을 확인했다.
2. 수정된 코드를 실제로 배포한 뒤 실제 네이버 API로 전체 상품 재동기화를
   2회 실행했다 - 둘 다 `created_products: 0`(완전 멱등), 방어 경로의
   `WARNING` 로그도 발생하지 않았다(이번 동기화에서는 페이지 분리가 재발하지
   않았음을 의미).
3. 브라우저에서 groupProductNo=51839130에 해당하는 상품(#2849)이 5개 옵션을
   모두 정상적으로 갖고 있음을 확인했다.

### 10.3 자동 병합 스크립트로 1,360개 그룹 일괄 처리 및 그 과정에서 발견된 2차 버그

`scripts/merge_split_product_groups.py`로 1,360개 그룹 전체를 대상으로
Dry-run(전수 안전성 검사, 100% SAFE 확인) → 전체 백업(`pg_dump -F c`) →
`--execute` 실행까지 완료했다. 1,360개 그룹, 옵션 1,360개 이동, Product 1,360개
Soft Delete, legacy platform_map 1,354개 삭제, order_item 8건 영향(모두 무손상)
까지 전부 정상 반영됐다.

**그런데 직후 `verify_product_integrity.py` 재실행 결과 "PlatformMap 없는
ProductOption"이 1건에서 1,355건으로 급증**했다. 원인 조사 결과, 스크립트의
legacy map 삭제 로직이 **"어떤 옵션의 legacy 자기참조 행이 삭제 후에도 그
옵션에 정상 매핑이 최소 1개 남는지"를 확인하지 않고, legacy 패턴에 맞으면
무조건 삭제**하도록 되어 있었다. 우연히 이번 1,360개 그룹에서는 이동된 옵션의
legacy 행 1,354개**전부**가 "그 옵션의 유일한 매핑"이었기 때문에, 삭제 직후
1,354개 옵션이 플랫폼과 완전히 연결이 끊겼다.

**복구**: 실행 직전에 남겨둔 전체 백업(`erp_backup_before_bulk_merge_20260708.dump`)을
별도 임시 DB(`erp_restore_temp`)로 복원한 뒤, 백업 시점과 현재 라이브 DB의
`product_platform_map.id` 목록을 diff해 **정확히 삭제된 1,354개 id**를 특정하고,
그 행들만(다른 데이터는 전혀 건드리지 않고) 원래 id 그대로 라이브 DB에
재삽입했다. 옵션 이동/Product Soft Delete는 올바른 작업이었으므로 되돌리지
않았다 - 오직 잘못된 legacy map 삭제만 복구했다. 복구 후
`verify_product_integrity.py`의 "PlatformMap 없는 ProductOption"이 merge 이전과
동일한 1건(기존에 알려진 케이스)으로 정확히 복귀했음을 확인했다.

**스크립트 수정**: `build_plan()`의 legacy map 판단 로직을 "그 옵션의 매핑
행들을 전부 모은 뒤, legacy 행과 정상(실데이터) 행을 나누고, **정상 행이
1개 이상 남아있을 때만** legacy 행들을 삭제 대상에 포함"하도록 고쳤다. 정상
행이 하나도 없는 옵션(legacy 행만 있는 옵션)은 아무것도 삭제하지 않는다.
이 정확한 경우를 재현하는 회귀 테스트 2건을 추가했다
(`tests/unit/test_merge_split_product_groups.py`) - 수정 전 로직으로는 첫 번째
테스트가 실패했을 것이고, 수정 후에는 둘 다 통과한다.

### 10.4 최종 상태 (전부 실제 실행 결과로 확인)

- `verify_product_integrity.py`: **ERROR 0건** (동일 groupProductNo 분리 0건,
  동일 platform_option_id 중복 0건, 고아 platform_map 0건)
- 실제 네이버 API로 상품 동기화 재실행 2회: 둘 다 `created_products: 0`(완전
  멱등), 방어 경로 경고 로그 미발생
- pytest 전체(378개) 통과, ruff/mypy 통과
- 브라우저: 6개 수동 병합 그룹(#133 등) + 51839130 자동 병합 그룹(#2849) 전부
  정상 옵션 수 표시, 콘솔 에러 없음
- 남은 WARNING(9.4절 분류 기준, "운영에 영향 없음"으로 뭉뚱그리지 않고 성격별로
  구분): AUTO SKU 2,528건(기술부채, 매칭 로직 무관), legacy 자기참조
  platform_map 2,424건(기술부채이나 정리 시 검증 필수 - 10.3절 사고 참고),
  PlatformMap 없는 ProductOption 1건(**수동검토필요** - 상품1, known duplicate
  후보, 자동 병합 불가). 검증용 테스트 상품(`product_id=97`)의 옵션 잔존
  건은 2026-07-09 참조 0건 확인 후 삭제해 해소함(9.6절).
