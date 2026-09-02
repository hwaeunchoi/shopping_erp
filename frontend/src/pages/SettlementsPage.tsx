import { Fragment, useState } from 'react'
import { api } from '../api/client'
import { useApiData } from '../api/useApiData'
import type { Settlement, SettlementDetail, SettlementDiscrepancy } from '../api/types'

const DISCREPANCY_REASON_LABELS: Record<string, string> = {
  NO_MATCHING_ORDER: '주문 미매칭',
  NO_MATCHING_SETTLEMENT: '정산회차 미매칭',
  AMOUNT_MISMATCH: '금액 불일치',
}

function DiscrepanciesSection() {
  const { data, error, isLoading } = useApiData<SettlementDiscrepancy[]>(
    () => api.get('/api/settlements/discrepancies'),
    [],
  )

  return (
    <div style={{ marginTop: 32 }}>
      <h3>미매칭/차액</h3>
      {isLoading && <p>불러오는 중...</p>}
      {error && <p className="form-error">{error}</p>}
      {data && (
        <table className="data-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>플랫폼ID</th>
              <th>정산ID</th>
              <th>주문ID</th>
              <th>사유</th>
              <th>예상금액</th>
              <th>실제금액</th>
              <th>차액</th>
              <th>발견일시</th>
              <th>해소일시</th>
            </tr>
          </thead>
          <tbody>
            {data.map((d) => (
              <tr key={d.id}>
                <td>{d.id}</td>
                <td>{d.platform_id}</td>
                <td>{d.settlement_id ?? '-'}</td>
                <td>{d.order_id ?? '-'}</td>
                <td>{DISCREPANCY_REASON_LABELS[d.reason] ?? d.reason}</td>
                <td>{d.expected_amount != null ? d.expected_amount.toLocaleString() : '-'}</td>
                <td>{d.actual_amount != null ? d.actual_amount.toLocaleString() : '-'}</td>
                <td>{d.diff_amount != null ? d.diff_amount.toLocaleString() : '-'}</td>
                <td>{new Date(d.detected_at).toLocaleString()}</td>
                <td>{d.resolved_at ? new Date(d.resolved_at).toLocaleString() : '미해소'}</td>
              </tr>
            ))}
            {data.length === 0 && (
              <tr><td colSpan={10}>미해소 불일치가 없습니다.</td></tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  )
}

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
              <th>회차유형</th>
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
                  <td>{s.settlement_type ?? '-'}</td>
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
                    <td colSpan={9}>
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
      <DiscrepanciesSection />
    </div>
  )
}
