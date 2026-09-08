import type { ReactNode } from 'react'
import { NavLink } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { AIAssistantPanel } from './AIAssistantPanel'
import { FavoritesDropdown } from './FavoritesDropdown'
import { GlobalSearch } from './GlobalSearch'
import { NotificationBell } from './NotificationBell'
import { RecentViewsDropdown } from './RecentViewsDropdown'

// permission이 없는 메뉴(대시보드/고객관리)는 로그인만 하면 누구나 볼 수 있다.
// 나머지는 백엔드 라우터가 요구하는 권한 코드와 동일한 값을 매칭해 role에 없는
// 메뉴는 사이드바에서 아예 숨긴다(백엔드가 403으로 막는 것과 동일하게 동작).
const NAV_ITEMS: Array<{ to: string; label: string; permission?: string }> = [
  { to: '/', label: '대시보드' },
  { to: '/orders', label: '주문관리', permission: 'ORDER_VIEW' },
  { to: '/shipments', label: '배송관리', permission: 'SHIPMENT_VIEW' },
  { to: '/fulfillment', label: '출고관리', permission: 'SHIPMENT_VIEW' },
  { to: '/order-conflicts', label: '주문상태 충돌', permission: 'ORDER_EDIT' },
  { to: '/exchanges', label: '교환관리', permission: 'EXCHANGE_RETURN_MANAGE' },
  { to: '/returns', label: '반품관리', permission: 'EXCHANGE_RETURN_MANAGE' },
  { to: '/cancellations', label: '취소관리', permission: 'EXCHANGE_RETURN_MANAGE' },
  { to: '/products', label: '상품관리', permission: 'PRODUCT_MANAGE' },
  { to: '/products-bulk', label: '상품 대량처리', permission: 'PRODUCT_MANAGE' },
  { to: '/unmatched-items', label: '미매칭 상품', permission: 'PRODUCT_MANAGE' },
  { to: '/customers', label: '고객관리' },
  { to: '/inventory', label: '재고관리', permission: 'INVENTORY_VIEW' },
  { to: '/suppliers', label: '공급처관리', permission: 'SUPPLIER_MANAGE' },
  { to: '/purchase-orders', label: '발주관리', permission: 'SUPPLIER_MANAGE' },
  { to: '/settlements', label: '정산관리', permission: 'SETTLEMENT_VIEW' },
  { to: '/costs', label: '비용관리', permission: 'COST_MANAGE' },
  { to: '/ads', label: '광고관리', permission: 'AD_MANAGE' },
  { to: '/analytics', label: '매출/손익분석', permission: 'ANALYTICS_VIEW' },
  { to: '/reports', label: '보고서', permission: 'REPORT_VIEW' },
  { to: '/system-monitoring', label: '시스템 모니터링', permission: 'SYSTEM_MONITOR_VIEW' },
  { to: '/notifications', label: '알림센터', permission: 'NOTIFICATION_VIEW' },
  { to: '/settings', label: '설정', permission: 'SETTINGS_MANAGE' },
]

export function Layout({ children }: { children: ReactNode }) {
  const { user, logout, toggleTheme } = useAuth()
  const permissions = new Set(user?.permissions ?? [])
  const visibleNavItems = NAV_ITEMS.filter((item) => !item.permission || permissions.has(item.permission))

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-title">쇼핑몰 통합 ERP</div>
        <nav>
          {visibleNavItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === '/'}
              className={({ isActive }) => 'nav-link' + (isActive ? ' active' : '')}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <div className="main-area">
        <header className="topbar">
          <GlobalSearch />
          <div className="topbar-right">
            <NotificationBell />
            <RecentViewsDropdown />
            <FavoritesDropdown />
            <button type="button" className="topbar-icon-button" onClick={toggleTheme} title="다크모드 전환">
              {user?.theme_preference === 'DARK' ? '☀️' : '🌙'}
            </button>
            <span>{user?.name} ({user?.username})</span>
            <button type="button" onClick={logout}>로그아웃</button>
          </div>
        </header>
        <main className="content">{children}</main>
        <AIAssistantPanel />
      </div>
    </div>
  )
}
