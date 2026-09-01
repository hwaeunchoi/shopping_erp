import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'
import { useApiData } from '../api/useApiData'
import { Pagination } from '../components/Pagination'
import { SHIPMENT_STATUSES } from '../api/types'
import type { PaginatedResponse, Shipment } from '../api/types'

const PAGE_SIZE = 10

export function ShipmentsPage() {
  const [status, setStatus] = useState('')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)

  const query = new URLSearchParams()
  if (status) query.set('status_filter', status)
  if (search) query.set('search', search)
  query.set('page', String(page))
  query.set('page_size', String(PAGE_SIZE))

  const { data, error, isLoading, reload } = useApiData<PaginatedResponse<Shipment>>(
    () => api.get(`/api/shipments?${query.toString()}`),
    [status, search, page],
  )

  const [orderId, setOrderId] = useState('')
  const [carrier, setCarrier] = useState('')
  const [trackingNo, setTrackingNo] = useState('')
  const [createError, setCreateError] = useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)

  const [statusDrafts, setStatusDrafts] = useState<Record<number, { status: string; warehouseId: string }>>({})
  const [statusError, setStatusError] = useState<string | null>(null)

  // 상용 ERP 확장(1단계) - 채널(네이버/쿠팡)로 송장 전송. 결과는 shipment.id별로만 보관한다.
  const [submitResults, setSubmitResults] = useState<Record<number, string>>({})
  const [submittingId, setSubmittingId] = useState<number | null>(null)

  const handleSubmitToChannel = async (shipmentId: number) => {
    setSubmittingId(shipmentId)
    setSubmitResults((prev) => ({ ...prev, [shipmentId]: '' }))
    try {
      const result = await api.post<{ command_id: number; status: string; already_processed: boolean }>(
        `/api/shipments/${shipmentId}/submit`,
        {},
      )
      setSubmitResults((prev) => ({
        ...prev,
        [shipmentId]: result.already_processed ? '이미 전송됨(SUCCESS)' : `전송 완료(${result.status})`,
      }))
      reload()
    } catch (err) {
      setSubmitResults((prev) => ({
        ...prev,
        [shipmentId]: err instanceof ApiError ? `실패: ${err.message}` : '전송 중 오류가 발생했습니다.',
      }))
    } finally {
      setSubmittingId(null)
    }
  }

  const handleCreate = async (e: FormEvent) => {
    e.preventDefault()
    if (!orderId) {
      setCreateError('주문 ID를 입력하세요.')
      return
    }
    setCreateError(null)
    setIsSubmitting(true)
    try {
      await api.post('/api/shipments', {
        order_id: Number(orderId),
        carrier: carrier || null,
        tracking_no: trackingNo || null,
      })
      setOrderId('')
      setCarrier('')
      setTrackingNo('')
      reload()
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.message : '배송 등록 중 오류가 발생했습니다.')
    } finally {
      setIsSubmitting(false)
    }
  }

  const handleStatusChange = async (shipmentId: number) => {
    const draft = statusDrafts[shipmentId]
    if (!draft?.status) {
      setStatusError('변경할 상태를 선택하세요.')
      return
    }
    setStatusError(null)
    try {
      await api.patch(`/api/shipments/${shipmentId}/status`, {
        status: draft.status,
        warehouse_id: draft.warehouseId ? Number(draft.warehouseId) : null,
      })
      reload()
    } catch (err) {
      setStatusError(err instanceof ApiError ? err.message : '상태 변경 중 오류가 발생했습니다.')
    }
  }

  return (
    <div>
      <h2>배송관리</h2>

      <form className="inline-form" onSubmit={handleCreate}>
        <input
          type="number"
          value={orderId}
          onChange={(e) => setOrderId(e.target.value)}
          placeholder="주문 ID"
          min={1}
        />
        <input value={carrier} onChange={(e) => setCarrier(e.target.value)} placeholder="운송사" />
        <input value={trackingNo} onChange={(e) => setTrackingNo(e.target.value)} placeholder="송장번호" />
        <button type="submit" disabled={isSubmitting}>{isSubmitting ? '등록 중...' : '배송 등록'}</button>
      </form>
      {createError && <p className="form-error">{createError}</p>}

      <div className="filter-bar">
        <input
          value={search}
          onChange={(e) => {
            setSearch(e.target.value)
            setPage(1)
          }}
          placeholder="운송사/송장번호 검색"
        />
        {['', ...SHIPMENT_STATUSES].map((s) => (
          <button
            key={s || 'all'}
            type="button"
            className={status === s ? 'active' : ''}
            onClick={() => {
              setStatus(s)
              setPage(1)
            }}
          >
            {s || '전체'}
          </button>
        ))}
      </div>

      {statusError && <p className="form-error">{statusError}</p>}
      {isLoading && <p>불러오는 중...</p>}
      {error && <p className="form-error">{error}</p>}
      {data && (
        <>
          <table className="data-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>주문ID</th>
                <th>운송사</th>
                <th>송장번호</th>
                <th>상태</th>
                <th>발송일시</th>
                <th>배송완료일시</th>
                <th>상태 변경</th>
                <th>채널 전송</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((s) => {
                const draft = statusDrafts[s.id] ?? { status: '', warehouseId: '' }
                return (
                  <tr key={s.id}>
                    <td>{s.id}</td>
                    <td>{s.order_id}</td>
                    <td>{s.carrier ?? '-'}</td>
                    <td>{s.tracking_no ?? '-'}</td>
                    <td><span className="status-badge">{s.status}</span></td>
                    <td>{s.shipped_at ? new Date(s.shipped_at).toLocaleString() : '-'}</td>
                    <td>{s.delivered_at ? new Date(s.delivered_at).toLocaleString() : '-'}</td>
                    <td>
                      <div className="inline-form" style={{ margin: 0 }}>
                        <select
                          value={draft.status}
                          onChange={(e) =>
                            setStatusDrafts({ ...statusDrafts, [s.id]: { ...draft, status: e.target.value } })
                          }
                        >
                          <option value="">상태 선택</option>
                          {SHIPMENT_STATUSES.map((st) => (
                            <option key={st} value={st}>{st}</option>
                          ))}
                        </select>
                        <input
                          type="number"
                          value={draft.warehouseId}
                          onChange={(e) =>
                            setStatusDrafts({ ...statusDrafts, [s.id]: { ...draft, warehouseId: e.target.value } })
                          }
                          placeholder="창고ID"
                          min={1}
                          style={{ width: 70 }}
                        />
                        <button type="button" onClick={() => handleStatusChange(s.id)}>변경</button>
                      </div>
                    </td>
                    <td>
                      <button
                        type="button"
                        disabled={s.status !== 'READY' || submittingId === s.id}
                        onClick={() => handleSubmitToChannel(s.id)}
                        title={s.status !== 'READY' ? 'READY 상태의 배송만 전송할 수 있습니다.' : undefined}
                      >
                        {submittingId === s.id ? '전송 중...' : '전송'}
                      </button>
                      {submitResults[s.id] && <div className="form-hint">{submitResults[s.id]}</div>}
                    </td>
                  </tr>
                )
              })}
              {data.items.length === 0 && (
                <tr><td colSpan={9}>등록된 배송이 없습니다.</td></tr>
              )}
            </tbody>
          </table>
          <Pagination page={data.page} pageSize={data.page_size} total={data.total} onPageChange={setPage} />
        </>
      )}
    </div>
  )
}
