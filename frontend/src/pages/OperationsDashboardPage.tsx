import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import { useApiData } from '../api/useApiData'
import { useAutoRefreshData } from '../api/useAutoRefreshData'
import { useAuth } from '../auth/AuthContext'
import { DrillLink } from '../components/DrillLink'
import { MiniBarChart, type BarDatum } from '../components/MiniBarChart'
import type {
  OpsBulkRetryItem,
  OpsFailure,
  OpsFailureDetail,
  OpsFailureListOut,
  OpsIntegrationStatus,
  OpsSchedulerJob,
  OpsSeverity,
  OpsSummary,
  OpsTimeseriesPoint,
  Platform,
} from '../api/types'

// 상용 ERP 확장(6단계) - 통합 운영 대시보드. 기존 화면(출고관리/상품 대량처리/
// CS 관리/설정)은 그대로 두고, 이 화면은 요약 + 통합 실패 작업함 + 해당 상세
// 화면으로 이동하는 진입점 역할만 한다(요구사항 2번). 목록(failures)과 통계
// (summary/timeseries)를 분리된 API로 호출해 자동 새로고침 때마다 실패 행
// 전체를 다시 가져오지 않는다.

const REFRESH_INTERVAL_MS = 30_000
const FAILURE_STATUS_OPTIONS = ['', 'FAILED', 'RETRY_WAIT', 'UNKNOWN', 'RUNNING', 'PENDING', 'SUCCESS', 'CANCELLED']
const COMMAND_TYPE_OPTIONS = [
  '',
  'SHIPMENT_SUBMIT',
  'PRODUCT_CREATE',
  'PRODUCT_OPTION_CREATE',
  'INVENTORY_UPDATE',
  'SALE_STATUS_UPDATE',
  'PRODUCT_INFO_UPDATE',
]
const STATUS_LABELS: Record<string, string> = {
  PENDING: '대기',
  RUNNING: '실행중',
  RETRY_WAIT: '재시도대기',
  FAILED: '실패',
  UNKNOWN: '결과확인필요',
  SUCCESS: '성공',
  CANCELLED: '취소됨',
}
const SEVERITY_LABELS: Record<OpsSeverity, string> = {
  INFO: '정상',
  WARNING: '주의',
  ERROR: '오류',
  CRITICAL: '심각',
}
const RESOLUTION_OPTIONS = [
  { value: 'CONFIRMED_SUCCESS', label: '채널에서 성공 확인' },
  { value: 'CONFIRMED_NOT_SENT', label: '채널에서 미처리 확인' },
  { value: 'CONFIRMED_FAILED', label: '채널 미반영 확정(재시도 불필요)' },
]

function SeverityBadge({ severity }: { severity: OpsSeverity }) {
  return <span className={`severity-badge severity-${severity.toLowerCase()}`}>{SEVERITY_LABELS[severity]}</span>
}

function formatTime(iso: string | null): string {
  if (!iso) return '-'
  return new Date(iso.endsWith('Z') || iso.includes('+') ? iso : `${iso}Z`).toLocaleString()
}

