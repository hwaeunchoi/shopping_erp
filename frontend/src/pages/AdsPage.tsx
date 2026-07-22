import { Fragment, useState } from 'react'
import { api } from '../api/client'
import { useApiData } from '../api/useApiData'
import type { AdCampaign, AdPerformance } from '../api/types'

function todayIso(offsetDays = 0): string {
  const d = new Date()
  d.setDate(d.getDate() + offsetDays)
  return d.toISOString().slice(0, 10)
}

function PerformanceRows({ campaignId }: { campaignId: number }) {
  const start = todayIso(-7)
  const end = todayIso()
  const { data, error, isLoading } = useApiData<AdPerformance[]>(
    () => api.get(`/api/ad-campaigns/${campaignId}/performance?start_date=${start}&end_date=${end}`),
    [campaignId],
  )
  if (isLoading) return <tr><td colSpan={10}>불러오는 중...</td></tr>
  if (error) return <tr><td colSpan={10} className="form-error">{error}</td></tr>
  if (!data || data.length === 0) return <tr><td colSpan={10}>최근 7일간 성과 데이터가 없습니다.</td></tr>
  return (
    <>
      {data.map((p) => (
        <tr key={p.stat_date}>
          <td>{p.stat_date}</td>
          <td>{p.impressions.toLocaleString()}</td>
          <td>{p.clicks.toLocaleString()}</td>
          <td>{p.cost.toLocaleString()}</td>
          <td>{p.conversions}</td>
          <td>{p.conversion_amount.toLocaleString()}</td>
          <td>{p.cpc.toLocaleString()}</td>
          <td>{p.cpm.toLocaleString()}</td>
          <td>{p.ctr.toLocaleString()}%</td>
          <td>{p.roas.toLocaleString()}%</td>
        </tr>
      ))}
    </>
  )
}

export function AdsPage() {
  const { data, error, isLoading } = useApiData<AdCampaign[]>(() => api.get('/api/ad-campaigns'), [])
  const [expandedId, setExpandedId] = useState<number | null>(null)

  return (
    <div>
      <h2>광고관리</h2>
      {isLoading && <p>불러오는 중...</p>}
      {error && <p className="form-error">{error}</p>}
      {data && (
        <table className="data-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>광고 플랫폼</th>
              <th>캠페인명</th>
              <th>연결 SKU</th>
              <th>활성</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {data.map((c) => (
              <Fragment key={c.id}>
                <tr>
                  <td>{c.id}</td>
                  <td>{c.ad_platform_code}</td>
                  <td>{c.name ?? '-'}</td>
                  <td>{c.product_option_id ?? '-'}</td>
                  <td>{c.is_active ? 'Y' : ''}</td>
                  <td>
                    <button type="button" onClick={() => setExpandedId(expandedId === c.id ? null : c.id)}>
                      {expandedId === c.id ? '접기' : '최근 7일 성과'}
                    </button>
                  </td>
                </tr>
                {expandedId === c.id && (
                  <tr className="detail-subrow">
                    <td colSpan={6}>
                      <table className="data-table nested">
                        <thead>
                          <tr>
                            <th>날짜</th><th>노출</th><th>클릭</th><th>비용</th><th>전환</th><th>전환매출</th>
                            <th>CPC</th><th>CPM</th><th>CTR</th><th>ROAS</th>
                          </tr>
                        </thead>
                        <tbody><PerformanceRows campaignId={c.id} /></tbody>
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
