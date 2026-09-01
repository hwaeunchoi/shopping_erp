import { useState } from 'react'
import { api, ApiError } from '../api/client'
import { useApiData } from '../api/useApiData'
import type { OrderStatusConflict } from '../api/types'

// 상용 ERP 확장(1단계) - 채널/내부 주문상태가 허용된 전이로 설명되지 않을 때
// 자동으로 어느 한쪽을 정답으로 덮어쓰지 않고 여기서 운영자가 확인/해소한다.
export function OrderConflictsPage() {
  const { data, error, isLoading, reload } = useApiData<OrderStatusConflict[]>(
    () => api.get('/api/order-conflicts'),
    [],
  )

  const [resolvingId, setResolvingId] = useState<number | null>(null)
  const [resolveError, setResolveError] = useState<string | null>(null)

  const handleResolve = async (conflictId: number, resolution: 'ACCEPT_CHANNEL' | 'KEEP_INTERNAL') => {
    setResolvingId(conflictId)
    setResolveError(null)
    try {
      await api.post(`/api/order-conflicts/${conflictId}/resolve`, { resolution })
      reload()
    } catch (err) {
      setResolveError(err instanceof ApiError ? err.message : '충돌 해소 중 오류가 발생했습니다.')
    } finally {
      setResolvingId(null)
    }
  }

  return (
    <div>
      <h2>주문상태 충돌</h2>
      <p className="form-hint">
        채널에서 수집한 주문상태가 내부 상태에서 허용되지 않는 전이일 때 여기 남습니다.
        채널 상태 채택 시 내부 상태를 강제로 덮어쓰고(상태머신 우회, 감사로그 기록), 내부 상태 유지 시 채널 값은 폐기됩니다.
      </p>

      {resolveError && <p className="form-error">{resolveError}</p>}
      {isLoading && <p>불러오는 중...</p>}
      {error && <p className="form-error">{error}</p>}
      {data && (
        <table className="data-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>주문ID</th>
              <th>내부 상태</th>
              <th>채널 상태</th>
              <th>감지일시</th>
              <th>해소</th>
            </tr>
          </thead>
          <tbody>
            {data.map((c) => (
              <tr key={c.id}>
                <td>{c.id}</td>
                <td>{c.order_id}</td>
                <td><span className="status-badge">{c.internal_status}</span></td>
                <td><span className="status-badge">{c.channel_status}</span></td>
                <td>{new Date(c.detected_at).toLocaleString()}</td>
                <td>
                  <div className="inline-form" style={{ margin: 0 }}>
                    <button
                      type="button"
                      disabled={resolvingId === c.id}
                      onClick={() => handleResolve(c.id, 'ACCEPT_CHANNEL')}
                    >
                      채널 상태 채택
                    </button>
                    <button
                      type="button"
                      disabled={resolvingId === c.id}
                      onClick={() => handleResolve(c.id, 'KEEP_INTERNAL')}
                    >
                      내부 상태 유지
                    </button>
                  </div>
                </td>
              </tr>
            ))}
            {data.length === 0 && (
              <tr><td colSpan={6}>미해소 충돌이 없습니다.</td></tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  )
}
