import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

// UI 와이어프레임 v1.1 7장(Drill-down 공통 규칙)의 개발 지침에 따른 공통 래퍼.
// KPI 카드/그래프 데이터포인트/통계 수치/경고 스트립 등 어디서든 이 컴포넌트로
// "원본 목록 화면 + 동일 필터"로 이동하는 규칙을 강제한다.
interface DrillLinkProps {
  to: string
  filters?: Record<string, string | number | undefined>
  className?: string
  children: ReactNode
}

export function DrillLink({ to, filters, className, children }: DrillLinkProps) {
  const params = new URLSearchParams()
  if (filters) {
    for (const [key, value] of Object.entries(filters)) {
      if (value !== undefined && value !== '') params.set(key, String(value))
    }
  }
  const query = params.toString()
  return (
    <Link to={query ? `${to}?${query}` : to} className={className}>
      {children}
    </Link>
  )
}
