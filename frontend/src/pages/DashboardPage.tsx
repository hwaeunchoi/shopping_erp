import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { useApiData } from '../api/useApiData'
import { DrillLink } from '../components/DrillLink'
import { MiniBarChart, type BarDatum } from '../components/MiniBarChart'
import { QuickActionButtons } from '../components/QuickActionButtons'
import type { OrderAlerts, ProfitLossSummary } from '../api/types'

// UI 와이어프레임 v1.1 7장 Drill-down 공통 규칙: KPI 카드/그래프는 항상
// 동일 기간·조건 필터가 적용된 원본 목록 화면으로 이동한다.
export function DashboardPage() {
  const navigate = useNavigate()
  const { data, error, isLoading } = useApiData<ProfitLossSummary[]>(
    () => api.get('/api/analytics/profit-loss'),
    [],
  )
  const { data: alerts } = useApiData<OrderAlerts>(() => api.get('/api/orders/alerts'), [])

  const recent = data ? [...data].sort((a, b) => b.period_key.localeCompare(a.period_key)).slice(0, 7) : []
  const chronological = [...recent].reverse()
  const totalRevenue = recent.reduce((sum, s) => sum + s.net_revenue, 0)
  const totalProfit = recent.reduce((sum, s) => sum + s.net_profit, 0)
  const totalOrders = recent.reduce((sum, s) => sum + s.order_count, 0)

  const revenueChartData: BarDatum[] = chronological.map((s) => ({
    key: s.period_key,
    label: s.period_key.slice(5), // MM-DD
    value: s.net_revenue,
  }))
  const profitChartData: BarDatum[] = chronological.map((s) => ({
    key: s.period_key,
    label: s.period_key.slice(5),
    value: s.net_profit,
  }))

  function drillToDay(periodKey: string) {
    navigate(`/orders?start_date=${periodKey}&end_date=${periodKey}`)
  }

  return (
    <div>
      <h2>대시보드</h2>

      <QuickActionButtons />

      {alerts && (
        <div className="kpi-cards">
          <DrillLink to="/orders" filters={{ status_filter: 'NEW' }} className="kpi-card">
            <div className="kpi-label">미배송 주문</div>
            <div className={'kpi-value' + (alerts.delayed_unshipped_count > 0 ? ' negative' : '')}>
              {alerts.unshipped_count}건
            </div>
            {alerts.delayed_unshipped_count > 0 && (
              <div className="kpi-label negative">2일 이상 지연 {alerts.delayed_unshipped_count}건</div>
            )}
          </DrillLink>
          <DrillLink to="/exchanges" filters={{ status_filter: 'REQUESTED' }} className="kpi-card">
            <div className="kpi-label">교환 대기</div>
            <div className={'kpi-value' + (alerts.exchange_pending_count > 0 ? ' negative' : '')}>
              {alerts.exchange_pending_count}건
            </div>
          </DrillLink>
          <DrillLink to="/returns" filters={{ status_filter: 'REQUESTED' }} className="kpi-card">
            <div className="kpi-label">반품 대기</div>
            <div className={'kpi-value' + (alerts.return_pending_count > 0 ? ' negative' : '')}>
              {alerts.return_pending_count}건
            </div>
          </DrillLink>
          <DrillLink to="/cancellations" filters={{ status_filter: 'REQUESTED' }} className="kpi-card">
            <div className="kpi-label">취소 대기</div>
            <div className={'kpi-value' + (alerts.cancellation_pending_count > 0 ? ' negative' : '')}>
              {alerts.cancellation_pending_count}건
            </div>
          </DrillLink>
        </div>
      )}

      {isLoading && <p>불러오는 중...</p>}
      {error && <p className="form-error">{error}</p>}
      {data && (
        <>
          <div className="kpi-cards">
            <DrillLink to="/orders" filters={{ start_date: chronological[0]?.period_key, end_date: chronological[chronological.length - 1]?.period_key }} className="kpi-card">
              <div className="kpi-label">최근 7일 순매출</div>
              <div className="kpi-value">{totalRevenue.toLocaleString()}원</div>
            </DrillLink>
            <DrillLink to="/analytics" className="kpi-card">
              <div className="kpi-label">최근 7일 순이익</div>
              <div className={'kpi-value' + (totalProfit < 0 ? ' negative' : '')}>{totalProfit.toLocaleString()}원</div>
            </DrillLink>
            <DrillLink to="/orders" filters={{ start_date: chronological[0]?.period_key, end_date: chronological[chronological.length - 1]?.period_key }} className="kpi-card">
              <div className="kpi-label">최근 7일 주문수</div>
              <div className="kpi-value">{totalOrders.toLocaleString()}건</div>
            </DrillLink>
          </div>

          <h3>일별 순매출 추이</h3>
          <p className="form-info">막대를 클릭하면 해당 날짜의 주문 목록으로 이동합니다.</p>
          <MiniBarChart
            data={revenueChartData}
            onBarClick={(d) => drillToDay(d.key)}
            formatValue={(v) => `${v.toLocaleString()}원`}
          />

          <h3>일별 순이익 추이</h3>
          <MiniBarChart
            data={profitChartData}
            onBarClick={(d) => drillToDay(d.key)}
            formatValue={(v) => `${v.toLocaleString()}원`}
          />

          <h3>일별 추이</h3>
          <table className="data-table">
            <thead>
              <tr><th>날짜</th><th>순매출</th><th>주문수</th><th>순이익</th><th>순이익률</th></tr>
            </thead>
            <tbody>
              {recent.map((s) => (
                <tr
                  key={s.period_key}
                  className={(s.net_profit < 0 ? 'row-warning ' : '') + 'clickable-row'}
                  onClick={() => drillToDay(s.period_key)}
                >
                  <td>{s.period_key}</td>
                  <td>{s.net_revenue.toLocaleString()}</td>
                  <td>{s.order_count}</td>
                  <td>{s.net_profit.toLocaleString()}</td>
                  <td>{s.net_profit_rate.toFixed(1)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  )
}
