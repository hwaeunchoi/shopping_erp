// 화면에 보여 줄 한국어 표기의 단일 출처.
//
// - 백엔드가 주고받는 내부 값(enum, 코드)은 바꾸지 않는다. 이 파일은 "표시"만 담당한다.
// - 같은 값은 모든 화면에서 같은 문구로 보인다. 화면마다 따로 번역표를 만들지 않는다.
// - 알 수 없는 값은 내부 코드를 그대로 노출하지 않고 UNKNOWN_LABEL로 보여 준다.
// - 이 파일은 React에 의존하지 않는 순수 모듈이라 Node 테스트에서 그대로 검증한다.

export const UNKNOWN_LABEL = '알 수 없음'

export type Tone = 'ok' | 'info' | 'warn' | 'danger' | 'muted'

interface Entry {
  label: string
  tone?: Tone
}

function entries(map: Record<string, string | Entry>): Record<string, Entry> {
  const out: Record<string, Entry> = {}
  for (const [key, value] of Object.entries(map)) {
    out[key] = typeof value === 'string' ? { label: value } : value
  }
  return out
}

export const LABELS = {
  // 주문
  orderStatus: entries({
    NEW: { label: '신규', tone: 'info' },
    PREPARING: { label: '상품 준비 중', tone: 'info' },
    SHIPPING: { label: '배송 중', tone: 'info' },
    DELIVERED: { label: '배송 완료', tone: 'ok' },
    CANCELED: { label: '취소', tone: 'danger' },
    EXCHANGED: { label: '교환 완료', tone: 'muted' },
    RETURNED: { label: '반품 완료', tone: 'muted' },
    REFUNDED: { label: '환불 완료', tone: 'muted' },
  }),
  paymentStatus: entries({
    PAID: { label: '결제 완료', tone: 'ok' },
    UNPAID: { label: '미결제', tone: 'warn' },
  }),
  // 주문의 배송 진행 상태
  shippingStatus: entries({
    UNSHIPPED: { label: '미발송', tone: 'warn' },
    SHIPPING: { label: '배송 중', tone: 'info' },
    DELIVERED: { label: '배송 완료', tone: 'ok' },
  }),
  // 배송(송장) 건의 상태
  shipmentStatus: entries({
    READY: { label: '출고 준비', tone: 'warn' },
    SHIPPING: { label: '배송 중', tone: 'info' },
    DELIVERED: { label: '배송 완료', tone: 'ok' },
  }),
  // 주문에 연결된 클레임(교환·반품·취소) 구분
  orderClaim: entries({
    NONE: '없음',
    EXCHANGE: { label: '교환', tone: 'info' },
    RETURN: { label: '반품', tone: 'danger' },
    CANCEL: { label: '취소', tone: 'danger' },
    CANCELLATION: { label: '취소', tone: 'danger' },
  }),
  // 교환·반품·취소 처리 상태(세 화면이 같은 값은 같은 문구를 쓴다)
  claimStatus: entries({
    REQUESTED: { label: '접수', tone: 'warn' },
    APPROVED: { label: '승인', tone: 'info' },
    SHIPPED: { label: '발송', tone: 'info' },
    RECEIVED: { label: '수령', tone: 'info' },
    REFUNDED: { label: '환불 완료', tone: 'ok' },
    COMPLETED: { label: '완료', tone: 'ok' },
    REJECTED: { label: '거절', tone: 'danger' },
  }),

  // 고객 문의(CS)
  csStatus: entries({
    OPEN: { label: '접수', tone: 'warn' },
    IN_PROGRESS: { label: '처리 중', tone: 'info' },
    WAITING_CUSTOMER: { label: '고객 답변 대기', tone: 'muted' },
    WAITING_CHANNEL: { label: '채널 처리 대기', tone: 'muted' },
    RESOLVED: { label: '해결 완료', tone: 'ok' },
    CLOSED: { label: '종결', tone: 'muted' },
  }),
  csPriority: entries({ LOW: '낮음', NORMAL: '보통', HIGH: '높음', URGENT: { label: '긴급', tone: 'danger' } }),
  inquiryType: entries({
    DELIVERY: '배송',
    EXCHANGE_RETURN: '교환·반품',
    PRODUCT: '상품',
    PAYMENT: '결제',
    ETC: '기타',
  }),
  csBulkOutcome: entries({
    ACCEPTED: { label: '접수됨', tone: 'ok' },
    BLOCKED: { label: '진행할 수 없음', tone: 'danger' },
    VALIDATION_FAILED: { label: '입력값 오류', tone: 'danger' },
    NOT_FOUND: { label: '대상 없음', tone: 'danger' },
  }),

  // 채널 전송 명령(출고·상품 일괄 처리·상품 상세 공통)
  commandStatus: entries({
    PENDING: { label: '전송 대기', tone: 'muted' },
    RUNNING: { label: '전송 중', tone: 'info' },
    SUCCESS: { label: '전송 완료', tone: 'ok' },
    FAILED: { label: '전송 실패', tone: 'danger' },
    RETRY_WAIT: { label: '재시도 대기', tone: 'warn' },
    UNKNOWN: { label: '결과 확인 필요', tone: 'warn' },
    CANCELLED: { label: '취소됨', tone: 'muted' },
  }),
  commandType: entries({
    SHIPMENT_SUBMIT: '송장 전송',
    PRODUCT_CREATE: '상품 등록',
    PRODUCT_OPTION_CREATE: '옵션 등록',
    INVENTORY_UPDATE: '재고 전송',
    SALE_STATUS_UPDATE: '판매상태 전송',
    PRODUCT_INFO_UPDATE: '상품정보 수정',
  }),
  // 접수 결과(일괄 처리·출고 요청 공통)
  requestOutcome: entries({
    ACCEPTED: { label: '접수됨', tone: 'ok' },
    ALREADY_PROCESSED: { label: '이미 처리됨', tone: 'muted' },
    DUPLICATE_OR_SUPERSEDED: { label: '중복 또는 대체됨', tone: 'muted' },
    BLOCKED: { label: '진행할 수 없음', tone: 'danger' },
    BLOCKED_BY_UNKNOWN: { label: '결과 확인이 필요한 이전 명령 때문에 보류', tone: 'warn' },
    VALIDATION_FAILED: { label: '입력값 오류', tone: 'danger' },
    UNSUPPORTED: { label: '지원하지 않는 채널', tone: 'danger' },
    FAILED_TO_ENQUEUE: { label: '접수 실패', tone: 'danger' },
    RETRIED: { label: '다시 접수됨', tone: 'ok' },
    NOT_FOUND: { label: '대상 없음', tone: 'danger' },
    NOT_RETRYABLE: { label: '다시 시도할 수 없음', tone: 'danger' },
    UNKNOWN_REQUIRES_RESOLUTION: { label: '결과 확인 후 처리 필요', tone: 'warn' },
  }),
  // 출고 항목·배치
  fulfillmentStatus: entries({
    READY: { label: '출고 대기', tone: 'warn' },
    PICKING: { label: '피킹 중', tone: 'info' },
    PICKED: { label: '피킹 완료', tone: 'info' },
    VERIFYING: { label: '검수 중', tone: 'info' },
    VERIFIED: { label: '검수 완료', tone: 'info' },
    PACKED: { label: '포장 완료', tone: 'ok' },
    SUBMIT_PENDING: { label: '송장 전송 접수 중', tone: 'info' },
    SUBMITTED: { label: '송장 전송 접수됨', tone: 'ok' },
    BLOCKED: { label: '진행할 수 없음', tone: 'danger' },
    CANCELLED: { label: '취소됨', tone: 'muted' },
    CLOSED: { label: '마감', tone: 'muted' },
  }),

  // 동기화·작업 결과
  taskStatus: entries({
    RUNNING: { label: '실행 중', tone: 'info' },
    SUCCESS: { label: '성공', tone: 'ok' },
    PARTIAL_SUCCESS: { label: '일부 완료', tone: 'warn' },
    PARTIAL: { label: '일부 완료', tone: 'warn' },
    FAILED: { label: '실패', tone: 'danger' },
    UNSUPPORTED: { label: '지원하지 않음', tone: 'muted' },
    DISABLED: { label: '사용 안 함', tone: 'muted' },
    ALREADY_RUNNING: { label: '이미 실행 중', tone: 'muted' },
    NOT_REQUESTED: { label: '요청 안 함', tone: 'muted' },
  }),
  taskType: entries({
    ORDER_COLLECT: '주문 수집',
    AD_COLLECT: '광고 수집',
    BACKUP: '백업',
    REPORT_GENERATE: '보고서 생성',
    FULL_SYNC: '전체 동기화',
    MALL_SYNC: '쇼핑몰 동기화',
    PRODUCT_SYNC: '상품 동기화',
    ALERT_EVALUATE: '알림 조건 확인',
    IMPORT: '가져오기',
    EXPORT: '내보내기',
  }),
  triggerType: entries({ SCHEDULE: '자동 실행', MANUAL: '수동 실행', CATCHUP: '누락분 보충' }),
  integrationStatus: entries({
    NORMAL: { label: '정상', tone: 'ok' },
    PARTIAL: { label: '일부 완료', tone: 'warn' },
    ERROR: { label: '오류', tone: 'danger' },
    TOKEN_EXPIRING: { label: '인증 만료 임박', tone: 'warn' },
  }),
  integrationType: entries({ MALL: '쇼핑몰', AD: '광고' }),
  // 운영 현황의 심각도(INFO는 "정상"을 뜻한다)
  opsSeverity: entries({
    INFO: { label: '정상', tone: 'ok' },
    WARNING: { label: '주의', tone: 'warn' },
    ERROR: { label: '오류', tone: 'danger' },
    CRITICAL: { label: '심각', tone: 'danger' },
  }),
  // 알림의 중요도
  notificationSeverity: entries({
    INFO: { label: '안내', tone: 'info' },
    WARNING: { label: '주의', tone: 'warn' },
    CRITICAL: { label: '심각', tone: 'danger' },
  }),
  logLevel: entries({
    INFO: { label: '정보', tone: 'info' },
    WARN: { label: '경고', tone: 'warn' },
    ERROR: { label: '오류', tone: 'danger' },
  }),

  // 상품·재고·발주
  productStatus: entries({ ACTIVE: '판매 가능', DISCONTINUED: '단종' }),
  saleStatus: entries({
    ON_SALE: { label: '판매중', tone: 'ok' },
    SUSPENDED: { label: '판매중지', tone: 'warn' },
  }),
  mappingStatus: entries({
    PENDING: { label: '연결 대기', tone: 'warn' },
    MATCHED: { label: '연결 완료', tone: 'ok' },
  }),
  purchaseOrderStatus: entries({
    DRAFT: '작성 중',
    ORDERED: { label: '발주 완료', tone: 'info' },
    PARTIALLY_RECEIVED: { label: '부분 입고', tone: 'warn' },
    RECEIVED: { label: '입고 완료', tone: 'ok' },
    CANCELLED: { label: '취소됨', tone: 'muted' },
  }),
  inventoryTransaction: entries({ IN: '입고', OUT: '출고', ADJUST: '조정', INSPECT: '검수' }),
  referenceType: entries({
    ORDER: '주문',
    PURCHASE_ORDER: '발주',
    PURCHASE: '발주',
    RETURN: '반품',
    MANUAL: '직접 입력',
  }),

  // 정산·비용·보고서·알림 규칙
  settlementStatus: entries({
    SCHEDULED: { label: '정산 예정', tone: 'muted' },
    PARTIAL: { label: '일부 정산', tone: 'warn' },
    COMPLETED: { label: '정산 완료', tone: 'ok' },
    DISCREPANCY: { label: '차액 확인 필요', tone: 'danger' },
  }),
  costCategory: entries({
    SHIPPING: '배송비',
    PACKAGING: '포장비',
    PLATFORM_FEE: '플랫폼 수수료',
    AD_AGENCY_FEE: '광고 대행 수수료',
    RETURN_SHIPPING: '반품 배송비',
    ETC: '기타',
  }),
  costType: entries({ FIXED: '고정비', VARIABLE: '변동비' }),
  reportFrequency: entries({ DAILY: '매일', WEEKLY: '매주', MONTHLY: '매월' }),
  reportFormat: entries({ XLSX: '엑셀(XLSX)', PDF: 'PDF' }),
  alertMetric: entries({
    ORDER_COUNT_TODAY: '오늘 주문 수',
    AD_COST: '광고비',
    ROAS: '광고 수익률(ROAS)',
    RETURN_RATE: '반품률',
    UNSHIPPED_DAYS: '미발송 경과일',
    API_FAILURE: '채널 연동 실패',
    BACKUP_FAILURE: '백업 실패',
  }),
  alertOperator: entries({ LT: '미만', LTE: '이하', GT: '초과', GTE: '이상', EQ: '같음' }),
  alertCheckFrequency: entries({ REALTIME: '실시간', HOURLY: '매시간', DAILY: '매일' }),
  settingCategory: entries({ BACKUP: '백업', REPORT: '보고서', SYSTEM: '시스템' }),
  recordTarget: entries({
    ORDER: '주문',
    PRODUCT: '상품',
    CUSTOMER: '고객',
    PLATFORM: '플랫폼',
    GLOBAL: '전체',
    ALL: '전체',
  }),
} as const satisfies Record<string, Record<string, Entry>>

