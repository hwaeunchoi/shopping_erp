import { Fragment, useState } from 'react'
import { api } from '../api/client'
import { useApiData } from '../api/useApiData'
import type { Settlement, SettlementDetail } from '../api/types'

function SettlementDetailsRow({ settlementId }: { settlementId: number }) {
  const { data, error, isLoading } = useApiData<SettlementDetail[]>(
    () => api.get(`/api/settlements/${settlementId}/details`),
    [settlementId],
  )
  if (isLoading) return <tr><td colSpan={4}>불러오는 중...</td></tr>
  if (error) return <tr><td colSpan={4} className="form-error">{error}</td></tr>
  if (!data || data.length === 0) return <tr><td colSpan={4}>정산 상세가 없습니다.</td></tr>
  return (
    <>
      {data.map((d) => (
        <tr key={d.id}>
          <td>주문 #{d.order_id}</td>
          <td>{d.gross_amount.toLocaleString()}</td>
          <td>{d.fee_amount.toLocaleString()}</td>
          <td>{d.net_amount.toLocaleString()}</td>
        </tr>
      ))}
    </>
  )
}

export function SettlementsPage() {
  const { data, error, isLoading } = useApiData<Settlement[]>(() => api.get('/api/settlements'), [])
  const [expandedId, setExpandedId] = useState<number | null>(null)

  return (
    <div>
      <h2>정산관리</h2>
      {isLoading && <p>불러오는 중...</p>}
      {error && <p className="form-error">{error}</p>}
      {data && (
        <table className="data-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>플랫폼ID</th>
              <th>정산회차</th>
              <th>상태</th>
              <th>예정금액</th>
              <th>정산금액</th>
              <th>미정산금액</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {data.map((s) => (
              <Fragment key={s.id}>
                <tr>
                  <td>{s.id}</td>
                  <td>{s.platform_id}</td>
                  <td>{s.settlement_cycle}</td>
                  <td>{s.status}</td>
                  <td>{s.expected_amount.toLocaleString()}</td>
                  <td>{s.settled_amount.toLocaleString()}</td>
                  <td>{s.unsettled_amount.toLocaleString()}</td>
                  <td>
                    <button type="button" onClick={() => setExpandedId(expandedId === s.id ? null : s.id)}>
                      {expandedId === s.id ? '접기' : '상세'}
                    </button>
                  </td>
                </tr>
                {expandedId === s.id && (
                  <tr className="detail-subrow">
                    <td colSpan={8}>
                      <table className="data-table nested">
                        <thead><tr><th>주문</th><th>총액</th><th>수수료</th><th>정산액</th></tr></thead>
                        <tbody><SettlementDetailsRow settlementId={s.id} /></tbody>
                      </table>
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
