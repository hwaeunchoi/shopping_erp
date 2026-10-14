// 메뉴 구조·권한·현재 위치 계산 검증. Node 내장 테스트 러너로 실행한다(`npm test`).
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { allNavItems, DETAIL_ROUTES, findNavLocation, NAV_GROUPS, visibleGroups } from '../src/navigation.ts'

const HANGUL = /[가-힣]/

// 개편 전(625c049) 메뉴의 route -> 권한. 개편 후에도 route·권한이 그대로여야 한다.
const ORIGINAL_ROUTE_PERMISSIONS: Record<string, string | undefined> = {
  '/': undefined,
  '/operations': 'DASHBOARD_VIEW',
  '/orders': 'ORDER_VIEW',
  '/shipments': 'SHIPMENT_VIEW',
  '/fulfillment': 'SHIPMENT_VIEW',
  '/cs-cases': 'CS_VIEW',
  '/order-conflicts': 'ORDER_EDIT',
  '/exchanges': 'EXCHANGE_RETURN_MANAGE',
  '/returns': 'EXCHANGE_RETURN_MANAGE',
  '/cancellations': 'EXCHANGE_RETURN_MANAGE',
  '/products': 'PRODUCT_MANAGE',
  '/products-bulk': 'PRODUCT_MANAGE',
  '/unmatched-items': 'PRODUCT_MANAGE',
  '/customers': undefined,
  '/inventory': 'INVENTORY_VIEW',
  '/suppliers': 'SUPPLIER_MANAGE',
  '/purchase-orders': 'SUPPLIER_MANAGE',
  '/settlements': 'SETTLEMENT_VIEW',
  '/costs': 'COST_MANAGE',
  '/ads': 'AD_MANAGE',
  '/analytics': 'ANALYTICS_VIEW',
  '/reports': 'REPORT_VIEW',
  '/system-monitoring': 'SYSTEM_MONITOR_VIEW',
  '/notifications': 'NOTIFICATION_VIEW',
  '/settings': 'SETTINGS_MANAGE',
}

function appRoutes(): string[] {
  const source = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')
  return [...source.matchAll(/<Route path="([^"]+)"/g)].map((m) => m[1])
}

test('대분류는 7개 이하, 소분류는 대분류마다 5개 이하다', () => {
  assert.ok(NAV_GROUPS.length <= 7)
  for (const group of NAV_GROUPS) {
    assert.ok(group.items.length >= 1, `${group.label}에 소분류가 없습니다`)
    assert.ok(group.items.length <= 5, `${group.label} 소분류가 5개를 넘습니다`)
  }
})

test('같은 route·같은 이름의 메뉴가 중복되지 않는다', () => {
  const items = allNavItems()
  assert.equal(new Set(items.map((i) => i.to)).size, items.length, 'route 중복')
  assert.equal(new Set(items.map((i) => i.label)).size, items.length, '메뉴 이름 중복')
  assert.equal(new Set(NAV_GROUPS.map((g) => g.id)).size, NAV_GROUPS.length, '대분류 id 중복')
})

test('모든 메뉴 이름은 한국어이고 개발 용어를 쓰지 않는다', () => {
  for (const group of NAV_GROUPS) {
    assert.match(group.label, HANGUL)
    for (const item of group.items) {
      assert.match(item.label, HANGUL, `${item.to}: ${item.label}`)
      assert.doesNotMatch(item.label, /[A-Z]{3,}|_|API|enum/i)
    }
  }
})

test('기존 route와 권한이 그대로 유지된다', () => {
  const byRoute = new Map(allNavItems().map((i) => [i.to, i.permission]))
  assert.deepEqual([...byRoute.keys()].sort(), Object.keys(ORIGINAL_ROUTE_PERMISSIONS).sort())
  for (const [route, permission] of Object.entries(ORIGINAL_ROUTE_PERMISSIONS)) {
    assert.equal(byRoute.get(route), permission, `${route} 권한이 바뀌었습니다`)
  }
})

