import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { Favorite } from '../api/types'

// UI 와이어프레임 v1.1 1장/5장: 상단바 즐겨찾기(⭐) 드롭다운.
function targetPath(item: Favorite): string {
  if (item.target_type === 'PRODUCT') return `/products/${item.target_id}`
  if (item.target_type === 'REPORT') return '/analytics'
  return '/'
}

const TYPE_LABELS: Record<string, string> = { PRODUCT: '상품', REPORT: '보고서', CUSTOMER: '고객' }

export function FavoritesDropdown() {
  const [isOpen, setIsOpen] = useState(false)
  const [items, setItems] = useState<Favorite[]>([])
  const ref = useRef<HTMLDivElement>(null)

  function toggle() {
    const next = !isOpen
    setIsOpen(next)
    if (next) {
      api.get<Favorite[]>('/api/favorites').then(setItems).catch(() => setItems([]))
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
        ⭐ 즐겨찾기
      </button>
      {isOpen && (
        <div className="dropdown-panel">
          {items.length === 0 && <div className="dropdown-empty">즐겨찾기한 항목이 없습니다.</div>}
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
