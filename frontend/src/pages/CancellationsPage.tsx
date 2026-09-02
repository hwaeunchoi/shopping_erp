import { useState, type FormEvent } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import { downloadBlob } from '../api/download'
import { useApiData } from '../api/useApiData'
import { Pagination } from '../components/Pagination'
import { CANCELLATION_STATUSES } from '../api/types'
import type { Cancellation, PaginatedResponse } from '../api/types'

const PAGE_SIZE = 10

function todayIso(): string {
  return new Date().toISOString().slice(0, 10)
}

export function CancellationsPage() {
  const [initialParams] = useSearchParams()
  const [status, setStatus] = useState(initialParams.get('status_filter') ?? '')
  const [search, setSearch] = useState('')
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')
  const [page, setPage] = useState(1)

  const query = new URLSearchParams()
  if (status) query.set('status_filter', status)
  if (search) query.set('search', search)
  if (startDate) query.set('start_date', startDate)
  if (endDate) query.set('end_date', endDate)
  query.set('page', String(page))
  query.set('page_size', String(PAGE_SIZE))

  const { data, error, isLoading, reload } = useApiData<PaginatedResponse<Cancellation>>(
    () => api.get(`/api/cancellations?${query.toString()}`),
    [status, search, startDate, endDate, page],
  )

  const [orderId, setOrderId] = useState('')
  const [reason, setReason] = useState('')
  const [refundAmount, setRefundAmount] = useState('')
  const [createError, setCreateError] = useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)

  const [statusDrafts, setStatusDrafts] = useState<Record<number, { status: string; warehouseId: string }>>({})
  const [statusError, setStatusError] = useState<string | null>(null)

  const [exportError, setExportError] = useState<string | null>(null)
  const [isExporting, setIsExporting] = useState(false)

  const handleExport = async () => {
    setExportError(null)
    setIsExporting(true)
    try {
      await downloadBlob(`/api/cancellations/export?${query.toString()}`, `cancellations_${todayIso()}.xlsx`)
    } catch (err) {
      setExportError(err instanceof ApiError ? err.message : '엑셀 다운로드 중 오류가 발생했습니다.')
    } finally {
      setIsExporting(false)
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
      await api.post('/api/cancellations', {
        order_id: Number(orderId),
        reason: reason || null,
        refund_amount: refundAmount ? Number(refundAmount) : null,
      })
      setOrderId('')
      setReason('')
      setRefundAmount('')
      reload()
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.message : '취소 신청 등록 중 오류가 발생했습니다.')
    } finally {
      setIsSubmitting(false)
    }
  }

  const handleStatusChange = async (cancellationId: number) => {
    const draft = statusDrafts[cancellationId]
    if (!draft?.status) {
      setStatusError('변경할 상태를 선택하세요.')
      return
    }
    setStatusError(null)
    try {
      await api.patch(`/api/cancellations/${cancellationId}/status`, {
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
      <h2>취소관리</h2>

      <form className="inline-form" onSubmit={handleCreate}>
        <input type="number" value={orderId} onChange={(e) => setOrderId(e.target.value)} placeholder="주문 ID" min={1} />
        <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="취소 사유" />
        <input
          type="number"
          value={refundAmount}
          onChange={(e) => setRefundAmount(e.target.value)}
          placeholder="환불금액"
          min={0}
        />
        <button type="submit" disabled={isSubmitting}>{isSubmitting ? '등록 중...' : '취소 신청'}</button>
      </form>
      {createError && <p className="form-error">{createError}</p>}

      <div className="filter-bar">
        <input value={search} onChange={(e) => { setSearch(e.target.value); setPage(1) }} placeholder="사유 검색" />
        <input type="date" value={startDate} onChange={(e) => { setStartDate(e.target.value); setPage(1) }} />
        <span>~</span>
        <input type="date" value={endDate} onChange={(e) => { setEndDate(e.target.value); setPage(1) }} />
        {['', ...CANCELLATION_STATUSES].map((s) => (
          <button
            key={s || 'all'}
            type="button"
            className={status === s ? 'active' : ''}
            onClick={() => { setStatus(s); setPage(1) }}
          >
            {s || '전체'}
          </button>
        ))}
        <button type="button" onClick={handleExport} disabled={isExporting}>
          {isExporting ? '다운로드 중...' : '엑셀 다운로드'}
        </button>
      </div>
      {exportError && <p className="form-error">{exportError}</p>}

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
                <th>사유</th>
                <th>환불금액</th>
                <th>상태</th>
                <th>채널 클레임ID</th>
                <th>채널 원본상태</th>
                <th>귀책</th>
                <th>신청일시</th>
                <th>상태 변경</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((c) => {
                const draft = statusDrafts[c.id] ?? { status: '', warehouseId: '' }
                return (
                  <tr key={c.id}>
                    <td>{c.id}</td>
                    <td>{c.order_id}</td>
                    <td>{c.reason ?? '-'}</td>
                    <td>{c.refund_amount != null ? c.refund_amount.toLocaleString() : '-'}</td>
                    <td><span className="status-badge">{c.status}</span></td>
                    <td>{c.platform_claim_id ?? '-'}</td>
                    <td>{c.raw_status ?? '-'}</td>
                    <td>{c.fault_type ?? '-'}</td>
                    <td>{new Date(c.requested_at).toLocaleString()}</td>
                    <td>
                      <div className="inline-form" style={{ margin: 0 }}>
                        <select
                          value={draft.status}
                          onChange={(e) =>
                            setStatusDrafts({ ...statusDrafts, [c.id]: { ...draft, status: e.target.value } })
                          }
                        >
                          <option value="">상태 선택</option>
                          {CANCELLATION_STATUSES.map((st) => (
                            <option key={st} value={st}>{st}</option>
                          ))}
                        </select>
                        <input
                          type="number"
                          value={draft.warehouseId}
                          onChange={(e) =>
                            setStatusDrafts({ ...statusDrafts, [c.id]: { ...draft, warehouseId: e.target.value } })
                          }
                          placeholder="창고ID"
                          min={1}
                          style={{ width: 70 }}
                        />
                        <button type="button" onClick={() => handleStatusChange(c.id)}>변경</button>
                      </div>
                    </td>
                  </tr>
                )
              })}
              {data.items.length === 0 && (
                <tr><td colSpan={10}>등록된 취소 신청이 없습니다.</td></tr>
              )}
            </tbody>
          </table>
          <Pagination page={data.page} pageSize={data.page_size} total={data.total} onPageChange={setPage} />
        </>
      )}
    </div>
  )
}
