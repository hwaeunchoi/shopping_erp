// 행 작업 메뉴(더보기) 로직과 적용 범위 검증.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { defaultConfirmMessage, nextEnabledIndex, placeMenu, visibleActions, type MenuAction } from '../src/rowActionMenu.ts'

const noop = () => {}
const act = (key: string, extra: Partial<MenuAction> = {}): MenuAction => ({ key, label: key, onSelect: noop, ...extra })

test('권한이 없어 hidden 인 작업은 표시하지 않는다', () => {
  const shown = visibleActions([act('edit'), act('delete', { danger: true, hidden: true }), act('copy')])
  assert.deepEqual(shown.map((a) => a.key), ['edit', 'copy'])
})

test('위험 작업은 항상 일반 작업 뒤(메뉴 맨 아래)에 온다', () => {
  const shown = visibleActions([act('delete', { danger: true }), act('edit'), act('purge', { danger: true }), act('copy')])
  assert.deepEqual(shown.map((a) => a.key), ['edit', 'copy', 'delete', 'purge'])
})

test('위험 작업의 기본 확인 문구가 있다', () => {
  assert.match(defaultConfirmMessage(act('x', { label: '삭제', danger: true })), /삭제.*진행하시겠습니까/)
})

test('방향키·Home·End 이동은 비활성 항목을 건너뛰고 순환한다', () => {
  const disabled = [false, true, false, false]
  assert.equal(nextEnabledIndex(0, 'ArrowDown', disabled), 2)
  assert.equal(nextEnabledIndex(3, 'ArrowDown', disabled), 0)
  assert.equal(nextEnabledIndex(0, 'ArrowUp', disabled), 3)
  assert.equal(nextEnabledIndex(2, 'Home', disabled), 0)
  assert.equal(nextEnabledIndex(0, 'End', disabled), 3)
  assert.equal(nextEnabledIndex(0, 'ArrowDown', [true, true]), -1)
  assert.equal(nextEnabledIndex(0, 'ArrowDown', []), -1)
})

test('메뉴는 모바일 viewport 밖으로 나가지 않는다', () => {
  const viewport = { width: 390, height: 844 }
  // 오른쪽 끝 버튼
  const right = placeMenu({ left: 330, top: 500, right: 380, bottom: 540 }, { width: 220, height: 120 }, viewport)
  assert.ok(right.left >= 8 && right.left + 220 <= 390 - 8, JSON.stringify(right))
  // 왼쪽 끝 버튼
  const left = placeMenu({ left: 0, top: 500, right: 40, bottom: 540 }, { width: 220, height: 120 }, viewport)
  assert.ok(left.left >= 8)
  // 아래 공간 부족하면 위로 열림
  const bottom = placeMenu({ left: 330, top: 800, right: 380, bottom: 840 }, { width: 220, height: 160 }, viewport)
  assert.ok(bottom.top + 160 <= 844, JSON.stringify(bottom))
  assert.ok(bottom.top < 800)
  // viewport 보다 넓은 메뉴는 폭이 줄어든다
  const wide = placeMenu({ left: 10, top: 100, right: 50, bottom: 140 }, { width: 600, height: 100 }, viewport)
  assert.ok(wide.left >= 8 && wide.left + Math.min(600, 390 - 16) <= 390)
})

