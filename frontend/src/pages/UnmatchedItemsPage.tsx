import { useState } from 'react'
import { api, ApiError } from '../api/client'
import { useApiData } from '../api/useApiData'
import type { UnmatchedPlatformItem } from '../api/types'

export function UnmatchedItemsPage() {
  const { data, error, isLoading, reload } = useApiData<UnmatchedPlatformItem[]>(
    () => api.get('/api/products/unmatched-items'),
    [],
  )
  const [optionIdByItem, setOptionIdByItem] = useState<Record<number, string>>({})
  const [resolveError, setResolveError] = useState<string | null>(null)

  const handleResolve = async (item: UnmatchedPlatformItem) => {
    const optionId = Number(optionIdByItem[item.id])
    if (!optionId) {
      setResolveError('연결할 옵션 ID를 입력하세요.')
      return
    }
    setResolveError(null)
    try {
      await api.post(`/api/products/unmatched-items/${item.id}/resolve`, { product_option_id: optionId })
      reload()
    } catch (err) {
      setResolveError(err instanceof ApiError ? err.message : '연결 중 오류가 발생했습니다.')
    }
  }

  return (
    <div>
      <h2>미매칭 상품</h2>
      <p>
        자동매칭(플랫폼옵션번호/플랫폼상품번호/판매자상품코드, 실제 고유ID 기준)에 모두 실패한
        주문상품 목록입니다. 상품명/옵션명 유사도는 오탐 위험이 커 자동매칭에 쓰지 않습니다 -
        상품관리에서 옵션 ID를 확인한 뒤 아래에서 직접 연결하세요.
      </p>
      {isLoading && <p>불러오는 중...</p>}
      {error && <p className="form-error">{error}</p>}
      {resolveError && <p className="form-error">{resolveError}</p>}
      {data && (
        <table className="data-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>플랫폼ID</th>
              <th>주문번호</th>
              <th>플랫폼옵션번호</th>
              <th>상품명</th>
              <th>옵션명</th>
              <th>판매자상품코드</th>
              <th>수량</th>
              <th>단가</th>
              <th>연결할 옵션 ID</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {data.map((item) => (
              <tr key={item.id}>
                <td>{item.id}</td>
                <td>{item.platform_id}</td>
                <td>{item.platform_order_no ?? '-'}</td>
                <td>{item.platform_option_id ?? '-'}</td>
                <td>{item.product_name ?? '-'}</td>
                <td>{item.option_name ?? '-'}</td>
                <td>{item.seller_product_code ?? '-'}</td>
                <td>{item.quantity ?? '-'}</td>
                <td>{item.unit_price?.toLocaleString() ?? '-'}</td>
                <td>
                  <input
                    type="number"
                    min={1}
                    value={optionIdByItem[item.id] ?? ''}
                    onChange={(e) => setOptionIdByItem({ ...optionIdByItem, [item.id]: e.target.value })}
                    style={{ width: '6rem' }}
                  />
                </td>
                <td>
                  <button type="button" onClick={() => handleResolve(item)}>연결</button>
                </td>
              </tr>
            ))}
            {data.length === 0 && <tr><td colSpan={11}>미매칭 상품이 없습니다.</td></tr>}
          </tbody>
        </table>
      )}
    </div>
  )
}
