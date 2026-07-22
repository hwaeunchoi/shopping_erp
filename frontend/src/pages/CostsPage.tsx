import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'
import { useApiData } from '../api/useApiData'
import type { Cost, CostCreate } from '../api/types'

const CATEGORIES = ['SHIPPING', 'PACKAGING', 'PLATFORM_FEE', 'AD_AGENCY_FEE', 'RETURN_SHIPPING', 'ETC']
const COST_TYPES = ['FIXED', 'VARIABLE']

function todayIso(): string {
  return new Date().toISOString().slice(0, 10)
}

export function CostsPage() {
  const [category, setCategory] = useState('')
  const { data, error, isLoading, reload } = useApiData<Cost[]>(
    () => api.get(`/api/costs${category ? `?category=${category}` : ''}`),
    [category],
  )

  const [form, setForm] = useState<CostCreate>({
    category: 'ETC', cost_type: 'FIXED', amount: 0, incurred_date: todayIso(), memo: '',
  })
  const [createError, setCreateError] = useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)

  const handleCreate = async (e: FormEvent) => {
    e.preventDefault()
    setCreateError(null)
    setIsSubmitting(true)
    try {
      await api.post<Cost>('/api/costs', form)
      setForm((f) => ({ ...f, amount: 0, memo: '' }))
      reload()
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.message : '비용 등록 중 오류가 발생했습니다.')
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <div>
      <h2>비용관리</h2>

      <form className="inline-form" onSubmit={handleCreate}>
        <select value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })}>
          {CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        <select value={form.cost_type} onChange={(e) => setForm({ ...form, cost_type: e.target.value })}>
          {COST_TYPES.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        <input
          type="number" value={form.amount} min={0}
          onChange={(e) => setForm({ ...form, amount: Number(e.target.value) })}
          placeholder="금액"
        />
        <input
          type="date" value={form.incurred_date}
          onChange={(e) => setForm({ ...form, incurred_date: e.target.value })}
        />
        <input
          value={form.memo ?? ''} onChange={(e) => setForm({ ...form, memo: e.target.value })}
          placeholder="메모"
        />
        <button type="submit" disabled={isSubmitting}>{isSubmitting ? '등록 중...' : '비용 등록'}</button>
      </form>
      {createError && <p className="form-error">{createError}</p>}

      <div className="filter-bar">
        <button type="button" className={category === '' ? 'active' : ''} onClick={() => setCategory('')}>전체</button>
        {CATEGORIES.map((c) => (
          <button key={c} type="button" className={category === c ? 'active' : ''} onClick={() => setCategory(c)}>{c}</button>
        ))}
      </div>

      {isLoading && <p>불러오는 중...</p>}
      {error && <p className="form-error">{error}</p>}
      {data && (
        <table className="data-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>카테고리</th>
              <th>유형</th>
              <th>금액</th>
              <th>발생일</th>
              <th>메모</th>
            </tr>
          </thead>
          <tbody>
            {data.map((c) => (
              <tr key={c.id}>
                <td>{c.id}</td>
                <td>{c.category}</td>
                <td>{c.cost_type}</td>
                <td>{c.amount.toLocaleString()}</td>
                <td>{c.incurred_date}</td>
                <td>{c.memo ?? '-'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