export function OperationsDashboardPage() {
  const { user } = useAuth()
  const permissions = new Set(user?.permissions ?? [])
  const canRetry = permissions.has('OPERATIONS_RETRY')
  const canResolveUnknown = permissions.has('OPERATIONS_UNKNOWN_RESOLVE')
  const canViewScheduler = permissions.has('SYSTEM_MONITOR_VIEW')

  const {
    data: summary,
    error: summaryError,
    isLoading: summaryLoading,
    isRefreshing: summaryRefreshing,
    lastUpdatedAt,
    reload: reloadSummary,
    autoRefreshEnabled,
    setAutoRefreshEnabled,
  } = useAutoRefreshData<OpsSummary>(() => api.get('/api/operations/summary'), REFRESH_INTERVAL_MS, [])

  const { data: timeseries } = useAutoRefreshData<OpsTimeseriesPoint[]>(
    () => api.get('/api/operations/timeseries?days=7'),
    REFRESH_INTERVAL_MS,
    [],
  )

  const { data: integrations } = useAutoRefreshData<OpsIntegrationStatus[]>(
    () => api.get('/api/operations/integrations'),
    REFRESH_INTERVAL_MS,
    [],
  )

  const { data: schedulerJobs } = useAutoRefreshData<OpsSchedulerJob[]>(
    () => (canViewScheduler ? api.get('/api/operations/scheduler-jobs') : Promise.resolve([])),
    REFRESH_INTERVAL_MS,
    [canViewScheduler],
  )

  const { data: platforms } = useApiData<Platform[]>(() => api.get('/api/platforms'), [])

  // --- 통합 실패 작업함 ---
  const [commandType, setCommandType] = useState('')
  const [platformId, setPlatformId] = useState<number | ''>('')
  const [statusFilter, setStatusFilter] = useState('')
  const [limit] = useState(20)
  const [offset, setOffset] = useState(0)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [bulkRunning, setBulkRunning] = useState(false)
  const [bulkResults, setBulkResults] = useState<OpsBulkRetryItem[] | null>(null)
  const [activeCommandId, setActiveCommandId] = useState<number | null>(null)

  const failureQuery = new URLSearchParams()
  if (commandType) failureQuery.set('command_type', commandType)
  if (platformId) failureQuery.set('platform_id', String(platformId))
  if (statusFilter) failureQuery.set('status_filter', statusFilter)
  failureQuery.set('limit', String(limit))
  failureQuery.set('offset', String(offset))

  const {
    data: failureList,
    error: failureError,
    isLoading: failureLoading,
    reload: reloadFailures,
  } = useApiData<OpsFailureListOut>(
    () => api.get(`/api/operations/failures?${failureQuery.toString()}`),
    [commandType, platformId, statusFilter, limit, offset],
  )
  const rows = failureList?.items ?? []
  const total = failureList?.total ?? 0
  const totalPages = Math.max(1, Math.ceil(total / limit))
  const currentPage = Math.floor(offset / limit) + 1

  const reloadAll = () => {
    reloadSummary()
    reloadFailures()
  }

  const resetFiltersAndReload = () => {
    setOffset(0)
  }

  const toggle = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }
  const toggleAll = () => {
    setSelected((prev) => (prev.size === rows.length ? new Set() : new Set(rows.map((r) => r.id))))
  }

  const runBulkRetry = async () => {
    if (bulkRunning || selected.size === 0) return
    setBulkRunning(true)
    try {
      const outcomes = await api.post<OpsBulkRetryItem[]>('/api/operations/failures/bulk-retry', {
        command_ids: [...selected],
      })
      setBulkResults(outcomes)
      setSelected(new Set())
      reloadAll()
    } catch (err) {
      setBulkResults(null)
      alert(err instanceof ApiError ? err.message : '대량 재처리 중 오류가 발생했습니다.')
    } finally {
      setBulkRunning(false)
    }
  }

  const trendSuccess: BarDatum[] = (timeseries ?? []).map((p) => ({ key: p.date, label: p.date.slice(5), value: p.success }))
  const trendFailed: BarDatum[] = (timeseries ?? []).map((p) => ({ key: p.date, label: p.date.slice(5), value: p.failed }))

  const unresolvedUnknownCount =
    (summary?.shipment_commands_by_status.UNKNOWN ?? 0) + (summary?.product_commands_by_status.UNKNOWN ?? 0)

  return (
    <div className="operations-dashboard-page">
      <div className="ops-header">
        <h2>운영 대시보드</h2>
        <div className="ops-header-actions">
          <span className="hint-text">
            마지막 갱신: {lastUpdatedAt ? lastUpdatedAt.toLocaleTimeString() : '-'}
            {summaryRefreshing && ' (갱신 중...)'}
          </span>
          <label>
            <input
              type="checkbox"
              checked={autoRefreshEnabled}
              onChange={(e) => setAutoRefreshEnabled(e.target.checked)}
            />{' '}
            자동 새로고침(30초)
          </label>
          <button type="button" onClick={reloadAll}>
            수동 새로고침
          </button>
        </div>
      </div>

      {summaryError && <p className="form-error">요약 정보를 불러오지 못했습니다: {summaryError}</p>}
      {summaryLoading && !summary && <p>불러오는 중...</p>}

      {summary && (
        <>
          <div className="kpi-cards">
            <div className="kpi-card">
              <div className="kpi-label">오늘 수집된 주문(UTC 기준)</div>
              <div className="kpi-value">{summary.orders.collected_today}건</div>
            </div>
            <DrillLink to="/orders" filters={{ status_filter: 'NEW' }} className="kpi-card">
              <div className="kpi-label">미배송 주문</div>
              <div className={'kpi-value' + (summary.orders.delayed_unshipped > 0 ? ' negative' : '')}>
                {summary.orders.unshipped}건
              </div>
              {summary.orders.delayed_unshipped > 0 && (
                <div className="kpi-label negative">2일 이상 지연 {summary.orders.delayed_unshipped}건</div>
              )}
            </DrillLink>
            <Link to="/cs-cases" className="kpi-card">
              <div className="kpi-label">미배정 CS</div>
              <div className={'kpi-value' + (summary.cs.unassigned > 0 ? ' negative' : '')}>{summary.cs.unassigned}건</div>
            </Link>
            <Link to="/cs-cases" className="kpi-card">
              <div className="kpi-label">지연 CS</div>
              <div className={'kpi-value' + (summary.cs.overdue > 0 ? ' negative' : '')}>{summary.cs.overdue}건</div>
            </Link>
            <Link to="/order-conflicts" className="kpi-card">
              <div className="kpi-label">미해결 주문상태 충돌</div>
              <div className={'kpi-value' + (summary.order_status_conflicts_unresolved > 0 ? ' negative' : '')}>
                {summary.order_status_conflicts_unresolved}건
              </div>
            </Link>
            <div className={'kpi-card' + (unresolvedUnknownCount > 0 ? '' : '')}>
              <div className="kpi-label negative">UNKNOWN(결과확인필요)</div>
              <div className={'kpi-value' + (unresolvedUnknownCount > 0 ? ' negative' : '')}>{unresolvedUnknownCount}건</div>
            </div>
          </div>

          <div className="kpi-cards">
            <div className="kpi-card">
              <div className="kpi-label">출고대기</div>
              <div className="kpi-value">{summary.fulfillment.by_kpi_bucket.READY_TO_PICK ?? 0}건</div>
            </div>
            <div className="kpi-card">
              <div className="kpi-label">피킹중</div>
              <div className="kpi-value">{summary.fulfillment.by_kpi_bucket.PICKING ?? 0}건</div>
            </div>
            <div className="kpi-card">
              <div className="kpi-label">검수중</div>
              <div className="kpi-value">{summary.fulfillment.by_kpi_bucket.INSPECTING ?? 0}건</div>
            </div>
            <div className="kpi-card">
              <div className="kpi-label">포장완료</div>
              <div className="kpi-value">{summary.fulfillment.by_kpi_bucket.PACKED ?? 0}건</div>
            </div>
            <DrillLink to="/exchanges" filters={{ status_filter: 'REQUESTED' }} className="kpi-card">
              <div className="kpi-label">교환 대기</div>
              <div className="kpi-value">{summary.claims_pending.exchange}건</div>
            </DrillLink>
            <DrillLink to="/returns" filters={{ status_filter: 'REQUESTED' }} className="kpi-card">
              <div className="kpi-label">반품 대기</div>
              <div className="kpi-value">{summary.claims_pending.return}건</div>
            </DrillLink>
            <DrillLink to="/cancellations" filters={{ status_filter: 'REQUESTED' }} className="kpi-card">
              <div className="kpi-label">취소 대기</div>
              <div className="kpi-value">{summary.claims_pending.cancellation}건</div>
            </DrillLink>
          </div>

          <div className="kpi-cards">
            <div className="kpi-card">
              <div className="kpi-label">최근 24시간 명령 성공률</div>
              <div className="kpi-value">
                {summary.success_rate.last_24h.rate_percent === null
                  ? 'N/A'
                  : `${summary.success_rate.last_24h.rate_percent}%`}
              </div>
              <div className="kpi-label">
                성공 {summary.success_rate.last_24h.success} / 실패 {summary.success_rate.last_24h.failed}
              </div>
            </div>
            <div className="kpi-card">
              <div className="kpi-label">최근 7일 명령 성공률</div>
              <div className="kpi-value">
                {summary.success_rate.last_7d.rate_percent === null ? 'N/A' : `${summary.success_rate.last_7d.rate_percent}%`}
              </div>
              <div className="kpi-label">
                성공 {summary.success_rate.last_7d.success} / 실패 {summary.success_rate.last_7d.failed}
              </div>
            </div>
          </div>
          <p className="hint-text">{summary.success_rate.scope_note}</p>

          <h3>최근 7일 명령 성공/실패 추이(일별, UTC 기준)</h3>
          <div className="ops-trend-row">
            <div>
              <p className="hint-text">성공</p>
              <MiniBarChart data={trendSuccess} height={100} />
            </div>
            <div>
              <p className="hint-text">실패</p>
              <MiniBarChart data={trendFailed} height={100} />
            </div>
          </div>

          <h3>채널별 연동 상태</h3>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>구분</th>
                  <th>채널</th>
                  <th>상태</th>
                  <th>심각도</th>
                  <th>마지막 성공</th>
                  <th>마지막 실패</th>
                  <th>실패 사유(안전요약)</th>
                </tr>
              </thead>
              <tbody>
                {(integrations ?? []).map((row) => (
                  <tr key={`${row.integration_type}-${row.integration_code}`}>
                    <td>{row.integration_type}</td>
                    <td>{row.integration_code}</td>
                    <td>{row.status}</td>
                    <td>
                      <SeverityBadge severity={row.severity} />
                    </td>
                    <td>{formatTime(row.last_success_at)}</td>
                    <td>{formatTime(row.last_error_at)}</td>
                    <td>{row.last_error_message ?? '-'}</td>
                  </tr>
                ))}
                {(integrations ?? []).length === 0 && (
                  <tr>
                    <td colSpan={7} style={{ textAlign: 'center', color: 'var(--text-muted)' }}>
                      아직 기록된 연동 상태가 없습니다.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          {canViewScheduler && (
            <>
              <h3>scheduler 잡 상태(잡별 최근 실행)</h3>
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>잡</th>
                      <th>상태</th>
                      <th>심각도</th>
                      <th>시작</th>
                      <th>종료</th>
                      <th>오류</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(schedulerJobs ?? []).map((job) => (
                      <tr key={job.target ?? job.task_type}>
                        <td>{job.target ?? job.task_type}</td>
                        <td>{job.status}</td>
                        <td>
                          <SeverityBadge severity={job.severity} />
                        </td>
                        <td>{formatTime(job.started_at)}</td>
                        <td>{formatTime(job.finished_at)}</td>
                        <td>{job.error_message ?? '-'}</td>
                      </tr>
                    ))}
                    {(schedulerJobs ?? []).length === 0 && (
                      <tr>
                        <td colSpan={6} style={{ textAlign: 'center', color: 'var(--text-muted)' }}>
                          아직 실행 이력이 없습니다.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </>
      )}

      <h3>통합 실패 작업함</h3>
      <p className="hint-text">
        송장 전송/상품 등록/옵션조합 등록/재고 전송/판매상태 전송/상품정보 수정 명령의 실패·재시도대기·결과확인필요
        (UNKNOWN)·실행중 건을 한 곳에서 확인합니다. UNKNOWN은 대량 재처리 대상이 아닙니다 - 외부 채널을 직접 확인한
        뒤 개별적으로 해소해야 합니다.
      </p>

      <div className="filter-bar">
        <select
          value={commandType}
          onChange={(e) => {
            setCommandType(e.target.value)
            resetFiltersAndReload()
          }}
        >
          {COMMAND_TYPE_OPTIONS.map((t) => (
            <option key={t} value={t}>
              {t || '전체 기능유형'}
            </option>
          ))}
        </select>
        <select
          value={platformId}
          onChange={(e) => {
            setPlatformId(e.target.value ? Number(e.target.value) : '')
            resetFiltersAndReload()
          }}
        >
          <option value="">전체 채널</option>
          {(platforms ?? []).map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <select
          value={statusFilter}
          onChange={(e) => {
            setStatusFilter(e.target.value)
            resetFiltersAndReload()
          }}
        >
          {FAILURE_STATUS_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {s ? `${STATUS_LABELS[s] ?? s}만 보기` : '실패/재시도대기/UNKNOWN/실행중(기본)'}
            </option>
          ))}
        </select>
      </div>

      {canRetry && selected.size > 0 && (
        <div className="bulk-bar">
          <span className="count">{selected.size}건 선택됨</span>
          <button type="button" disabled={bulkRunning} onClick={runBulkRetry}>
            {bulkRunning ? '재처리 중...' : '선택 재처리(FAILED만 대상)'}
          </button>
          <button type="button" onClick={() => setSelected(new Set())}>
            선택 해제
          </button>
        </div>
      )}
      {bulkResults && (
        <div className="filter-bar">
          {bulkResults.map((r) => (
            <span key={r.command_id} className={r.outcome === 'RETRIED' ? 'status-badge' : 'badge-negative'}>
              #{r.command_id}: {r.outcome}
              {r.error_code ? `(${r.error_code})` : ''}
            </span>
          ))}
          <button type="button" onClick={() => setBulkResults(null)}>
            닫기
          </button>
        </div>
      )}

      {failureError && <p className="form-error">{failureError}</p>}
      {failureLoading ? (
        <p>불러오는 중...</p>
      ) : (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                {canRetry && (
                  <th>
                    <input
                      type="checkbox"
                      checked={rows.length > 0 && selected.size === rows.length}
                      onChange={toggleAll}
                    />
                  </th>
                )}
                <th>ID</th>
                <th>기능유형</th>
                <th>채널</th>
                <th>대상</th>
                <th>상태</th>
                <th>시도횟수</th>
                <th>마지막 시도</th>
                <th>다음 재시도</th>
                <th>오류코드</th>
                <th>상세화면</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 && (
                <tr>
                  <td colSpan={canRetry ? 11 : 10} style={{ textAlign: 'center', color: 'var(--text-muted)' }}>
                    조건에 맞는 실패 작업이 없습니다.
                  </td>
                </tr>
              )}
              {rows.map((row) => (
                <FailureRow
                  key={row.id}
                  row={row}
                  selected={selected.has(row.id)}
                  canRetry={canRetry}
                  canResolveUnknown={canResolveUnknown}
                  isActive={activeCommandId === row.id}
                  onToggle={() => toggle(row.id)}
                  onOpenDetail={() => setActiveCommandId(activeCommandId === row.id ? null : row.id)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="pagination">
        <span>
          전체 {total}건 · {currentPage} / {totalPages} 페이지
        </span>
        <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>
          ‹ 이전
        </button>
        <button disabled={currentPage >= totalPages} onClick={() => setOffset(offset + limit)}>
          다음 ›
        </button>
      </div>

      {activeCommandId !== null && (
        <FailureDetailPanel
          commandId={activeCommandId}
          canResolveUnknown={canResolveUnknown}
          onClose={() => setActiveCommandId(null)}
          onResolved={() => {
            setActiveCommandId(null)
            reloadAll()
          }}
        />
      )}
    </div>
  )
}

function FailureRow({
  row,
  selected,
  canRetry,
  canResolveUnknown,
  isActive,
  onToggle,
  onOpenDetail,
}: {
  row: OpsFailure
  selected: boolean
  canRetry: boolean
  canResolveUnknown: boolean
  isActive: boolean
  onToggle: () => void
  onOpenDetail: () => void
}) {
  return (
    <tr className={[selected ? 'sel' : '', 'clickable-row'].join(' ')}>
      {canRetry && (
        <td>
          <input type="checkbox" checked={selected} onChange={onToggle} disabled={row.status !== 'FAILED'} />
        </td>
      )}
      <td>{row.id}</td>
      <td>{row.command_type}</td>
      <td>{row.platform_code ?? row.platform_id}</td>
      <td>
        {row.target_type} #{row.target_id}
      </td>
      <td>
        {row.status === 'UNKNOWN' ? <span className="badge-negative">{STATUS_LABELS[row.status]}</span> : STATUS_LABELS[row.status] ?? row.status}
      </td>
      <td>{row.attempt_count}</td>
      <td>{formatTime(row.created_at)}</td>
      <td>{formatTime(row.next_retry_at)}</td>
      <td>{row.error_code ?? '-'}</td>
      <td>
        <Link to={row.detail_link}>이동</Link>
      </td>
      <td>
        <button type="button" onClick={onOpenDetail}>
          {isActive ? '닫기' : row.status === 'UNKNOWN' && canResolveUnknown ? 'UNKNOWN 해소' : '상세'}
        </button>
      </td>
    </tr>
  )
}

function FailureDetailPanel({
  commandId,
  canResolveUnknown,
  onClose,
  onResolved,
}: {
  commandId: number
  canResolveUnknown: boolean
  onClose: () => void
  onResolved: () => void
}) {
  const { data: detail, error, isLoading } = useApiData<OpsFailureDetail>(
    () => api.get(`/api/operations/failures/${commandId}`),
    [commandId],
  )
  const [resolution, setResolution] = useState(RESOLUTION_OPTIONS[0].value)
  const [evidenceNote, setEvidenceNote] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)

  const runResolve = async () => {
    if (submitting) return
    if (evidenceNote.trim().length < 5) {
      setSubmitError('확인 근거를 5자 이상 입력하세요(예: 채널 관리자 화면에서 확인한 방법과 시각).')
      return
    }
    setSubmitting(true)
    setSubmitError(null)
    try {
      await api.post(`/api/operations/failures/${commandId}/resolve-unknown`, {
        resolution,
        evidence_note: evidenceNote,
      })
      onResolved()
    } catch (err) {
      setSubmitError(err instanceof ApiError ? err.message : 'UNKNOWN 해소 중 오류가 발생했습니다.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="bulk-bar confirm-panel">
      <div>
        <strong>실패 작업 #{commandId} 상세</strong>
        {isLoading && <p>불러오는 중...</p>}
        {error && <p className="form-error">{error}</p>}
        {detail && (
          <>
            <p className="hint-text">
              {detail.command_type} · {STATUS_LABELS[detail.status] ?? detail.status} · 시도 {detail.attempt_count}회
            </p>
            {detail.request_summary && <p className="hint-text">요청 요약: {detail.request_summary}</p>}
            {detail.response_summary && <p className="hint-text">응답 요약: {detail.response_summary}</p>}

            {detail.status === 'UNKNOWN' && (
              <>
                <p className="form-error">
                  UNKNOWN은 채널이 실제로 처리했는지 알 수 없는 상태입니다 - 자동/대량 재처리를 절대 하지 마세요.
                  외부 채널(판매자센터 등)에서 직접 확인한 뒤에만 아래에서 해소하세요.
                </p>
                {canResolveUnknown ? (
                  <>
                    <div className="inline-form">
                      <select value={resolution} onChange={(e) => setResolution(e.target.value)}>
                        {RESOLUTION_OPTIONS.map((o) => (
                          <option key={o.value} value={o.value}>
                            {o.label}
                          </option>
                        ))}
                      </select>
                    </div>
                    <textarea
                      placeholder="확인 근거(예: 쿠팡 판매자센터 주문상세에서 배송중 상태 확인, 2026-03-05 10:00)"
                      value={evidenceNote}
                      onChange={(e) => setEvidenceNote(e.target.value)}
                      rows={2}
                      style={{ width: '100%', marginTop: 8 }}
                    />
                    {submitError && <p className="form-error">{submitError}</p>}
                    <button type="button" disabled={submitting} onClick={runResolve} style={{ marginTop: 8 }}>
                      {submitting ? '처리 중...' : '해소 반영'}
                    </button>
                  </>
                ) : (
                  <p className="hint-text">UNKNOWN 해소 권한이 없습니다 - 권한자에게 요청하세요.</p>
                )}
              </>
            )}
          </>
        )}
      </div>
      <button type="button" onClick={onClose}>
        닫기
      </button>
    </div>
  )
}
