import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import type { AppNotification } from '../api/types'

// UI v1.0 알림센터: 상단바 알림 벨(🔔). 배지 숫자는 notifications.is_read=0 기준.
// 알림 클릭 → 읽음 처리 + 알림센터 화면으로 이동(UI v1.1 7장 Drill-down 공통 규칙).
export function NotificationBell() {
  const [isOpen, setIsOpen] = useState(false)
  const [items, setItems] = useState<AppNotification[]>([])
  const [unreadCount, setUnreadCount] = useState(0)
  const ref = useRef<HTMLDivElement>(null)
  const navigate = useNavigate()

  function loadUnreadCount() {
    api
      .get<{ unread_count: number }>('/api/notifications/unread-count')
      .then((res) => setUnreadCount(res.unread_count))
      .catch(() => {})
  }

  useEffect(() => {
    loadUnreadCount()
  }, [])

  function toggle() {
    const next = !isOpen
    setIsOpen(next)
    if (next) {
      api
        .get<AppNotification[]>('/api/notifications?unread_only=true')
        .then(setItems)
        .catch(() => setItems([]))
    }
  }

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setIsOpen(false)
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  async function handleSelect(notification: AppNotification) {
    setIsOpen(false)
    try {
      await api.patch(`/api/notifications/${notification.id}/read`)
    } catch {
      // 읽음 처리 실패해도 이동은 계속한다.
    }
    loadUnreadCount()
    navigate('/notifications')
  }

  return (
    <div className="topbar-dropdown" ref={ref}>
      <button type="button" className="topbar-icon-button notification-bell" onClick={toggle} title="알림">
        🔔{unreadCount > 0 && <span className="notification-badge">{unreadCount}</span>}
      </button>
      {isOpen && (
        <div className="dropdown-panel">
          {items.length === 0 && <div className="dropdown-empty">읽지 않은 알림이 없습니다.</div>}
          {items.map((n) => (
            <button key={n.id} type="button" className="dropdown-item notification-item" onClick={() => handleSelect(n)}>
              <span className={`notification-severity notification-severity-${n.severity.toLowerCase()}`}>
                {n.severity}
              </span>
              <span>{n.message}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
