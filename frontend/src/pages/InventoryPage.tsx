import { Fragment, useState, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'
import { useApiData } from '../api/useApiData'
import type { InventoryItem, InventoryTransaction } from '../api/types'

function InventoryRowDetail({ inv, onSaved }: { inv: InventoryItem; onSaved: () => void }) {
  const { data: transactions, reload: reloadTransactions } = useApiData<InventoryTransaction[]>(
    () => api.get(`/api/inventory/${inv.id}/transactions`),
    [inv.id],
  )
  const [safetyStock, setSafetyStock] = useState(String(inv.safety_stock))
  const [safetyError, setSafetyError] = useState<string | null>(null)
  const [delta, setDelta] = useState('')
  const [memo, setMemo] = useState('')
  const [adjustError, setAdjustError] = useState<string | null>(null)

  const handleSaveSafetyStock = async (e: FormEvent) => {
    e.preventDefault()
    setSafetyError(null)
    try {
      await api.patch(`/api/inventory/safety-stock`, {
        product_option_id: inv.product_option_id,
        warehouse_id: inv.warehouse_id,
        safety_stock: Number(safetyStock),
      })
      onSaved()
    } catch (err) {
      setSafetyError(err instanceof ApiError ? err.message : '안전재고 수정 중 오류가 발생했습니다.')
    }
  }

  const handleAdjust = async (e: FormEvent) => {
    e.preventDefault()
    if (!delta) {
      setAdjustError('조정 수량을 입력하세요.')
      return
    }
    setAdjustError(null)
    try {
      await api.post(`/api/inventory/adjust`, {
        product_option_id: inv.product_option_id,
        warehouse_id: inv.warehouse_id,
        delta: Number(delta),
        memo: memo || null,
      })
      setDelta('')
      setMemo('')
      onSaved()
      reloadTransactions()
    } catch (err) {
      setAdjustError(err instanceof ApiError ? err.message : '재고 조정 중 오류가 발생했습니다.')
    }
  }

  return (
    <tr className="detail-subrow">
      <td colSpan={8}>
        <h3>안전재고 기준값 변경</h3>
        <form className="inline-form" onSubmit={handleSaveSafetyStock}>
          <input
            type="number"
            min={0}
            value={safetyStock}
            onChange={(e) => setSafetyStock(e.target.value)}
            style={{ width: '6rem' }}
          />
          <button type="submit">저장</button>
          {safetyError && <span className="form-error">{safetyError}</span>}
        </form>

        <h3>재고 조정(입고/실사)</h3>
        <form className="inline-form" onSubmit={handleAdjust}>
          <input
            type="number"
            placeholder="증감 수량(+/-)"
            value={delta}
            onChange={(e) => setDelta(e.target.value)}
            style={{ width: '8rem' }}
          />
          <input
            value={memo}
            onChange={(e) => setMemo(e.target.value)}
            placeholder="메모(사유)"
            style={{ width: '16rem' }}
          />
          <button type="submit">조정 반영</button>
        </form>
        {adjustError && <p className="form-error">{adjustError}</p>}

        <h3>입출고 이력</h3>
        <table className="data-table nested">
          <thead>
            <tr>
              <th>일시</th><th>구분</th><th>수량</th><th>참조</th><th>메모</th>
            </tr>
          </thead>
          <tbody>
            {transactions?.map((t) => (
              <tr key={t.id}>
                <td>{new Date(t.created_at).toLocaleString()}</td>
                <td>{t.type}</td>
                <td>{t.quantity > 0 ? `+${t.quantity}` : t.quantity}</td>
                <td>{t.reference_type ? `${t.reference_type}${t.reference_id ? ` #${t.reference_id}` : ''}` : '-'}</td>
                <td>{t.memo ?? '-'}</td>
              </tr>
            ))}
            {transactions && transactions.length === 0 && (
              <tr><td colSpan={5}>이력이 없습니다.</td></tr>
            )}
          </tbody>
        </table>
      </td>
    </tr>
  )
}

export function InventoryPage() {
  const [lowStockOnly, setLowStockOnly] = useState(false)
  const [expandedId, setExpandedId] = useState<number | null>(null)
  const { data, error, isLoading, reload } = useApiData<InventoryItem[]>(
    () => api.get(`/api/inventory${lowStockOnly ? '?low_stock_only=true' : ''}`),
    [lowStockOnly],
  )

  return (
    <div>
      <h2>재고관리</h2>
      <div className="filter-bar">
        <button type="button" className={!lowStockOnly ? 'active' : ''} onClick={() => setLowStockOnly(false)}>전체</button>
        <button type="button" className={lowStockOnly ? 'active' : ''} onClick={() => setLowStockOnly(true)}>안전재고 미만</button>
      </div>
      {isLoading && <p>불러오는 중...</p>}
      {error && <p className="form-error">{error}</p>}
      {data && (
        <table className="data-table">
          <thead>
            <tr>
              <th>상품명</th>
              <th>SKU</th>
              <th>창고</th>
              <th>현재재고</th>
              <th>예약재고</th>
              <th>가용재고</th>
              <th>안전재고</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {data.map((inv) => (
              <Fragment key={inv.id}>
                <tr className={inv.sellable_stock < inv.safety_stock ? 'row-warning' : ''}>
                  <td>{inv.product_name ?? '-'}{inv.option_name ? ` (${inv.option_name})` : ''}</td>
                  <td>{inv.sku_code ?? '-'}</td>
                  <td>{inv.warehouse_name ?? inv.warehouse_id}</td>
                  <td>{inv.sellable_stock}</td>
                  <td>{inv.reserved_stock}</td>
                  <td>{inv.sellable_stock - inv.reserved_stock}</td>
                  <td>{inv.safety_stock}</td>
                  <td>
                    <button type="button" onClick={() => setExpandedId(expandedId === inv.id ? null : inv.id)}>
                      {expandedId === inv.id ? '닫기' : '관리'}
                    </button>
                  </td>
                </tr>
                {expandedId === inv.id && <InventoryRowDetail inv={inv} onSaved={reload} />}
              </Fragment>
            ))}
            {data.length === 0 && <tr><td colSpan={8}>재고 데이터가 없습니다.</td></tr>}
          </tbody>
        </table>
      )}
    </div>
  )
}