test('죽은 링크가 없다: 모든 메뉴 route가 App.tsx에 있고, 화면 route는 메뉴나 상세 경로로 도달 가능하다', () => {
  const routes = appRoutes()
  for (const item of allNavItems()) {
    assert.ok(routes.includes(item.to), `${item.to}에 해당하는 화면이 없습니다`)
  }
  const detailParents = new Set(DETAIL_ROUTES.map((d) => d.parent))
  const navRoutes = new Set(allNavItems().map((i) => i.to))
  for (const route of routes) {
    if (route === '/login' || route === '*') continue
    const base = route.replace(/\/:[^/]+$/, '')
    assert.ok(navRoutes.has(route) || (route !== base && detailParents.has(base)), `${route}는 메뉴에서 도달할 수 없습니다`)
  }
  assert.ok(routes.includes('*'), '404 화면 route가 없습니다')
})

test('권한이 없으면 해당 메뉴가 숨겨지고, 빈 대분류는 사라진다', () => {
  const none = visibleGroups([])
  const noneRoutes = none.flatMap((g) => g.items.map((i) => i.to)).sort()
  assert.deepEqual(noneRoutes, ['/', '/customers'], '권한 없는 사용자는 로그인만으로 보이는 메뉴만 본다')
  assert.ok(none.every((g) => g.items.length > 0))

  const orderViewer = visibleGroups(['ORDER_VIEW'])
  assert.ok(orderViewer.flatMap((g) => g.items).some((i) => i.to === '/orders'))
  assert.ok(!orderViewer.flatMap((g) => g.items).some((i) => i.to === '/settings'))
  assert.ok(!orderViewer.some((g) => g.id === 'settings'), '설정 권한이 없으면 설정 대분류가 없어야 한다')

  const all = new Set(allNavItems().flatMap((i) => (i.permission ? [i.permission] : [])))
  assert.equal(visibleGroups(all).flatMap((g) => g.items).length, allNavItems().length)
})

test('현재 위치: 대분류·소분류·상세 화면을 찾는다', () => {
  assert.equal(findNavLocation('/')?.item.label, '대시보드')
  assert.equal(findNavLocation('/cancellations')?.group.id, 'support')
  assert.equal(findNavLocation('/cancellations/')?.item.to, '/cancellations')
  assert.equal(findNavLocation('/orders?status_filter=NEW')?.item.to, '/orders')
  const detail = findNavLocation('/orders/12')
  assert.equal(detail?.item.to, '/orders')
  assert.equal(detail?.detailLabel, '주문 상세')
  assert.equal(findNavLocation('/products/3')?.detailLabel, '상품 상세')
  assert.equal(findNavLocation('/products-bulk')?.item.to, '/products-bulk', '/products 가 /products-bulk 를 가로채면 안 된다')
  assert.equal(findNavLocation('/없는-화면'), null)
  for (const item of allNavItems()) {
    assert.equal(findNavLocation(item.to)?.item.to, item.to)
  }
})

test('페이지 제목(h2)이 사이드바 메뉴 이름과 같다', () => {
  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')
  const imports = new Map([...app.matchAll(/import \{ (\w+) \} from '\.\/pages\/(\w+)'/g)].map((m) => [m[1], m[2]]))
  let checked = 0
  for (const item of allNavItems()) {
    const re = new RegExp(`path="${item.to === '/' ? '/' : item.to}" element=\\{<Protected><(\\w+) />`)
    const component = app.match(re)?.[1]
    assert.ok(component, `${item.to} 화면 컴포넌트를 찾지 못했습니다`)
    const page = readFileSync(new URL(`../src/pages/${imports.get(component!)}.tsx`, import.meta.url), 'utf8')
    const titles = [...page.matchAll(/<h2[^>]*>([^<{]*)<\/h2>/g)].map((m) => m[1].trim())
    assert.ok(titles.includes(item.label), `${item.to}: 페이지 제목 ${JSON.stringify(titles)} 에 메뉴 이름 "${item.label}" 이 없습니다`)
    checked++
  }
  assert.equal(checked, allNavItems().length)
})
