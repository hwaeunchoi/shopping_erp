import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'
import { downloadBlob } from '../api/download'
import { useApiData } from '../api/useApiData'
import type { MonthlyReport, ReportSchedule, ReportScheduleCreate } from '../api/types'

// SRS 3.7 종합 보고서: 월별 경영보고서 조회/다운로드 + 예약 보고서 관리.
const FREQUENCY_OPTIONS = ['DAILY', 'WEEKLY', 'MONTHLY']
const OUTPUT_FORMAT_OPTIONS = ['XLSX', 'PDF']

function todayIso(): string {
  return new Date().toISOString().slice(0, 10)
}

function MonthlyReportTab() {
  const now = new Date()
  const [year, setYear] = useState(now.getFullYear())
  const [month, setMonth] = useState(now.getMonth() + 1)
  const [queryKey, setQueryKey] = useState(`${year}-${month}`)
  const [downloadError, setDownloadError] = useState<string | null>(null)
  const [isDownloading, setIsDownloading] = useState(false)

  const { data, error, isLoading } = useApiData<MonthlyReport>(
    () => api.get(`/api/reports/monthly?year=${year}&month=${month}`),
    [queryKey],
  )

  function handleQuery(e: FormEvent) {
    e.preventDefault()
    setQueryKey(`${year}-${month}`)
  }

  async function handleDownload(format: 'excel' | 'pdf') {
    setDownloadError(null)
    setIsDownloading(true)
    try {
      const ext = format === 'excel' ? 'xlsx' : 'pdf'
      await downloadBlob(
        `/api/reports/monthly/${format}?year=${year}&month=${month}`,
        `monthly_report_${year}-${String(month).padStart(2, '0')}.${ext}`,
      )
    } catch (err) {
      setDownloadError(err instanceof ApiError ? err.message : '다운로드 중 오류가 발생했습니다.')
    } finally {
      setIsDownloading(false)
    }
  }

  return (
    <div>
      <form className="inline-form" onSubmit={handleQuery}>
        <input type="number" value={year} onChange={(e) => setYear(Number(e.target.value))} min={2020} max={2100} />
        <input type="number" value={month} onChange={(e) => setMonth(Number(e.target.value))} min={1} max={12} />
        <button type="submit">조회</button>
        <button type="button" onClick={() => handleDownload('excel')} disabled={isDownloading}>
          {isDownloading ? '다운로드 중...' : '엑셀 다운로드'}
        </button>
        <button type="button" onClick={() => handleDownload('pdf')} disabled={isDownloading}>
          {isDownloading ? '다운로드 중...' : 'PDF 다운로드'}
        </button>
      </form>
      {downloadError && <p className="form-error">{downloadError}</p>}

      {isLoading && <p>불러오는 중...</p>}
      {error && <p className="form-error">{error}</p>}
      {data && (
        <>
          <h3>{data.period_key} 손익 요약</h3>
          <div className="kpi-cards">
            <div className="kpi-card">
              <div className="kpi-label">순매출</div>
              <div className="kpi-value">{data.profit_loss.net_revenue.toLocaleString()}원</div>
            </div>
            <div className="kpi-card">
              <div className="kpi-label">순이익</div>
              <div className={'kpi-value' + (data.profit_loss.net_profit < 0 ? ' negative' : '')}>
                {data.profit_loss.net_profit.toLocaleString()}원
              </div>
            </div>
            <div className="kpi-card">
              <div className="kpi-label">순이익률</div>
              <div className="kpi-value">{data.profit_loss.net_profit_rate.toFixed(1)}%</div>
            </div>
            <div className="kpi-card">
              <div className="kpi-label">주문건수</div>
              <div className="kpi-value">{data.profit_loss.order_count.toLocaleString()}건</div>
            </div>
          </div>
          <dl className="detail-grid">
            <dt>총매출</dt><dd>{data.profit_loss.gross_revenue.toLocaleString()}</dd>
            <dt>광고비</dt><dd>{data.profit_loss.ad_cost.toLocaleString()}</dd>
            <dt>광고전환매출</dt><dd>{data.profit_loss.ad_conversion_revenue.toLocaleString()}</dd>
            <dt>상품원가</dt><dd>{data.profit_loss.cost_of_goods.toLocaleString()}</dd>
            <dt>플랫폼수수료</dt><dd>{data.profit_loss.platform_fee.toLocaleString()}</dd>
            <dt>배송비</dt><dd>{data.profit_loss.shipping_cost.toLocaleString()}</dd>
            <dt>포장비</dt><dd>{data.profit_loss.packaging_cost.toLocaleString()}</dd>
            <dt>기타비용</dt><dd>{data.profit_loss.other_cost.toLocaleString()}</dd>
            <dt>총비용</dt><dd>{data.profit_loss.total_cost.toLocaleString()}</dd>
            <dt>반품률</dt><dd>{data.return_rate.toFixed(1)}%</dd>
            <dt>교환률</dt><dd>{data.exchange_rate.toFixed(1)}%</dd>
            <dt>취소율</dt><dd>{data.cancel_rate.toFixed(1)}%</dd>
            <dt>고정비</dt><dd>{data.cost_type_breakdown.fixed_cost.toLocaleString()}</dd>
            <dt>변동비</dt><dd>{data.cost_type_breakdown.variable_cost.toLocaleString()}</dd>
          </dl>

          <h3>플랫폼별 매출</h3>
          <table className="data-table">
            <thead><tr><th>플랫폼</th><th>매출</th><th>주문건수</th></tr></thead>
            <tbody>
              {data.platform_breakdown.map((p) => (
                <tr key={p.platform_id}>
                  <td>{p.platform_name}</td>
                  <td>{p.revenue.toLocaleString()}</td>
                  <td>{p.order_count}</td>
                </tr>
              ))}
              {data.platform_breakdown.length === 0 && <tr><td colSpan={3}>데이터가 없습니다.</td></tr>}
            </tbody>
          </table>

          <h3>베스트 상품 Top 5</h3>
          <table className="data-table">
            <thead><tr><th>상품옵션ID</th><th>판매수량</th><th>매출</th><th>순이익</th><th>ROAS</th></tr></thead>
            <tbody>
              {data.best_products.map((p) => (
                <tr key={p.product_option_id}>
                  <td>{p.product_option_id}</td>
                  <td>{p.sales_qty}</td>
                  <td>{p.revenue.toLocaleString()}</td>
                  <td>{p.net_profit.toLocaleString()}</td>
                  <td>{p.roas.toFixed(1)}%</td>
                </tr>
              ))}
              {data.best_products.length === 0 && <tr><td colSpan={5}>데이터가 없습니다.</td></tr>}
            </tbody>
          </table>

          <h3>워스트 상품 Top 5</h3>
          <table className="data-table">
            <thead><tr><th>상품옵션ID</th><th>판매수량</th><th>매출</th><th>순이익</th><th>ROAS</th></tr></thead>
            <tbody>
              {data.worst_products.map((p) => (
                <tr key={p.product_option_id} className={p.net_profit < 0 ? 'row-warning' : ''}>
                  <td>{p.product_option_id}</td>
                  <td>{p.sales_qty}</td>
                  <td>{p.revenue.toLocaleString()}</td>
                  <td>{p.net_profit.toLocaleString()}</td>
                  <td>{p.roas.toFixed(1)}%</td>
                </tr>
              ))}
              {data.worst_products.length === 0 && <tr><td colSpan={5}>데이터가 없습니다.</td></tr>}
            </tbody>
          </table>

          <h3>광고 성과</h3>
          <table className="data-table">
            <thead><tr><th>광고플랫폼</th><th>노출</th><th>클릭</th><th>비용</th><th>전환매출</th><th>CPC</th><th>CPM</th><th>ROAS</th></tr></thead>
            <tbody>
              {data.ad_performance.map((a) => (
                <tr key={a.ad_platform_code}>
                  <td>{a.ad_platform_code}</td>
                  <td>{a.impressions.toLocaleString()}</td>
                  <td>{a.clicks.toLocaleString()}</td>
                  <td>{a.cost.toLocaleString()}</td>
                  <td>{a.conversion_amount.toLocaleString()}</td>
                  <td>{a.cpc.toLocaleString()}</td>
                  <td>{a.cpm.toLocaleString()}</td>
                  <td>{a.roas.toFixed(1)}%</td>
                </tr>
              ))}
              {data.ad_performance.length === 0 && <tr><td colSpan={8}>데이터가 없습니다.</td></tr>}
            </tbody>
          </table>

          {data.kpi_comparisons.length > 0 && (
            <>
              <h3>KPI 달성률</h3>
              <table className="data-table">
                <thead><tr><th>지표</th><th>목표</th><th>실적</th><th>달성률</th></tr></thead>
                <tbody>
                  {data.kpi_comparisons.map((k) => (
                    <tr key={k.metric}>
                      <td>{k.metric}</td>
                      <td>{k.target_value.toLocaleString()}</td>
                      <td>{k.actual_value.toLocaleString()}</td>
                      <td>{k.achievement_rate.toFixed(1)}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </>
      )}
    </div>
  )
}

function ReportSchedulesTab() {
  const { data, error, isLoading, reload } = useApiData<ReportSchedule[]>(() => api.get('/api/reports/schedules'), [])
  const [form, setForm] = useState<ReportScheduleCreate>({
    report_type: 'PROFIT_REPORT',
    frequency: 'MONTHLY',
    output_format: 'XLSX',
    next_run_at: `${todayIso()}T00:00`,
    recipient_emails: '',
  })
  const [createError, setCreateError] = useState<string | null>(null)

  const handleCreate = async (e: FormEvent) => {
    e.preventDefault()
    if (!form.report_type) {
      setCreateError('보고서 유형을 입력하세요.')
      return
    }
    setCreateError(null)
    try {
      await api.post('/api/reports/schedules', {
        ...form,
        next_run_at: new Date(form.next_run_at).toISOString(),
        recipient_emails: form.recipient_emails || null,
      })
      reload()
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.message : '예약 보고서 등록 중 오류가 발생했습니다.')
    }
  }

  async function handleToggle(schedule: ReportSchedule) {
    await api.patch(`/api/reports/schedules/${schedule.id}`, { is_enabled: !schedule.is_enabled })
    reload()
  }

  async function handleDelete(id: number) {
    await api.del(`/api/reports/schedules/${id}`)
    reload()
  }

  return (
    <div>
      <form className="inline-form" onSubmit={handleCreate}>
        <input
          value={form.report_type}
          onChange={(e) => setForm({ ...form, report_type: e.target.value })}
          placeholder="보고서 유형 (예: PROFIT_REPORT)"
        />
        <select value={form.frequency} onChange={(e) => setForm({ ...form, frequency: e.target.value })}>
          {FREQUENCY_OPTIONS.map((f) => <option key={f} value={f}>{f}</option>)}
        </select>
        <select value={form.output_format} onChange={(e) => setForm({ ...form, output_format: e.target.value })}>
          {OUTPUT_FORMAT_OPTIONS.map((f) => <option key={f} value={f}>{f}</option>)}
        </select>
        <input
          type="datetime-local"
          value={form.next_run_at}
          onChange={(e) => setForm({ ...form, next_run_at: e.target.value })}
        />
        <input
          value={form.recipient_emails ?? ''}
          onChange={(e) => setForm({ ...form, recipient_emails: e.target.value })}
          placeholder="수신 이메일(콤마 구분, 선택)"
        />
        <button type="submit">예약 등록</button>
      </form>
      {createError && <p className="form-error">{createError}</p>}
      <p className="form-info">frequency=MONTHLY인 예약만 스케줄러가 자동 생성합니다(직전 달 보고서, reports/generated에 저장).</p>

      {isLoading && <p>불러오는 중...</p>}
      {error && <p className="form-error">{error}</p>}
      {data && (
        <table className="data-table">
          <thead>
            <tr><th>유형</th><th>주기</th><th>형식</th><th>다음실행</th><th>마지막실행</th><th>활성</th><th></th></tr>
          </thead>
          <tbody>
            {data.map((s) => (
              <tr key={s.id}>
                <td>{s.report_type}</td>
                <td>{s.frequency}</td>
                <td>{s.output_format}</td>
                <td>{new Date(s.next_run_at).toLocaleString()}</td>
                <td>{s.last_run_at ? new Date(s.last_run_at).toLocaleString() : '-'}</td>
                <td><span className="status-badge">{s.is_enabled ? 'ON' : 'OFF'}</span></td>
                <td>
                  <button type="button" onClick={() => handleToggle(s)}>{s.is_enabled ? '비활성화' : '활성화'}</button>{' '}
                  <button type="button" onClick={() => handleDelete(s.id)}>삭제</button>
                </td>
              </tr>
            ))}
            {data.length === 0 && <tr><td colSpan={7}>등록된 예약 보고서가 없습니다.</td></tr>}
          </tbody>
        </table>
      )}
    </div>
  )
}

export function ReportsPage() {
  const [tab, setTab] = useState<'monthly' | 'schedules'>('monthly')

  return (
    <div>
      <h2>종합 보고서</h2>
      <div className="tabs">
        <button type="button" className={'tab-button' + (tab === 'monthly' ? ' active' : '')} onClick={() => setTab('monthly')}>
          월별 경영보고서
        </button>
        <button type="button" className={'tab-button' + (tab === 'schedules' ? ' active' : '')} onClick={() => setTab('schedules')}>
          예약 보고서
        </button>
      </div>
      {tab === 'monthly' && <MonthlyReportTab />}
      {tab === 'schedules' && <ReportSchedulesTab />}
    </div>
  )
}
