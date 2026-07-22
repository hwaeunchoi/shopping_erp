import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import type { SearchResultItem, SearchResults } from '../api/types'

// UI 와이어프레임 v1.1 1장: 상단 중앙 고정 글로벌 통합검색.
// orders/products/product_options/customers/shipments를 동시 질의해
// 카테고리별로 드롭다운에 표시하고, 결과 클릭 시 상세로 이동 + recent_views 기록.
const CATEGORY_LABELS: Record<keyof SearchResults, string> = {
  orders: '주문',
  products: '상품',
  product_options: 'SKU',
  customers: '고객',
  shipments: '송장번호',
}

function resultPath(item: SearchResultItem): string {
  switch (item.type) {
    case 'ORDER':
      return `/orders/${item.id}`
    case 'PRODUCT':
      return `/products/${item.id}`
    case 'CUSTOMER':
      return '/customers'
    default:
      return item.type === 'PRODUCT_OPTION' ? '/products' : '/shipments'
  }
}

export function GlobalSearch() {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<SearchResults | null>(null)
  const [isOpen, setIsOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)
  const navigate = useNavigate()

  useEffect(() => {
    if (!query.trim()) {
      setResults(null)
      setIsOpen(false)
      return
    }
    const timer = setTimeout(() => {
      api
        .get<SearchResults>(`/api/search?q=${encodeURIComponent(query)}`)
        .then((data) => {
          setResults(data)
          setIsOpen(true)
        })
        .catch(() => setResults(null))
    }, 300)
    return () => clearTimeout(timer)
  }, [query])

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setIsOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  function handleSelect(item: SearchResultItem) {
    setIsOpen(false)
    setQuery('')
    if (item.type === 'ORDER' || item.type === 'PRODUCT') {
      api.post('/api/recent-views', { target_type: item.type, target_id: item.id }).catch(() => {})
    }
    navigate(resultPath(item))
  }

  const categoryEntries = results ? (Object.keys(CATEGORY_LABELS) as Array<keyof SearchResults>) : []
  const totalCount = results ? categoryEntries.reduce((sum, key) => sum + results[key].length, 0) : 0

  return (
    <div className="global-search" ref={containerRef}>
      <input
        type="text"
        placeholder="🔍 통합검색: 주문번호/상품명/SKU/고객명/송장번호..."
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onFocus={() => results && setIsOpen(true)}
      />
      {isOpen && results && (
        <div className="search-dropdown">
          {totalCount === 0 && <div className="dropdown-empty">검색 결과가 없습니다.</div>}
          {categoryEntries.map((key) => {
            const items = results[key]
            if (items.length === 0) return null
            return (
              <div key={key} className="search-category">
                <div className="search-category-label">
                  {CATEGORY_LABELS[key]} {items.length}건
                </div>
                {items.map((item) => (
                  <button
                    key={`${item.type}-${item.id}`}
                    type="button"
                    className="search-result-item"
                    onClick={() => handleSelect(item)}
                  >
                    <span>{item.label}</span>
                    {item.sublabel && <span className="search-sublabel">{item.sublabel}</span>}
                  </button>
                ))}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
