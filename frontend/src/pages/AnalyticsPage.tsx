import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'
import { useApiData } from '../api/useApiData'
import type { Platform, ProductPerformance, ProfitLossSummary } from '../api/types'

function todayIso(): string {
  return new Date().toISOString().slice(0, 10)
}

export function AnalyticsPage() {
  // SRS FR-PROFIT-02: 손익을 플랫폼 단위로 세분화하여 조회할 수 있어야 한다.
  const [platformId, setPlatformId] = useState<number | ''>('')
  const { data: platforms } = useApiData<Platform[]>(() => api.get('/api/platforms'), [])

  const { data, error, isLoading, reload } = useApiData<ProfitLossSummary[]>(
    () => api.get(`/api/analytics/profit-loss${platformId ? `?platform_id=${platformId}` : ''}`),
    [platformId],
  )

  const [targetDate, setTargetDate] = useState(todayIso())
  const [calcError, setCalcError] = useState<string | null>(null)
  const [isCalculating, setIsCalculating] = useState(false)

  const sorted = data ? [...data].sort((a, b) => b.period_key.localeCompare(a.period_key)) : null

  const handleCalculate = async (e: FormEvent) => {
    e.preventDefault()
    setCalcError(null)
    setIsCalculating(true)
    try {
      await api.post('/api/analytics/profit-loss/calculate', {
        target_date: targetDate,
        platform_id: platformId || null,
      })
      await api.post('/api/analytics/product-performance/calculate', { target_date: targetDate })
      reload()
      reloadPerformance()
    } catch (err) {
      setCalcError(err instanceof ApiError ? err.message : '계산 중 오류가 발생했습니다.')
    } finally {
      setIsCalculating(false)
    }
  }

  const [rankMode, setRankMode] = useState<'desc' | 'asc'>('desc')
  const { data: performance, reload: reloadPerformance } = useApiData<ProductPerformance[]>(
    () => api.get(`/api/analytics/product-performance?period_key=${targetDate}&order=${rankMode}`),
    [targetDate, rankMode],
  )

  return (
    <div>
      <h2>매출/손익분석</h2>

      <form className="inline-form" onSubmit={handleCalculate}>
        <input type="date" value={targetDate} onChange={(e) => setTargetDate(e.target.value)} />
        <select value={platformId} onChange={(e) => setPlatformId(e.target.value ? Number(e.target.value) : '')}>
          <option value="">전체(플랫폼 무관)</option>
          {platforms?.map((p) => (
            <option key={p.id} value={p.id}>{p.name}</option>
          ))}
        </select>
        <button type="submit" disabled={isCalculating}>{isCalculating ? '계산 중...' : '해당 날짜 손익 재계산'}</button>
      </form>
      {calcError && <p className="form-error">{calcError}</p>}

      {isLoading && <p>불러오는 중...</p>}
      {error && <p className="form-error">{error}</p>}
      {sorted && (
        <table className="data-table">
          <thead>
            <tr>
              <th>날짜</th>
              <th>순매출</th>
              <th>주문수</th>
              <th>광고비</th>
              <th>매출원가</th>
              <th>총비용</th>
              <th>순이익</th>
              <th>순이익률</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((s) => (
              <tr key={s.period_key} className={s.net_profit < 0 ? 'row-warning' : ''}>
                <td>{s.period_key}</td>
                <td>{s.net_revenue.toLocaleString()}</td>
                <td>{s.order_count}</td>
                <td>{s.ad_cost.toLocaleString()}</td>
                <td>{s.cost_of_goods.toLocaleString()}</td>
                <td>{s.total_cost.toLocaleString()}</td>
                <td>{s.net_profit.toLocaleString()}</td>
                <td>{s.net_profit_rate.toFixed(1)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>상품별 베스트/워스트 ({targetDate})</h3>
      <div className="filter-bar">
        <button type="button" className={rankMode === 'desc' ? 'active' : ''} onClick={() => setRankMode('desc')}>
          베스트(순이익 높은 순)
        </button>
        <button type="button" className={rankMode === 'asc' ? 'active' : ''} onClick={() => setRankMode('asc')}>
          워스트(순이익 낮은 순)
        </button>
      </div>
      {performance && (
        <table className="data-table">
          <thead>
            <tr>
              <th>SKU ID</th>
              <th>판매수량</th>
              <th>매출</th>
              <th>광고비</th>
              <th>순이익</th>
              <th>ROAS</th>
            </tr>
          </thead>
          <tbody>
            {performance.map((p) => (
              <tr key={p.product_option_id} className={p.net_profit < 0 ? 'row-warning' : ''}>
                <td>{p.product_option_id}</td>
                <td>{p.sales_qty}</td>
                <td>{p.revenue.toLocaleString()}</td>
                <td>{p.ad_cost.toLocaleString()}</td>
                <td>{p.net_profit.toLocaleString()}</td>
                <td>{p.roas.toFixed(1)}%</td>
              </tr>
            ))}
            {performance.length === 0 && (
              <tr><td colSpan={6}>계산된 데이터가 없습니다. 위에서 날짜를 선택해 재계산하세요.</td></tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  )
}
