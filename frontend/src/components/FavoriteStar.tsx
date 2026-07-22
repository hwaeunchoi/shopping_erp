import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { Favorite } from '../api/types'

// UI 와이어프레임 v1.1 5장: 상품관리 목록/상세 등에 배치하는 즐겨찾기 토글 아이콘.
export function FavoriteStar({ targetType, targetId }: { targetType: string; targetId: number }) {
  const [isFavorited, setIsFavorited] = useState<boolean | null>(null)

  useEffect(() => {
    api
      .get<Favorite[]>(`/api/favorites?target_type=${encodeURIComponent(targetType)}`)
      .then((favs) => setIsFavorited(favs.some((f) => f.target_id === targetId)))
      .catch(() => setIsFavorited(false))
  }, [targetType, targetId])

  async function toggle() {
    try {
      const res = await api.post<{ is_favorited: boolean }>('/api/favorites/toggle', {
        target_type: targetType,
        target_id: targetId,
      })
      setIsFavorited(res.is_favorited)
    } catch {
      // 실패해도 화면 흐름을 막지 않는다(즐겨찾기는 부가 기능).
    }
  }

  return (
    <button type="button" className="favorite-toggle" onClick={toggle} title={isFavorited ? '즐겨찾기 해제' : '즐겨찾기 추가'}>
      {isFavorited ? '⭐' : '☆'}
    </button>
  )
}
