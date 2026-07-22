import { useState, type FormEvent } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import { useApiData } from '../api/useApiData'
import { useRecentView } from '../api/useRecentView'
import type { OrderDetail, OrderMemo } from '../api/types'

export function OrderDetailPage() {
  const { orderId } = useParams<{ orderId: string }>()
  useRecentView('ORDER', orderId)
  const { data, error, isLoading } = useApiData<OrderDetail>(() => api.get(`/api/orders/${orderId}`), [orderId])

  const { data: memos, reload: reloadMemos } = useApiData<OrderMemo[]>(
    () => api.get(`/api/orders/${orderId}/memos`),
    [orderId],
  )
  const [memoContent, setMemoContent] = useState('')
  const [memoError, setMemoError] = useState<string | null>(null)
  const [isSubmittingMemo, setIsSubmittingMemo] = useState(false)

  const handleAddMemo = async (e: FormEvent) => {
    e.preventDefault()
    if (!memoContent.trim()) {
      setMemoError('메모 내용을 입력하세요.')
      return
    }
    setMemoError(null)
    setIsSubmittingMemo(true)
    try {
      await api.post(`/api/orders/${orderId}/memos`, { content: memoContent })
      setMemoContent('')
      reloadMemos()
    } catch (err) {
      setMemoError(err instanceof ApiError ? err.message : '메모 등록 중 오류가 발생했습니다.')
    } finally {
      setIsSubmittingMemo(false)
    }
  }

  return (
    <div>
      <p><Link to="/orders">← 주문 목록으로</Link></p>
      <h2>주문 상세 #{orderId}</h2>
      {isLoading && <p>불러오는 중...</p>}
      {error && <p className="form-error">{error}</p>}
      {data && (
        <>
          <dl className="detail-grid">
            <dt>주문번호</dt><dd>{data.platform_order_no}</dd>
            <dt>상태</dt><dd>{data.status}</dd>
            <dt>주문일시</dt><dd>{new Date(data.order_date).toLocaleString()}</dd>
            <dt>구매자명</dt><dd>{data.customer_name ?? '-'}</dd>
            <dt>구매자 연락처</dt><dd>{data.customer_phone ?? '-'}</dd>
            <dt>상품별 총 주문금액</dt><dd>{data.total_amount.toLocaleString()}</dd>
            <dt>할인금액</dt><dd>{data.discount_amount.toLocaleString()}</dd>
          </dl>

          <h3>주문상품</h3>
          <table className="data-table">
            <thead>
              <tr>
                <th>상품명</th>
                <th>옵션정보</th>
                <th>판매자 내부코드1(SKU)</th>
                <th>수량</th>
                <th>단가</th>
                <th>금액</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((item) => (
                <tr key={item.id}>
                  <td>{item.product_name ?? '-'}</td>
                  <td>{item.option_name ?? '-'}</td>
                  <td>{item.sku_code ?? '-'}</td>
                  <td>{item.quantity}</td>
                  <td>{item.unit_price.toLocaleString()}</td>
                  <td>{item.line_amount.toLocaleString()}</td>
                </tr>
              ))}
              {data.items.length === 0 && (
                <tr><td colSpan={6}>주문상품이 없습니다(상품 매핑 없이 수집된 주문일 수 있습니다 - 상품관리에서 해당 SKU의 플랫폼 매핑을 등록해주세요).</td></tr>
              )}
            </tbody>
          </table>

          <h3>메모</h3>
          <form className="inline-form" onSubmit={handleAddMemo}>
            <input
              value={memoContent}
              onChange={(e) => setMemoContent(e.target.value)}
              placeholder="이 주문에 대한 메모를 입력하세요"
            />
            <button type="submit" disabled={isSubmittingMemo}>{isSubmittingMemo ? '등록 중...' : '메모 추가'}</button>
          </form>
          {memoError && <p className="form-error">{memoError}</p>}
          <ul className="memo-list">
            {memos?.map((memo) => (
              <li key={memo.id}>
                <span>{memo.content}</span>
                <span className="memo-meta">{new Date(memo.created_at).toLocaleString()}</span>
              </li>
            ))}
            {memos && memos.length === 0 && <li>등록된 메모가 없습니다.</li>}
          </ul>
        </>
      )}
    </div>
  )
}
