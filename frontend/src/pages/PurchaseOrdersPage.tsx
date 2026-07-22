import { Fragment, useState, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'
import { useApiData } from '../api/useApiData'
import type { PurchaseOrder, Supplier } from '../api/types'

const STATUS_LABEL: Record<PurchaseOrder['status'], string> = {
  DRAFT: '작성중',
  ORDERED: '발주완료',
  PARTIALLY_RECEIVED: '부분입고',
  RECEIVED: '입고완료',
  CANCELLED: '취소됨',
}

function PurchaseOrderRowDetail({
  po,
  suppliers,
  onSaved,
}: {
  po: PurchaseOrder
  suppliers: Supplier[]
  onSaved: () => void
}) {
  const [optionId, setOptionId] = useState('')
  const [quantity, setQuantity] = useState('')
  const [unitCost, setUnitCost] = useState('')
  const [itemError, setItemError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [receiveInputs, setReceiveInputs] = useState<Record<number, { quantity: string; warehouseId: string }>>({})

  const supplier = suppliers.find((s) => s.id === po.supplier_id)

  const handleAddItem = async (e: FormEvent) => {
    e.preventDefault()
    if (!optionId || !quantity) {
      setItemError('상품옵션 ID와 수량을 입력하세요.')
      return
    }
    setItemError(null)
    try {
      await api.post(`/api/purchase-orders/${po.id}/items`, {
        product_option_id: Number(optionId),
        quantity: Number(quantity),
        unit_cost: unitCost ? Number(unitCost) : 0,
      })
      setOptionId('')
      setQuantity('')
      setUnitCost('')
      onSaved()
    } catch (err) {
      setItemError(err instanceof ApiError ? err.message : '품목 추가 중 오류가 발생했습니다.')
    }
  }

  const handleConfirm = async () => {
    setActionError(null)
    try {
      await api.post(`/api/purchase-orders/${po.id}/confirm`)
      onSaved()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : '발주 확정 중 오류가 발생했습니다.')
    }
  }

  const handleCancel = async () => {
    setActionError(null)
    try {
      await api.post(`/api/purchase-orders/${po.id}/cancel`)
      onSaved()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : '발주 취소 중 오류가 발생했습니다.')
    }
  }

  const handleReceive = async (itemId: number) => {
    const input = receiveInputs[itemId]
    if (!input?.quantity || !input?.warehouseId) {
      setActionError('입고 수량과 창고 ID를 입력하세요.')
      return
    }
    setActionError(null)
    try {
      await api.post(`/api/purchase-orders/${po.id}/items/${itemId}/receive`, {
        quantity: Number(input.quantity),
        warehouse_id: Number(input.warehouseId),
      })
      setReceiveInputs((prev) => ({ ...prev, [itemId]: { quantity: '', warehouseId: '' } }))
      onSaved()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : '입고 처리 중 오류가 발생했습니다.')
    }
  }

  return (
    <tr className="detail-subrow">
      <td colSpan={6}>
        <h3>공급처: {supplier?.name ?? po.supplier_id}</h3>

        <table className="data-table nested">
          <thead>
            <tr>
              <th>상품옵션 ID</th><th>수량</th><th>단가</th><th>입고수량</th><th>입고 처리</th>
            </tr>
          </thead>
          <tbody>
            {po.items.map((item) => (
              <tr key={item.id}>
                <td>{item.product_option_id}</td>
                <td>{item.quantity}</td>
                <td>{item.unit_cost.toLocaleString()}</td>
                <td>{item.received_quantity}</td>
                <td>
                  {po.status === 'ORDERED' || po.status === 'PARTIALLY_RECEIVED' ? (
                    <span className="inline-form">
                      <input
                        type="number"
                        placeholder="입고수량"
                        value={receiveInputs[item.id]?.quantity ?? ''}
                        onChange={(e) =>
                          setReceiveInputs((prev) => ({
                            ...prev,
                            [item.id]: { ...prev[item.id], quantity: e.target.value, warehouseId: prev[item.id]?.warehouseId ?? '' },
                          }))
                        }
                        style={{ width: '6rem' }}
                      />
                      <input
                        type="number"
                        placeholder="창고 ID"
                        value={receiveInputs[item.id]?.warehouseId ?? ''}
                        onChange={(e) =>
                          setReceiveInputs((prev) => ({
                            ...prev,
                            [item.id]: { ...prev[item.id], warehouseId: e.target.value, quantity: prev[item.id]?.quantity ?? '' },
                          }))
                        }
                        style={{ width: '6rem' }}
                      />
                      <button type="button" onClick={() => handleReceive(item.id)}>입고 처리</button>
                    </span>
                  ) : (
                    '-'
                  )}
                </td>
              </tr>
            ))}
            {po.items.length === 0 && <tr><td colSpan={5}>등록된 품목이 없습니다.</td></tr>}
          </tbody>
        </table>

        {po.status === 'DRAFT' && (
          <form className="inline-form" onSubmit={handleAddItem}>
            <input
              type="number"
              placeholder="상품옵션 ID"
              value={optionId}
              onChange={(e) => setOptionId(e.target.value)}
              style={{ width: '8rem' }}
            />
            <input
              type="number"
              placeholder="수량"
              value={quantity}
              onChange={(e) => setQuantity(e.target.value)}
              style={{ width: '6rem' }}
            />
            <input
              type="number"
              placeholder="단가"
              value={unitCost}
              onChange={(e) => setUnitCost(e.target.value)}
              style={{ width: '8rem' }}
            />
            <button type="submit">품목 추가</button>
          </form>
        )}
        {itemError && <p className="form-error">{itemError}</p>}

        <div className="inline-form">
          {po.status === 'DRAFT' && <button type="button" onClick={handleConfirm}>발주 확정</button>}
          {(po.status === 'DRAFT' || po.status === 'ORDERED') && (
            <button type="button" onClick={handleCancel}>발주 취소</button>
          )}
        </div>
        {actionError && <p className="form-error">{actionError}</p>}
      </td>
    </tr>
  )
}

