import { useEffect } from 'react'
import { api } from './client'

// UI 와이어프레임 v1.1 6장: 주문/상품/고객 상세 화면 진입 시 recent_views에 자동 기록한다.
export function useRecentView(targetType: string, targetId: number | string | undefined): void {
  useEffect(() => {
    if (!targetId) return
    api.post('/api/recent-views', { target_type: targetType, target_id: Number(targetId) }).catch(() => {
      // 최근 본 항목 기록 실패는 화면 흐름을 막지 않는다.
    })
  }, [targetType, targetId])
}
