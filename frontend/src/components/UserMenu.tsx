import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { Favorite, RecentView } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { label } from '../labels'
import { Icon } from './Icon'

// 상단 우측의 사용자 메뉴. 자주 쓰지 않는 부가 기능(최근 본 항목, 즐겨찾기, 화면 모드, 로그아웃)을
// 한 곳에 모아 상단바를 단순하게 유지한다. 열릴 때만 목록을 불러온다.
function recentPath(item: RecentView): string {
  if (item.target_type === 'ORDER') return `/orders/${item.target_id}`
  if (item.target_type === 'PRODUCT') return `/products/${item.target_id}`
  return '/customers'
}

function favoritePath(item: Favorite): string {
  if (item.target_type === 'PRODUCT') return `/products/${item.target_id}`
  if (item.target_type === 'REPORT') return '/analytics'
  return '/'
}

export function UserMenu() {
  const { user, logout, toggleTheme } = useAuth()
  const [isOpen, setIsOpen] = useState(false)
  const [recent, setRecent] = useState<RecentView[]>([])
  const [favorites, setFavorites] = useState<Favorite[]>([])
  const ref = useRef<HTMLDivElement>(null)
  const isDark = user?.theme_preference === 'DARK'

  function toggle() {
    const next = !isOpen
    setIsOpen(next)
    if (next) {
      api.get<RecentView[]>('/api/recent-views').then(setRecent).catch(() => setRecent([]))
      api.get<Favorite[]>('/api/favorites').then(setFavorites).catch(() => setFavorites([]))
    }
  }

  useEffect(() => {
    if (!isOpen) return
    function onMouseDown(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setIsOpen(false)
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') setIsOpen(false)
    }
    document.addEventListener('mousedown', onMouseDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onMouseDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [isOpen])

  return (
    <div className="topbar-dropdown" ref={ref}>
      <button
        type="button"
        className="user-menu-button"
        onClick={toggle}
        aria-haspopup="true"
        aria-expanded={isOpen}
        aria-label={`사용자 메뉴, ${user?.name ?? ''}`}
      >
        <Icon name="user" />
        <span className="user-menu-name">{user?.name}</span>
        <Icon name="chevronDown" size={14} />
      </button>
      {isOpen && (
        <div className="dropdown-panel user-menu-panel">
          <div className="user-menu-id">
            <strong>{user?.name}</strong>
            <span>{user?.username}</span>
          </div>
          <div className="dropdown-section-title">최근 본 항목</div>
          {recent.length === 0 && <div className="dropdown-empty">최근 본 항목이 없습니다.</div>}
          {recent.map((item) => (
            <Link key={item.id} to={recentPath(item)} className="dropdown-item" onClick={() => setIsOpen(false)}>
              {label('recordTarget', item.target_type)} #{item.target_id}
            </Link>
          ))}
          <div className="dropdown-section-title">즐겨찾기</div>
          {favorites.length === 0 && <div className="dropdown-empty">즐겨찾기한 항목이 없습니다.</div>}
          {favorites.map((item) => (
            <Link key={item.id} to={favoritePath(item)} className="dropdown-item" onClick={() => setIsOpen(false)}>
              {item.target_type === 'REPORT' ? '보고서' : label('recordTarget', item.target_type)} #{item.target_id}
            </Link>
          ))}
          <div className="dropdown-section-title">계정</div>
          <button type="button" className="dropdown-item" onClick={toggleTheme}>
            <Icon name={isDark ? 'sun' : 'moon'} size={16} />
            {isDark ? '밝은 화면으로 전환' : '어두운 화면으로 전환'}
          </button>
          <button type="button" className="dropdown-item" onClick={logout}>
            로그아웃
          </button>
        </div>
      )}
    </div>
  )
}
