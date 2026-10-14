// 한국어 표시 문구(labels.ts) 검증.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { channelStatusText, friendlyHttpError, LABELS, label, optionsOf, toneOf, TONE_SYMBOL, UNKNOWN_LABEL, type LabelDomain } from '../src/labels.ts'

const HANGUL = /[가-힣]/
// 한글 없이 허용하는 약어 표기
const ALLOWED_NON_KOREAN = new Set<string>()

test('요청된 기본 상태 문구가 그대로 매핑된다', () => {
  const expected: Array<[LabelDomain, string, string]> = [
    ['csStatus', 'OPEN', '접수'],
    ['csStatus', 'IN_PROGRESS', '처리 중'],
    ['csStatus', 'WAITING_CUSTOMER', '고객 답변 대기'],
    ['csStatus', 'WAITING_CHANNEL', '채널 처리 대기'],
    ['csStatus', 'RESOLVED', '해결 완료'],
    ['csStatus', 'CLOSED', '종결'],
    ['taskStatus', 'SUCCESS', '성공'],
    ['taskStatus', 'PARTIAL', '일부 완료'],
    ['taskStatus', 'PARTIAL_SUCCESS', '일부 완료'],
    ['taskStatus', 'FAILED', '실패'],
    ['taskStatus', 'ALREADY_RUNNING', '이미 실행 중'],
    ['taskStatus', 'NOT_REQUESTED', '요청 안 함'],
    ['taskStatus', 'UNSUPPORTED', '지원하지 않음'],
  ]
  for (const [domain, value, text] of expected) {
    assert.equal(label(domain, value), text, `${domain}.${value}`)
  }
})

test('백엔드 동기화 결과 계약의 모든 상태 값이 표시 문구를 가진다', () => {
  // 클레임·정산 동기화: SUCCESS | PARTIAL | UNSUPPORTED | FAILED (api/routers/orders.py, settlements.py)
  // CS 동기화: SUCCESS | PARTIAL_SUCCESS | FAILED | UNSUPPORTED | DISABLED | ALREADY_RUNNING (frontend/src/api/types.ts)
  const types = readFileSync(new URL('../src/api/types.ts', import.meta.url), 'utf8')
  const union = types.match(/status: ('SUCCESS'(?: \| '[A-Z_]+')+)/)
  assert.ok(union, 'types.ts 의 동기화 상태 union 을 찾지 못했습니다')
  const frontendStatuses = [...union![1].matchAll(/'([A-Z_]+)'/g)].map((m) => m[1])
  for (const status of [...frontendStatuses, 'SUCCESS', 'PARTIAL', 'UNSUPPORTED', 'FAILED']) {
    assert.notEqual(label('taskStatus', status), UNKNOWN_LABEL, `taskStatus.${status} 문구가 없습니다`)
  }
})

test('모든 표시 문구는 한국어이며 내부 코드와 같지 않다', () => {
  for (const [domain, map] of Object.entries(LABELS)) {
    for (const [key, entry] of Object.entries(map as Record<string, { label: string }>)) {
      assert.ok(entry.label.length > 0, `${domain}.${key} 문구가 비어 있습니다`)
      assert.notEqual(entry.label, key, `${domain}.${key} 는 내부 코드를 그대로 보여 줍니다`)
      assert.doesNotMatch(entry.label, /^[A-Z0-9_]+$/, `${domain}.${key}: ${entry.label}`)
      assert.ok(HANGUL.test(entry.label) || ALLOWED_NON_KOREAN.has(entry.label), `${domain}.${key} 에 한글이 없습니다: ${entry.label}`)
    }
  }
})

test('같은 의미의 값은 모든 화면(도메인)에서 같은 문구를 쓴다', () => {
  // 명령 상태 문구는 출고·상품 일괄 처리·상품 상세·운영 현황이 모두 이 한 곳을 쓴다.
  assert.equal(label('commandStatus', 'UNKNOWN'), '결과 확인 필요')
  assert.equal(label('commandStatus', 'RETRY_WAIT'), '재시도 대기')
  // 교환·반품·취소가 공유하는 값
  for (const value of ['REQUESTED', 'APPROVED', 'COMPLETED', 'REJECTED']) {
    assert.notEqual(label('claimStatus', value), UNKNOWN_LABEL)
  }
  assert.equal(label('orderClaim', 'CANCEL'), label('orderClaim', 'CANCELLATION'))
  assert.equal(label('shippingStatus', 'DELIVERED'), label('orderStatus', 'DELIVERED'))
  assert.equal(label('purchaseOrderStatus', 'CANCELLED'), label('commandStatus', 'CANCELLED'))
})

test('알 수 없는 값은 내부 코드를 노출하지 않는다', () => {
  assert.equal(label('csStatus', 'SOMETHING_NEW'), UNKNOWN_LABEL)
  assert.doesNotMatch(label('taskStatus', 'RAW_ENUM_VALUE'), /RAW_ENUM_VALUE/)
  assert.equal(label('csStatus', null), '-')
  assert.equal(label('csStatus', undefined), '-')
  assert.equal(label('csStatus', ''), '-')
  assert.equal(toneOf('csStatus', 'SOMETHING_NEW'), 'muted')
})

test('선택 목록은 값은 그대로 두고 문구만 한국어로 바꾼다', () => {
  assert.deepEqual(optionsOf('claimStatus', ['REQUESTED', 'COMPLETED']), [
    ['REQUESTED', '접수'],
    ['COMPLETED', '완료'],
  ])
})

test('상태 종류마다 기호가 달라 흑백에서도 구분된다', () => {
  const symbols = Object.values(TONE_SYMBOL)
  assert.equal(new Set(symbols).size, symbols.length)
  assert.equal(toneOf('csStatus', 'RESOLVED'), 'ok')
  assert.equal(toneOf('taskStatus', 'FAILED'), 'danger')
  assert.equal(toneOf('taskStatus', 'PARTIAL'), 'warn')
})

test('채널 상태 원문은 내부 상태와 같은 값만 한국어로 바꾼다', () => {
  assert.equal(channelStatusText('SHIPPING'), '배송 중')
  assert.equal(channelStatusText('CHANNEL_ONLY_VALUE'), 'CHANNEL_ONLY_VALUE')
  assert.equal(channelStatusText(null), '-')
})

test('오류 문구는 한국어이며 상태 코드별로 안전하다', () => {
  for (const status of [undefined, 400, 401, 403, 404, 409, 422, 429, 500, 502, 503]) {
    const text = friendlyHttpError(status)
    assert.match(text, HANGUL)
    assert.doesNotMatch(text, /[A-Za-z]{3,}|traceback|exception|http/i)
  }
  assert.notEqual(friendlyHttpError(403), friendlyHttpError(500))
})
