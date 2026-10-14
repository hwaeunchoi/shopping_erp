// 사이드바 메뉴 구조(대분류 > 소분류)와 현재 위치(브레드크럼) 계산의 단일 출처.
// 이 파일은 React에 의존하지 않는 순수 모듈이라 Node 테스트(`npm test`)에서 그대로 검증한다.
//
// 원칙
// - route는 App.tsx의 기존 경로를 그대로 쓴다(메뉴를 옮겨도 URL은 바뀌지 않는다).
// - permission은 백엔드 라우터가 요구하는 권한 코드와 같은 값이다. permission이 없는 항목은
//   로그인만 하면 보이고, 권한이 없는 항목은 숨긴다(백엔드가 403으로 막는 것과 같은 동작).
// - 실제 화면이 없는 항목은 만들지 않는다.

export interface NavItem {
  to: string
  label: string
  permission?: string
}

export interface NavGroup {
  id: string
  label: string
  items: NavItem[]
}

export const NAV_GROUPS: NavGroup[] = [
  {
    id: 'home',
    label: '홈',
    items: [{ to: '/', label: '대시보드' }],
  },
  {
    id: 'orders',
    label: '주문·배송',
    items: [
      { to: '/orders', label: '주문', permission: 'ORDER_VIEW' },
      { to: '/shipments', label: '배송', permission: 'SHIPMENT_VIEW' },
      { to: '/fulfillment', label: '출고', permission: 'SHIPMENT_VIEW' },
      { to: '/order-conflicts', label: '주문 상태 충돌', permission: 'ORDER_EDIT' },
    ],
  },
  {
    id: 'products',
    label: '상품·재고',
    items: [
      { to: '/products', label: '상품', permission: 'PRODUCT_MANAGE' },
      { to: '/products-bulk', label: '상품 일괄 처리', permission: 'PRODUCT_MANAGE' },
      { to: '/inventory', label: '재고', permission: 'INVENTORY_VIEW' },
      { to: '/suppliers', label: '공급처', permission: 'SUPPLIER_MANAGE' },
      { to: '/purchase-orders', label: '발주', permission: 'SUPPLIER_MANAGE' },
    ],
  },
  {
    id: 'support',
    label: '고객지원',
    items: [
      { to: '/cs-cases', label: '고객 문의', permission: 'CS_VIEW' },
      { to: '/exchanges', label: '교환', permission: 'EXCHANGE_RETURN_MANAGE' },
      { to: '/returns', label: '반품', permission: 'EXCHANGE_RETURN_MANAGE' },
      { to: '/cancellations', label: '취소', permission: 'EXCHANGE_RETURN_MANAGE' },
      { to: '/customers', label: '고객' },
    ],
  },
  {
    id: 'finance',
    label: '정산·매출',
    items: [
      { to: '/settlements', label: '정산', permission: 'SETTLEMENT_VIEW' },
      { to: '/analytics', label: '매출·손익 분석', permission: 'ANALYTICS_VIEW' },
      { to: '/costs', label: '비용', permission: 'COST_MANAGE' },
      { to: '/ads', label: '광고', permission: 'AD_MANAGE' },
      { to: '/reports', label: '보고서', permission: 'REPORT_VIEW' },
    ],
  },
  {
    id: 'operations',
    label: '연동·작업',
    items: [
      { to: '/operations', label: '운영 현황', permission: 'DASHBOARD_VIEW' },
      { to: '/unmatched-items', label: '미매칭 상품', permission: 'PRODUCT_MANAGE' },
      { to: '/system-monitoring', label: '작업 이력·시스템 상태', permission: 'SYSTEM_MONITOR_VIEW' },
      { to: '/notifications', label: '알림', permission: 'NOTIFICATION_VIEW' },
    ],
  },
  {
    id: 'settings',
    label: '설정',
    items: [{ to: '/settings', label: '설정', permission: 'SETTINGS_MANAGE' }],
  },
]

// 메뉴에는 없지만 실제로 존재하는 하위 화면(상세). 현재 위치 표시에만 쓴다.
export const DETAIL_ROUTES: Array<{ pattern: RegExp; parent: string; label: string }> = [
  { pattern: /^\/orders\/[^/]+$/, parent: '/orders', label: '주문 상세' },
  { pattern: /^\/products\/[^/]+$/, parent: '/products', label: '상품 상세' },
]

export function allNavItems(): NavItem[] {
  return NAV_GROUPS.flatMap((g) => g.items)
}

/** 권한에 맞는 메뉴만 남긴다. 항목이 하나도 없는 대분류는 통째로 숨긴다. */
export function visibleGroups(permissions: Iterable<string>): NavGroup[] {
  const granted = new Set(permissions)
  return NAV_GROUPS.map((g) => ({
    ...g,
    items: g.items.filter((item) => !item.permission || granted.has(item.permission)),
  })).filter((g) => g.items.length > 0)
}

function normalize(pathname: string): string {
  const path = pathname.split('?')[0].split('#')[0]
  return path.length > 1 ? path.replace(/\/+$/, '') : path
}

function matches(itemPath: string, pathname: string): boolean {
  if (itemPath === '/') return pathname === '/'
  return pathname === itemPath || pathname.startsWith(itemPath + '/')
}

export interface NavLocation {
  group: NavGroup
  item: NavItem
  detailLabel?: string
}

/** 현재 경로가 속한 대분류·소분류(와 상세 화면 이름)를 찾는다. 모르는 경로면 null. */
export function findNavLocation(pathname: string): NavLocation | null {
  const path = normalize(pathname)
  let best: NavLocation | null = null
  for (const group of NAV_GROUPS) {
    for (const item of group.items) {
      if (matches(item.to, path) && (!best || item.to.length > best.item.to.length)) {
        best = { group, item }
      }
    }
  }
  if (!best) return null
  const detail = DETAIL_ROUTES.find((d) => d.parent === best!.item.to && d.pattern.test(path))
  return detail ? { ...best, detailLabel: detail.label } : best
}
