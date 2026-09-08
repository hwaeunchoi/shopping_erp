import { useMemo, useState } from 'react'
import { api, ApiError } from '../api/client'
import { useApiData } from '../api/useApiData'
import type {
  CarrierOption,
  FulfillableOrderItem,
  FulfillmentBatch,
  FulfillmentBatchDetail,
  FulfillmentBatchItem,
  FulfillmentHistoryEntry,
  FulfillmentItemOutcome,
  FulfillmentProgressItem,
  FulfillmentWarehouse,
  Platform,
} from '../api/types'

// 상용 ERP 확장(5단계, A묶음) - 출고 배치(피킹/검수/포장/송장등록) 화면. 기존
// 단건 배송 화면(ShipmentsPage)은 그대로 두고, 이 화면은 새 백엔드
// (api/routers/fulfillment.py -> services.fulfillment_service)만 사용한다.
// 채널 전송 확정(SUCCESS 여부)은 여기서 판정하지 않는다 - 진행상태 표는 항상
// command_status(ExternalCommand)를 그대로 보여주고, SUCCESS만 전송완료로 본다.

const OUTCOME_LABELS: Record<string, string> = {
  ACCEPTED: '접수됨',
  ALREADY_PROCESSED: '이미 처리됨(중복 요청)',
  BLOCKED: '막힘',
  VALIDATION_FAILED: '검증 실패',
  FAILED_TO_ENQUEUE: '접수 실패',
}

const STATUS_LABELS: Record<string, string> = {
  READY: '출고대기',
  PICKING: '피킹중',
  PICKED: '피킹완료',
  VERIFYING: '검수중',
  VERIFIED: '검수완료',
  PACKED: '포장완료',
  SUBMIT_PENDING: '전송접수중',
  SUBMITTED: '전송접수됨',
  BLOCKED: '막힘',
  CANCELLED: '취소됨',
}

const COMMAND_STATUS_LABELS: Record<string, string> = {
  PENDING: '전송 대기',
  RUNNING: '전송 중',
  SUCCESS: '채널 전송 완료',
  FAILED: '전송 실패',
  RETRY_WAIT: '재시도 대기',
  UNKNOWN: '확인 필요(운영자 확인 대상)',
  CANCELLED: '취소됨(더 최신 요청으로 대체)',
}

const BATCH_STATUS_FILTERS = ['ALL', 'READY', 'PICKING', 'PICKED', 'VERIFYING', 'VERIFIED', 'PACKED', 'CLOSED']