export function PurchaseOrdersPage() {
  const [statusFilter, setStatusFilter] = useState<string>('')
  const [expandedId, setExpandedId] = useState<number | null>(null)
  const { data, error, isLoading, reload } = useApiData<PurchaseOrder[]>(
    () => api.get(`/api/purchase-orders${statusFilter ? `?status_filter=${statusFilter}` : ''}`),
    [statusFilter],
  )
  const { data: suppliers } = useApiData<Supplier[]>(() => api.get('/api/suppliers?active_only=true'), [])

  const [supplierId, setSupplierId] = useState('')
  const [memo, setMemo] = useState('')
  const [createError, setCreateError] = useState<string | null>(null)

  const handleCreate = async (e: FormEvent) => {
    e.preventDefault()
    if (!supplierId) {
      setCreateError('공급처를 선택하세요.')
      return
    }
    setCreateError(null)
    try {
      await api.post('/api/purchase-orders', { supplier_id: Number(supplierId), memo: memo || null })
      setMemo('')
      reload()
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.message : '발주 작성 중 오류가 발생했습니다.')
    }
  }

  return (
    <div>
      <h2>발주관리</h2>
      <div className="filter-bar">
        <button type="button" className={statusFilter === '' ? 'active' : ''} onClick={() => setStatusFilter('')}>전체</button>
        {(['DRAFT', 'ORDERED', 'PARTIALLY_RECEIVED', 'RECEIVED', 'CANCELLED'] as const).map((s) => (
          <button
            key={s}
            type="button"
            className={statusFilter === s ? 'active' : ''}
            onClick={() => setStatusFilter(s)}
          >
            {STATUS_LABEL[s]}
          </button>
        ))}
      </div>

      <form className="inline-form" onSubmit={handleCreate}>
        <select value={supplierId} onChange={(e) => setSupplierId(e.target.value)}>
          <option value="">공급처 선택</option>
          {suppliers?.map((s) => (
            <option key={s.id} value={s.id}>{s.name}</option>
          ))}
        </select>
        <input value={memo} onChange={(e) => setMemo(e.target.value)} placeholder="메모" style={{ width: '16rem' }} />
        <button type="submit">발주 작성</button>
        {createError && <span className="form-error">{createError}</span>}
      </form>

      {isLoading && <p>불러오는 중...</p>}
      {error && <p className="form-error">{error}</p>}
      {data && (
        <table className="data-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>공급처</th>
              <th>상태</th>
              <th>발주일</th>
              <th>메모</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {data.map((po) => (
              <Fragment key={po.id}>
                <tr>
                  <td>{po.id}</td>
                  <td>{suppliers?.find((s) => s.id === po.supplier_id)?.name ?? po.supplier_id}</td>
                  <td>{STATUS_LABEL[po.status]}</td>
                  <td>{po.order_date ? new Date(po.order_date).toLocaleString() : '-'}</td>
                  <td>{po.memo ?? '-'}</td>
                  <td>
                    <button type="button" onClick={() => setExpandedId(expandedId === po.id ? null : po.id)}>
                      {expandedId === po.id ? '닫기' : '관리'}
                    </button>
                  </td>
                </tr>
                {expandedId === po.id && (
                  <PurchaseOrderRowDetail po={po} suppliers={suppliers ?? []} onSaved={reload} />
                )}
              </Fragment>
            ))}
            {data.length === 0 && <tr><td colSpan={6}>등록된 발주가 없습니다.</td></tr>}
          </tbody>
        </table>
      )}
    </div>
  )
}
