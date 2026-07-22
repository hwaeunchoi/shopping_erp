import { useState } from 'react'
import { api } from '../api/client'
import { useApiData } from '../api/useApiData'
import type { Customer } from '../api/types'

type FilterMode = 'all' | 'vip' | 'dormant'

export function CustomersPage() {
  const [filter, setFilter] = useState<FilterMode>('all')
  const query = filter === 'vip' ? '?vip_only=true' : filter === 'dormant' ? '?dormant_only=true' : ''
  const { data, error, isLoading } = useApiData<Customer[]>(
    () => api.get(`/api/customers${query}`),
    [filter],
  )

  return (
    <div>
      <h2>고객관리</h2>
      <div className="filter-bar">
        <button type="button" className={filter === 'all' ? 'active' : ''} onClick={() => setFilter('all')}>전체</button>
        <button type="button" className={filter === 'vip' ? 'active' : ''} onClick={() => setFilter('vip')}>VIP</button>
        <button type="button" className={filter === 'dormant' ? 'active' : ''} onClick={() => setFilter('dormant')}>휴면</button>
      </div>
      {isLoading && <p>불러오는 중...</p>}
      {error && <p className="form-error">{error}</p>}
      {data && (
        <table className="data-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>이름</th>
              <th>연락처</th>
              <th>등급</th>
              <th>누적구매액</th>
              <th>주문수</th>
              <th>VIP</th>
              <th>휴면</th>
            </tr>
          </thead>
          <tbody>
            {data.map((c) => (
              <tr key={c.id}>
                <td>{c.id}</td>
                <td>{c.name ?? '-'}</td>
                <td>{c.phone ?? '-'}</td>
                <td>{c.grade ?? '-'}</td>
                <td>{c.total_purchase_amount.toLocaleString()}</td>
                <td>{c.order_count}</td>
                <td>{c.is_vip ? 'Y' : ''}</td>
                <td>{c.is_dormant ? 'Y' : ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