export function FulfillmentPage() {
  const [platformId, setPlatformId] = useState<number | ''>('')
  const [warehouseId, setWarehouseId] = useState<number | ''>('')
  const [keyword, setKeyword] = useState('')
  const [keywordInput, setKeywordInput] = useState('')
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [qtyByOrderItemId, setQtyByOrderItemId] = useState<Record<number, string>>({})
  const [confirming, setConfirming] = useState(false)
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const [activeBatchId, setActiveBatchId] = useState<number | null>(null)
  const [batchStatusFilter, setBatchStatusFilter] = useState('ALL')

  const { data: warehouses } = useApiData<FulfillmentWarehouse[]>(() => api.get('/api/fulfillment/warehouses'), [])
  const { data: platforms } = useApiData<Platform[]>(() => api.get('/api/platforms'), [])

  const itemsQuery = new URLSearchParams()
  if (platformId) itemsQuery.set('platform_id', String(platformId))
  if (keyword) itemsQuery.set('search', keyword)

  const {
    data: fulfillableItems,
    error: itemsError,
    isLoading: itemsLoading,
    reload: reloadItems,
  } = useApiData<FulfillableOrderItem[]>(
    () => api.get(`/api/fulfillment/fulfillable-order-items?${itemsQuery.toString()}`),
    [platformId, keyword],
  )
  const rows = useMemo(() => fulfillableItems ?? [], [fulfillableItems])

  const batchesQuery = new URLSearchParams()
  if (batchStatusFilter !== 'ALL') batchesQuery.set('status_filter', batchStatusFilter)
  const {
    data: batches,
    isLoading: batchesLoading,
    reload: reloadBatches,
  } = useApiData<FulfillmentBatch[]>(() => api.get(`/api/fulfillment/batches?${batchesQuery.toString()}`), [
    batchStatusFilter,
  ])

  const toggle = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }
  const toggleAll = () => {
    setSelected((prev) => (prev.size === rows.length ? new Set() : new Set(rows.map((r) => r.order_item_id))))
  }

  const selectedRows = rows.filter((r) => selected.has(r.order_item_id))

  // 서버가 최종 검증을 하지만(신뢰하지 않음), 잔여수량 초과/0 이하 입력은 요청을
  // 보내기 전에 화면에서 바로 안내한다 - 굳이 400 응답을 한 바퀴 왕복할 필요가 없다.
  const qtyErrorByOrderItemId = useMemo(() => {
    const errors: Record<number, string> = {}
    for (const row of selectedRows) {
      const raw = qtyByOrderItemId[row.order_item_id] ?? String(row.remaining_quantity)
      const value = Number(raw)
      if (!raw.trim() || !Number.isFinite(value) || value <= 0) {
        errors[row.order_item_id] = '1 이상의 수량을 입력하세요.'
      } else if (value > row.remaining_quantity) {
        errors[row.order_item_id] = `잔여수량(${row.remaining_quantity})을 초과했습니다.`
      }
    }
    return errors
  }, [selectedRows, qtyByOrderItemId])
  const hasQtyError = Object.keys(qtyErrorByOrderItemId).length > 0

  const runCreate = async () => {
    if (creating) return
    if (!warehouseId) {
      setCreateError('창고를 선택해야 합니다.')
      return
    }
    if (hasQtyError) {
      setCreateError('출고 요청수량을 확인하세요(1 이상, 잔여수량 이하).')
      return
    }
    setCreating(true)
    setCreateError(null)
    try {
      const selections = selectedRows.map((r) => ({
        order_item_id: r.order_item_id,
        quantity: Number(qtyByOrderItemId[r.order_item_id] ?? r.remaining_quantity),
      }))
      const detail = await api.post<FulfillmentBatchDetail>('/api/fulfillment/batches', {
        warehouse_id: warehouseId,
        selections,
      })
      setSelected(new Set())
      setQtyByOrderItemId({})
      setConfirming(false)
      reloadItems()
      reloadBatches()
      setActiveBatchId(detail.batch.id)
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.message : '출고 배치 생성 중 오류가 발생했습니다.')
    } finally {
      setCreating(false)
    }
  }

  return (
    <div className="fulfillment-page">
      <h2>출고 관리</h2>
      <p className="hint-text">
        출고대기 주문라인을 배치로 묶어 피킹-검수-포장-송장등록-채널전송 접수까지 처리합니다. 실제 채널 HTTP
        전송은 여기서 하지 않고 기존 outbox로 접수만 합니다.
      </p>

      <h3>출고 배치 생성</h3>
      <div className="filter-bar">
        <select value={platformId} onChange={(e) => setPlatformId(e.target.value ? Number(e.target.value) : '')}>
          <option value="">전체 채널</option>
          {(platforms ?? []).map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <select value={warehouseId} onChange={(e) => setWarehouseId(e.target.value ? Number(e.target.value) : '')}>
          <option value="">창고 선택</option>
          {(warehouses ?? []).map((w) => (
            <option key={w.id} value={w.id}>
              {w.name}
            </option>
          ))}
        </select>
        <form
          onSubmit={(e) => {
            e.preventDefault()
            setKeyword(keywordInput.trim())
          }}
        >
          <input
            value={keywordInput}
            onChange={(e) => setKeywordInput(e.target.value)}
            placeholder="주문번호/상품명/SKU 검색"
          />
          <button type="submit">검색</button>
        </form>
      </div>

      {itemsError && <p className="form-error">{itemsError}</p>}
      {itemsLoading ? (
        <p>불러오는 중...</p>
      ) : (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>
                  <input
                    type="checkbox"
                    checked={rows.length > 0 && selected.size === rows.length}
                    onChange={toggleAll}
                  />
                </th>
                <th>주문번호</th>
                <th>상품명</th>
                <th>SKU</th>
                <th>주문수량</th>
                <th>잔여수량</th>
                <th>출고 요청수량</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.order_item_id} className={selected.has(row.order_item_id) ? 'sel' : ''}>
                  <td>
                    <input
                      type="checkbox"
                      checked={selected.has(row.order_item_id)}
                      onChange={() => toggle(row.order_item_id)}
                    />
                  </td>
                  <td>{row.platform_order_no}</td>
                  <td>{row.product_name || '-'}</td>
                  <td>{row.sku_code || '-'}</td>
                  <td>{row.order_quantity}</td>
                  <td>{row.remaining_quantity}</td>
                  <td>
                    <input
                      type="number"
                      min={1}
                      max={row.remaining_quantity}
                      value={qtyByOrderItemId[row.order_item_id] ?? String(row.remaining_quantity)}
                      onChange={(e) =>
                        setQtyByOrderItemId((prev) => ({ ...prev, [row.order_item_id]: e.target.value }))
                      }
                      style={{ width: 80 }}
                    />
                    {selected.has(row.order_item_id) && qtyErrorByOrderItemId[row.order_item_id] && (
                      <div className="form-error">{qtyErrorByOrderItemId[row.order_item_id]}</div>
                    )}
                  </td>
                </tr>
              ))}
              {rows.length === 0 && (
                <tr>
                  <td colSpan={7}>출고 가능한 주문라인이 없습니다.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {selected.size > 0 && !confirming && (
        <div className="bulk-bar">
          <span className="count">{selected.size}건 선택됨</span>
          <button type="button" onClick={() => setConfirming(true)}>
            출고 배치 생성
          </button>
          <button type="button" onClick={() => setSelected(new Set())}>
            선택 해제
          </button>
        </div>
      )}

      {confirming && (
        <div className="bulk-bar confirm-panel">
          <div>
            <strong>{selected.size}건으로 출고 배치를 생성합니다.</strong>
            <p className="hint-text">창고: {(warehouses ?? []).find((w) => w.id === warehouseId)?.name ?? '(미선택)'}</p>
            {hasQtyError && <p className="form-error">출고 요청수량을 확인하세요(위 표에 빨간 안내가 있는 행).</p>}
          </div>
          <button type="button" disabled={creating || hasQtyError || !warehouseId} onClick={runCreate}>
            {creating ? '생성 중...' : '생성 실행'}
          </button>
          <button type="button" onClick={() => setConfirming(false)}>
            취소
          </button>
        </div>
      )}
      {createError && <p className="form-error">{createError}</p>}

      <h3>출고 배치 목록</h3>
      <div className="filter-bar">
        <select value={batchStatusFilter} onChange={(e) => setBatchStatusFilter(e.target.value)}>
          {BATCH_STATUS_FILTERS.map((s) => (
            <option key={s} value={s}>
              {s === 'ALL' ? '전체' : s}
            </option>
          ))}
        </select>
        <button type="button" onClick={reloadBatches}>
          새로고침
        </button>
      </div>
      {batchesLoading ? (
        <p>불러오는 중...</p>
      ) : (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>배치 ID</th>
                <th>창고</th>
                <th>상태</th>
                <th>생성시각</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {(batches ?? []).map((b) => (
                <tr key={b.id} className={activeBatchId === b.id ? 'sel' : ''}>
                  <td>{b.id}</td>
                  <td>{(warehouses ?? []).find((w) => w.id === b.warehouse_id)?.name ?? b.warehouse_id}</td>
                  <td>
                    <span className="status-badge">{b.status}</span>
                  </td>
                  <td>{new Date(b.created_at).toLocaleString()}</td>
                  <td>
                    <button type="button" onClick={() => setActiveBatchId(b.id)}>
                      상세 보기
                    </button>
                  </td>
                </tr>
              ))}
              {(batches ?? []).length === 0 && (
                <tr>
                  <td colSpan={5}>배치가 없습니다.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {activeBatchId !== null && (
        <BatchDetailPanel
          batchId={activeBatchId}
          onClose={() => setActiveBatchId(null)}
          onBatchesChanged={reloadBatches}
        />
      )}
    </div>
  )
}

function BatchDetailPanel({
  batchId,
  onClose,
  onBatchesChanged,
}: {
  batchId: number
  onClose: () => void
  onBatchesChanged: () => void
}) {
  const [pickedQtyById, setPickedQtyById] = useState<Record<number, string>>({})
  const [verifiedQtyById, setVerifiedQtyById] = useState<Record<number, string>>({})
  const [actionError, setActionError] = useState<string | null>(null)
  const [pendingId, setPendingId] = useState<number | null>(null)

  const [packSelected, setPackSelected] = useState<Set<number>>(new Set())
  const [carrier, setCarrier] = useState('')
  const [trackingNo, setTrackingNo] = useState('')
  const [packing, setPacking] = useState(false)
  const [packResults, setPackResults] = useState<FulfillmentItemOutcome[] | null>(null)

  const [submitting, setSubmitting] = useState(false)
  const [submitResults, setSubmitResults] = useState<FulfillmentItemOutcome[] | null>(null)

  const [retrySelected, setRetrySelected] = useState<Set<number>>(new Set())
  const [retrying, setRetrying] = useState(false)

  const { data: carriers } = useApiData<CarrierOption[]>(() => api.get('/api/fulfillment/carriers'), [])

  const {
    data: detail,
    error: detailError,
    isLoading: detailLoading,
    reload: reloadDetail,
  } = useApiData<FulfillmentBatchDetail>(() => api.get(`/api/fulfillment/batches/${batchId}`), [batchId])

  const {
    data: progress,
    reload: reloadProgress,
  } = useApiData<FulfillmentProgressItem[]>(() => api.get(`/api/fulfillment/batches/${batchId}/progress`), [batchId])

  const {
    data: history,
    reload: reloadHistory,
  } = useApiData<FulfillmentHistoryEntry[]>(() => api.get(`/api/fulfillment/batches/${batchId}/history`), [batchId])

  const items = useMemo(() => detail?.items ?? [], [detail])

  const reloadAll = () => {
    reloadDetail()
    reloadProgress()
    reloadHistory()
    onBatchesChanged()
  }

  const runTransition = async (item: FulfillmentBatchItem, path: string, body: Record<string, unknown>) => {
    if (pendingId !== null) return
    setPendingId(item.id)
    setActionError(null)
    try {
      await api.post(`/api/fulfillment/batch-items/${item.id}/${path}`, { expected_status: item.status, ...body })
      reloadAll()
    } catch (err) {
      setActionError(
        err instanceof ApiError
          ? `${err.message}${err.status === 409 ? ' (다른 사용자가 이미 처리했습니다 - 새로고침합니다)' : ''}`
          : '처리 중 오류가 발생했습니다.',
      )
      reloadAll()
    } finally {
      setPendingId(null)
    }
  }

  const completePickingIfValid = (item: FulfillmentBatchItem) => {
    const raw = pickedQtyById[item.id] ?? String(item.requested_quantity)
    const value = Number(raw)
    if (!raw.trim() || !Number.isFinite(value) || value <= 0 || value > item.requested_quantity) {
      setActionError(`피킹 수량은 1 이상 요청수량(${item.requested_quantity}) 이하여야 합니다.`)
      return
    }
    runTransition(item, 'complete-picking', { picked_quantity: value })
  }

  const completeVerificationIfValid = (item: FulfillmentBatchItem) => {
    const maxAllowed = item.picked_quantity ?? item.requested_quantity
    const raw = verifiedQtyById[item.id] ?? String(maxAllowed)
    const value = Number(raw)
    if (raw.trim() === '' || !Number.isFinite(value) || value < 0 || value > maxAllowed) {
      setActionError(`검수 수량은 0 이상 피킹수량(${maxAllowed}) 이하여야 합니다.`)
      return
    }
    runTransition(item, 'complete-verification', { verified_quantity: value })
  }

  const togglePack = (id: number) => {
    setPackSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const runPack = async () => {
    if (packing) return
    setPacking(true)
    setActionError(null)
    try {
      const outcomes = await api.post<FulfillmentItemOutcome[]>('/api/fulfillment/batch-items/pack', {
        batch_item_ids: [...packSelected],
        carrier,
        tracking_no: trackingNo,
      })
      setPackResults(outcomes)
      setPackSelected(new Set())
      reloadAll()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : '포장 처리 중 오류가 발생했습니다.')
    } finally {
      setPacking(false)
    }
  }

  const packedShipmentIds = useMemo(
    () => Array.from(new Set(items.filter((i) => i.status === 'PACKED' && i.shipment_id).map((i) => i.shipment_id as number))),
    [items],
  )

  const runSubmit = async () => {
    if (submitting) return
    setSubmitting(true)
    setActionError(null)
    try {
      const outcomes = await api.post<FulfillmentItemOutcome[]>('/api/fulfillment/shipments/submit', {
        shipment_ids: packedShipmentIds,
      })
      setSubmitResults(outcomes)
      reloadAll()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : '채널 전송 접수 중 오류가 발생했습니다.')
    } finally {
      setSubmitting(false)
    }
  }

  const failedProgressItems = (progress ?? []).filter((p) => p.command_status === 'FAILED')
  const unknownProgressItems = (progress ?? []).filter((p) => p.command_status === 'UNKNOWN')

  const toggleRetry = (id: number) => {
    setRetrySelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const runRetry = async () => {
    if (retrying) return
    setRetrying(true)
    setActionError(null)
    try {
      await api.post('/api/fulfillment/batch-items/retry', { batch_item_ids: [...retrySelected] })
      setRetrySelected(new Set())
      reloadAll()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : '재처리 중 오류가 발생했습니다.')
    } finally {
      setRetrying(false)
    }
  }

  return (
    <div>
      <h3>
        배치 #{batchId} 상세{' '}
        <button type="button" onClick={onClose}>
          닫기
        </button>
      </h3>
      {detailError && <p className="form-error">{detailError}</p>}
      {actionError && <p className="form-error">{actionError}</p>}

      {detailLoading ? (
        <p>불러오는 중...</p>
      ) : (
        <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>포장선택</th>
              <th>주문번호</th>
              <th>상품명/SKU</th>
              <th>요청수량</th>
              <th>상태</th>
              <th>작업</th>
              <th>취소</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.id}>
                <td>
                  {item.status === 'VERIFIED' ? (
                    <input type="checkbox" checked={packSelected.has(item.id)} onChange={() => togglePack(item.id)} />
                  ) : (
                    '-'
                  )}
                </td>
                <td>{item.platform_order_no}</td>
                <td>
                  {item.product_name || '-'} / {item.sku_code || '-'}
                </td>
                <td>{item.requested_quantity}</td>
                <td>
                  <span className="status-badge">{STATUS_LABELS[item.status] ?? item.status}</span>
                  {item.failure_reason_code && <div className="hint-text">사유: {item.failure_reason_code}</div>}
                </td>
                <td>
                  {item.status === 'READY' && (
                    <button type="button" disabled={pendingId === item.id} onClick={() => runTransition(item, 'start-picking', {})}>
                      피킹 시작
                    </button>
                  )}
                  {item.status === 'PICKING' && (
                    <span>
                      <input
                        type="number"
                        min={1}
                        max={item.requested_quantity}
                        value={pickedQtyById[item.id] ?? String(item.requested_quantity)}
                        onChange={(e) => setPickedQtyById((prev) => ({ ...prev, [item.id]: e.target.value }))}
                        style={{ width: 70 }}
                      />
                      <button
                        type="button"
                        disabled={pendingId === item.id}
                        onClick={() => completePickingIfValid(item)}
                      >
                        피킹 완료
                      </button>
                    </span>
                  )}
                  {item.status === 'PICKED' && (
                    <button type="button" disabled={pendingId === item.id} onClick={() => runTransition(item, 'start-verification', {})}>
                      검수 시작
                    </button>
                  )}
                  {item.status === 'VERIFYING' && (
                    <span>
                      <input
                        type="number"
                        min={0}
                        max={item.picked_quantity ?? item.requested_quantity}
                        value={verifiedQtyById[item.id] ?? String(item.picked_quantity ?? item.requested_quantity)}
                        onChange={(e) => setVerifiedQtyById((prev) => ({ ...prev, [item.id]: e.target.value }))}
                        style={{ width: 70 }}
                      />
                      <button
                        type="button"
                        disabled={pendingId === item.id}
                        onClick={() => completeVerificationIfValid(item)}
                      >
                        검수 확정
                      </button>
                    </span>
                  )}
                  {item.status === 'VERIFIED' && <span className="hint-text">왼쪽 체크 후 아래에서 포장</span>}
                </td>
                <td>
                  {['READY', 'PICKING', 'PICKED', 'VERIFYING', 'VERIFIED', 'PACKED'].includes(item.status) && (
                    <button
                      type="button"
                      disabled={pendingId === item.id}
                      onClick={() => runTransition(item, 'cancel', {})}
                    >
                      취소
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {items.length === 0 && (
              <tr>
                <td colSpan={7}>항목이 없습니다.</td>
              </tr>
            )}
          </tbody>
        </table>
        </div>
      )}

      <h4>포장완료 + 송장등록</h4>
      <div className="filter-bar">
        <span>{packSelected.size}건 선택됨(검수완료 항목만 선택 가능)</span>
        <select value={carrier} onChange={(e) => setCarrier(e.target.value)}>
          <option value="">택배사 선택</option>
          {(carriers ?? []).map((c) => (
            <option key={c.code} value={c.code}>
              {c.label}
            </option>
          ))}
        </select>
        <input value={trackingNo} onChange={(e) => setTrackingNo(e.target.value)} placeholder="송장번호(일괄 적용)" />
        <button type="button" disabled={packing || packSelected.size === 0 || !carrier || !trackingNo} onClick={runPack}>
          {packing ? '처리 중...' : '포장완료 등록'}
        </button>
      </div>
      {packResults && (
        <ul>
          {packResults.map((r) => (
            <li key={r.batch_item_id}>
              항목 #{r.batch_item_id}: {OUTCOME_LABELS[r.outcome] ?? r.outcome}
              {r.error_code ? ` (${r.error_code})` : ''}
            </li>
          ))}
        </ul>
      )}

      <h4>채널 송장 전송 접수</h4>
      <div className="filter-bar">
        <span>포장완료된 배송 {packedShipmentIds.length}건</span>
        <button type="button" disabled={submitting || packedShipmentIds.length === 0} onClick={runSubmit}>
          {submitting ? '접수 중...' : '전송 접수'}
        </button>
      </div>
      {submitResults && (
        <ul>
          {submitResults.map((r) => (
            <li key={r.batch_item_id}>
              항목 #{r.batch_item_id}: {OUTCOME_LABELS[r.outcome] ?? r.outcome}
              {r.error_code ? ` (${r.error_code})` : ''}
            </li>
          ))}
        </ul>
      )}

      <h4>
        진행상태(채널 전송){' '}
        <button type="button" onClick={reloadProgress}>
          새로고침
        </button>
      </h4>
      {unknownProgressItems.length > 0 && (
        <p className="form-error">
          UNKNOWN(확인 필요) {unknownProgressItems.length}건 - 채널 응답이 불명확합니다. 자동 재처리하지 않으며,
          기존 배송관리 화면에서 운영자가 직접 확인 후 해소해야 합니다.
        </p>
      )}
      <div className="table-scroll">
      <table className="data-table">
        <thead>
          <tr>
            <th>재처리 선택</th>
            <th>항목 ID</th>
            <th>배치항목 상태</th>
            <th>전송 상태</th>
            <th>사유</th>
          </tr>
        </thead>
        <tbody>
          {(progress ?? []).map((p) => (
            <tr key={p.batch_item.id}>
              <td>
                {p.command_status === 'FAILED' ? (
                  <input type="checkbox" checked={retrySelected.has(p.batch_item.id)} onChange={() => toggleRetry(p.batch_item.id)} />
                ) : (
                  '-'
                )}
              </td>
              <td>{p.batch_item.id}</td>
              <td>
                <span className="status-badge">{STATUS_LABELS[p.batch_item.status] ?? p.batch_item.status}</span>
              </td>
              <td>
                {p.command_status ? (
                  <span className="status-badge">{COMMAND_STATUS_LABELS[p.command_status] ?? p.command_status}</span>
                ) : (
                  '-'
                )}
              </td>
              <td>{p.command_error_code ?? '-'}</td>
            </tr>
          ))}
          {(progress ?? []).length === 0 && (
            <tr>
              <td colSpan={5}>표시할 항목이 없습니다.</td>
            </tr>
          )}
        </tbody>
      </table>
      </div>
      {failedProgressItems.length > 0 && (
        <button type="button" disabled={retrying || retrySelected.size === 0} onClick={runRetry}>
          {retrying ? '재처리 중...' : `선택한 ${retrySelected.size}건 재처리`}
        </button>
      )}

      <h4>작업 이력</h4>
      <div className="table-scroll">
      <table className="data-table">
        <thead>
          <tr>
            <th>항목 ID</th>
            <th>이전 상태</th>
            <th>다음 상태</th>
            <th>처리자</th>
            <th>처리시각</th>
            <th>메모</th>
          </tr>
        </thead>
        <tbody>
          {(history ?? []).map((h) => (
            <tr key={h.id}>
              <td>{h.batch_item_id}</td>
              <td>{h.from_status ? STATUS_LABELS[h.from_status] ?? h.from_status : '-'}</td>
              <td>{STATUS_LABELS[h.to_status] ?? h.to_status}</td>
              <td>{h.changed_by ?? '-'}</td>
              <td>{new Date(h.changed_at).toLocaleString()}</td>
              <td>{h.note ?? '-'}</td>
            </tr>
          ))}
          {(history ?? []).length === 0 && (
            <tr>
              <td colSpan={6}>이력이 없습니다.</td>
            </tr>
          )}
        </tbody>
      </table>
      </div>
    </div>
  )
}