test('실제 화면 적용: 표 한 행에 3개 이상 나열된 작업 버튼이 남아 있지 않다', () => {
  const pages = ['ProductsPage', 'NotificationsPage', 'ReportsPage', 'ProductDetailPage']
  for (const page of pages) {
    const source = readFileSync(new URL(`../src/pages/${page}.tsx`, import.meta.url), 'utf8').replace(/\r\n/g, '\n')
    assert.ok(source.includes('<RowActionMenu'), `${page} 가 행 작업 메뉴(<RowActionMenu>)를 쓰지 않습니다`)
  }
  // 모든 표 셀(td)에 버튼이 3개 이상 있는 곳이 없어야 한다(전수 검사).
  // FulfillmentPage 는 제외한다: 한 셀의 버튼 4개는 출고 항목 상태(READY/PICKING/VERIFYING/PACKED)별로 하나만 조건부 표시되는 단계 전환 버튼이다.
  const dir = new URL('../src/pages/', import.meta.url)
  for (const name of ['AdsPage', 'CsCasesPage', 'InventoryPage', 'NotificationsPage', 'OperationsDashboardPage', 'OrdersPage', 'ProductBulkPage', 'ProductDetailPage', 'ProductsPage', 'PurchaseOrdersPage', 'ReportsPage', 'SettingsPage', 'SuppliersPage']) {
    const source = readFileSync(new URL(`${name}.tsx`, dir), 'utf8').replace(/\r\n/g, '\n')
    for (const m of source.matchAll(/<td[^>]*>((?:(?!<\/td>)[\s\S])*)<\/td>/g)) {
      const buttons = (m[1].match(/<button\b/g) || []).length
      assert.ok(buttons < 3, `${name}: 한 셀에 버튼 ${buttons}개`)
    }
  }
})

test('더보기 메뉴: 위험 작업은 skipConfirm 이 아닌 한 확인창을 거친다', () => {
  const component = readFileSync(new URL('../src/components/RowActionMenu.tsx', import.meta.url), 'utf8')
  assert.match(component, /action\.danger && !action\.skipConfirm && !window\.confirm/)
  for (const needle of ['aria-haspopup="menu"', 'aria-expanded', 'role="menu"', 'role="menuitem"', 'aria-disabled', 'row-action-reason']) {
    assert.ok(component.includes(needle), `RowActionMenu 에 ${needle} 이 없습니다`)
  }
  // skipConfirm 은 자체 확인창이 있는 처리 함수에만 쓴다.
  for (const [file, needle] of [['ProductsPage', 'handleDelete'], ['ProductDetailPage', 'handleDeleteOption']]) {
    const src = readFileSync(new URL(`../src/pages/${file}.tsx`, import.meta.url), 'utf8')
    assert.ok(src.includes(`const ${needle} = async`) && /window\.confirm\(`[^`]*삭제하시겠습니까/.test(src), `${file}.${needle} 에 자체 확인창이 없습니다`)
  }
})

test('더보기 메뉴: Esc 로 닫고 버튼으로 초점을 돌려준다', () => {
  const component = readFileSync(new URL('../src/components/RowActionMenu.tsx', import.meta.url), 'utf8')
  assert.match(component, /e\.key === 'Escape'[\s\S]{0,120}close\(true\)/, 'Esc 가 메뉴를 닫고 초점을 복귀시켜야 합니다')
})

test('더보기 메뉴: 바깥 클릭과 Tab 으로 닫힌다', () => {
  const component = readFileSync(new URL('../src/components/RowActionMenu.tsx', import.meta.url), 'utf8')
  assert.match(component, /document\.addEventListener\('mousedown', onMouseDown\)/, '바깥 클릭 닫기 리스너가 없습니다')
  assert.match(component, /document\.removeEventListener\('mousedown', onMouseDown\)/, '리스너 정리가 없습니다')
  assert.match(component, /e\.key === 'Tab'[\s\S]{0,40}setOpen\(false\)/, 'Tab 으로 닫혀야 합니다')
})

test('보고서 구분 이름은 자유 문자열 계약을 그대로 유지한다(서버 값 변환 없음)', () => {
  const page = readFileSync(new URL('../src/pages/ReportsPage.tsx', import.meta.url), 'utf8')
  assert.match(page, /report_type: 'PROFIT_REPORT'/, '기본 전송 값이 바뀌었습니다')
  assert.match(page, /value=\{form\.report_type\}/)
  assert.doesNotMatch(page, /label\('report(Type|Kind)'/, '보고서 종류를 한국어 라벨로 바꿔 전송하면 안 됩니다')
  // 백엔드가 자유 문자열을 받는다는 근거(스키마)도 함께 확인한다.
  const schema = readFileSync(new URL('../../api/routers/reports.py', import.meta.url), 'utf8')
  assert.match(schema, /class ReportScheduleCreate\(BaseModel\):\s+report_type: str/)
})
