import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { RecentView } from '../api/types'

// UI 와이어프레임 v1.1 1장/6장: 상단바 최근 본 항목(🕐) 드롭다운, 최신 10건.
function targetPath(item: RecentView): string {
  if (item.target_type === 'ORDER') return `/orders/${item.target_id}`
  if (item.target_type === 'PRODUCT') return `/products/${item.target_id}`
  return '/customers'
}

const TYPE_LABELS: Record<string, string> = { ORDER: '주문', PRODUCT: '상품', CUSTOMER: '고객' }

export function RecentViewsDropdown() {
  const [isOpen, setIsOpen] = useState(false)
  const [items, setItems] = useState<RecentView[]>([])
  const ref = useRef<HTMLDivElement>(null)

  function toggle() {
    const next = !isOpen
    setIsOpen(next)
    if (next) {
      api.get<RecentView[]>('/api/recent-views').then(setItems).catch(() => setItems([]))
    }
  }

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setIsOpen(false)
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  return (
    <div className="topbar-dropdown" ref={ref}>
      <button type="button" className="topbar-icon-button" onClick={toggle}>
        🕐 최근본항목
      </button>
      {isOpen && (
        <div className="dropdown-panel">
          {items.length === 0 && <div className="dropdown-empty">최근 본 항목이 없습니다.</div>}
          {items.map((item) => (
            <Link key={item.id} to={targetPath(item)} className="dropdown-item" onClick={() => setIsOpen(false)}>
              [{TYPE_LABELS[item.target_type] ?? item.target_type}] #{item.target_id}
            </Link>
          ))}
        </div>
      )}
    </div>
  )
}