export type LabelDomain = keyof typeof LABELS

/** 내부 값을 화면 문구로 바꾼다. 모르는 값은 코드를 노출하지 않고 UNKNOWN_LABEL을 돌려준다. */
export function label(domain: LabelDomain, value: string | null | undefined): string {
  if (value === null || value === undefined || value === '') return '-'
  const entry = (LABELS[domain] as Record<string, Entry>)[value]
  return entry ? entry.label : UNKNOWN_LABEL
}

export function toneOf(domain: LabelDomain, value: string | null | undefined): Tone {
  if (!value) return 'muted'
  return (LABELS[domain] as Record<string, Entry>)[value]?.tone ?? 'muted'
}

/** 흑백 환경에서도 상태를 구분할 수 있도록 색이 아닌 기호를 함께 쓴다. */
export const TONE_SYMBOL: Record<Tone, string> = {
  ok: '✓',
  info: '●',
  warn: '!',
  danger: '✕',
  muted: '○',
}

/** 선택 목록용 [값, 문구] 쌍. 값 순서는 호출한 쪽이 정한다. */
export function optionsOf(domain: LabelDomain, values: readonly string[]): Array<[string, string]> {
  return values.map((v) => [v, label(domain, v)])
}

/** 개발자용 오류 문구가 그대로 사용자에게 보이지 않도록 안전한 한국어 문구로 바꾼다. */
export function friendlyHttpError(status: number | undefined): string {
  if (status === 401) return '로그인이 만료되었습니다. 다시 로그인해 주세요.'
  if (status === 403) return '이 작업을 할 권한이 없습니다.'
  if (status === 404) return '요청한 정보를 찾을 수 없습니다.'
  if (status === 409) return '다른 작업과 충돌했습니다. 화면을 새로고침한 뒤 다시 시도해 주세요.'
  if (status === 422 || status === 400) return '입력한 내용을 확인해 주세요.'
  if (status === 429) return '요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.'
  if (status !== undefined && status >= 500) return '서버에서 문제가 발생했습니다. 잠시 후 다시 시도해 주세요.'
  return '요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.'
}

/**
 * 채널(쇼핑몰)이 보낸 주문 상태 문자열. 내부 주문 상태와 같은 값이면 한국어로 바꾸고,
 * 채널 고유 값이면 원문을 그대로 보여 준다(운영자가 채널 화면과 대조해야 하므로 숨기지 않는다).
 */
export function channelStatusText(value: string | null | undefined): string {
  if (!value) return '-'
  const known = (LABELS.orderStatus as Record<string, Entry>)[value]
  return known ? known.label : value
}
