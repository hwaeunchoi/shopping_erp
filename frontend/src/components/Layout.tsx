import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Link, NavLink, useLocation } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { findNavLocation, visibleGroups } from '../navigation'
import { AIAssistantPanel } from './AIAssistantPanel'
import { ErrorBoundary } from './ErrorBoundary'
import { GlobalSearch } from './GlobalSearch'
import { Icon } from './Icon'
import { NotificationBell } from './NotificationBell'
import { UserMenu } from './UserMenu'
import { useScrollableRegions } from './useScrollableRegions'

// 왼쪽 사이드바(대분류 > 소분류) + 간결한 상단바.
// 메뉴 구조는 navigation.ts가 단일 출처이며 권한이 없는 메뉴는 보이지 않는다.
// 좁은 화면(<=900px)에서는 사이드바가 서랍(drawer)으로 바뀐다.
export function Layout({ children }: { children: ReactNode }) {
  const { user } = useAuth()
  const { pathname } = useLocation()
  const groups = visibleGroups(user?.permissions ?? [])
  const current = findNavLocation(pathname)
  const currentGroupId = current?.group.id
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [toggled, setToggled] = useState<Record<string, boolean>>({})
  const menuButtonRef = useRef<HTMLButtonElement>(null)
  const closeButtonRef = useRef<HTMLButtonElement>(null)
  const mainRef = useRef<HTMLElement>(null)
  useScrollableRegions(mainRef, [pathname])

  // 화면을 옮기면 서랍을 닫고, 현재 화면의 대분류는 자동으로 펼친다.
  useEffect(() => {
    setDrawerOpen(false)
    if (currentGroupId) setToggled((prev) => ({ ...prev, [currentGroupId]: true }))
  }, [pathname, currentGroupId])

  useEffect(() => {
    if (!drawerOpen) return
    closeButtonRef.current?.focus()
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        setDrawerOpen(false)
        menuButtonRef.current?.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [drawerOpen])

  function isGroupOpen(id: string): boolean {
    return toggled[id] ?? currentGroupId === id
  }

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">본문 바로가기</a>
      {drawerOpen && (
        <button type="button" className="drawer-overlay" aria-label="메뉴 닫기" onClick={() => setDrawerOpen(false)} />
      )}
      <aside id="app-sidebar" className={'sidebar' + (drawerOpen ? ' is-open' : '')} aria-label="주 메뉴">
        <div className="sidebar-head">
          <Link to="/" className="sidebar-title">쇼핑몰 통합 ERP</Link>
          <button
            ref={closeButtonRef}
            type="button"
            className="icon-button drawer-close"
            aria-label="메뉴 닫기"
            onClick={() => setDrawerOpen(false)}
          >
            <Icon name="close" />
          </button>
        </div>
        <nav>
          {groups.map((group) => {
            // 소분류가 하나뿐인 대분류(홈, 설정)는 펼침 없이 바로 이동하는 링크로 보여 준다.
            if (group.items.length === 1) {
              const only = group.items[0]
              return (
                <NavLink
                  key={group.id}
                  to={only.to}
                  end={only.to === '/'}
                  className={({ isActive }) => 'nav-group-single' + (isActive ? ' active' : '')}
                >
                  {group.label}
                </NavLink>
              )
            }
            const open = isGroupOpen(group.id)
            return (
              <div key={group.id} className="nav-group">
                <button
                  type="button"
                  className={'nav-group-button' + (currentGroupId === group.id ? ' has-current' : '')}
                  aria-expanded={open}
                  aria-controls={`nav-${group.id}`}
                  onClick={() => setToggled((prev) => ({ ...prev, [group.id]: !open }))}
                >
                  <span>{group.label}</span>
                  <span className={'nav-chevron' + (open ? ' is-open' : '')}>
                    <Icon name="chevronDown" size={14} />
                  </span>
                </button>
                {open && (
                  <ul id={`nav-${group.id}`} className="nav-sublist">
                    {group.items.map((item) => (
                      <li key={item.to}>
                        <NavLink
                          to={item.to}
                          end={item.to === '/'}
                          className={'nav-link' + (current?.item.to === item.to ? ' active' : '')}
                          aria-current={current?.item.to === item.to ? 'page' : undefined}
                        >
                          {item.label}
                        </NavLink>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )
          })}
        </nav>
      </aside>
      <div className="main-area">
        <header className="topbar">
          <button
            ref={menuButtonRef}
            type="button"
            className="icon-button menu-button"
            aria-label="메뉴 열기"
            aria-expanded={drawerOpen}
            aria-controls="app-sidebar"
            onClick={() => setDrawerOpen(true)}
          >
            <Icon name="menu" />
          </button>
          <GlobalSearch />
          <div className="topbar-right">
            <NotificationBell />
            <UserMenu />
          </div>
        </header>
        <main ref={mainRef} id="main-content" className="content" tabIndex={-1}>
          {current && (
            <nav className="breadcrumb" aria-label="현재 위치">
              <span>{current.group.label}</span>
              <span aria-hidden="true">/</span>
              {current.detailLabel ? (
                <>
                  <Link to={current.item.to}>{current.item.label}</Link>
                  <span aria-hidden="true">/</span>
                  <span aria-current="page">{current.detailLabel}</span>
                </>
              ) : (
                <span aria-current="page">{current.item.label}</span>
              )}
            </nav>
          )}
          <ErrorBoundary key={pathname}>{children}</ErrorBoundary>
        </main>
        <AIAssistantPanel />
      </div>
    </div>
  )
}
