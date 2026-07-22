import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { useApiData } from '../api/useApiData'
import type { IntegrationStatus, SystemStatus, TaskExecutionHistoryItem } from '../api/types'

// UI 와이어프레임 v1.1 4장: 시스템 모니터링(연동상태/작업이력/시스템상태 탭 3종 통합).
type Tab = 'integrations' | 'task-history' | 'status'

const STATUS_ICON: Record<string, string> = { NORMAL: '🟢', ERROR: '🔴', TOKEN_EXPIRING: '🟡' }
const INTEGRATION_TYPE_LABEL: Record<string, string> = { MALL: '쇼핑몰', AD: '광고' }
const TASK_TYPE_OPTIONS = ['', 'ORDER_COLLECT', 'AD_COLLECT', 'BACKUP', 'REPORT_GENERATE', 'FULL_SYNC']
const TASK_STATUS_OPTIONS = ['', 'RUNNING', 'SUCCESS', 'FAILED']

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)}MB`
  return `${(bytes / 1024 / 1024 / 1024).toFixed(1)}GB`
}

function formatUptime(seconds: number): string {
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  return `${days}일 ${hours}시간 ${minutes}분`
}

function IntegrationsTab({ onDrillToTaskHistory }: { onDrillToTaskHistory: (taskType: string) => void }) {
  const navigate = useNavigate()
  const { data, error, isLoading } = useApiData<IntegrationStatus[]>(
    () => api.get('/api/system-monitor/integrations'),
    [],
  )

  if (isLoading) return <p>불러오는 중...</p>
  if (error) return <p className="form-error">{error}</p>
  if (!data || data.length === 0) return <p className="form-info">연동 상태 기록이 없습니다.</p>

  // UI 와이어프레임 v1.1 4장: 🔴오류 클릭 → 작업이력 탭, 🟡토큰만료임박 클릭 → 설정 > API Credential 관리로 이동.
  function handleRowClick(row: IntegrationStatus) {
    if (row.status === 'ERROR') {
      onDrillToTaskHistory(row.integration_type === 'MALL' ? 'ORDER_COLLECT' : 'AD_COLLECT')
    } else if (row.status === 'TOKEN_EXPIRING') {
      navigate('/settings')
    }
  }

  return (
    <table className="data-table">
      <thead>
        <tr><th>구분</th><th>플랫폼</th><th>상태</th><th>마지막 성공</th><th>마지막 오류</th></tr>
      </thead>
      <tbody>
        {data.map((row) => (
          <tr
            key={row.id}
            className={row.status === 'ERROR' || row.status === 'TOKEN_EXPIRING' ? 'row-warning clickable-row' : ''}
            onClick={row.status === 'ERROR' || row.status === 'TOKEN_EXPIRING' ? () => handleRowClick(row) : undefined}
          >
            <td>{INTEGRATION_TYPE_LABEL[row.integration_type] ?? row.integration_type}</td>
            <td>{row.integration_code}</td>
            <td>{STATUS_ICON[row.status] ?? ''} {row.status}</td>
            <td>{row.last_success_at ? new Date(row.last_success_at).toLocaleString() : '-'}</td>
            <td>{row.last_error_message ?? '-'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function TaskHistoryTab({ initialTaskType }: { initialTaskType: string }) {
  const [taskType, setTaskType] = useState(initialTaskType)
  const [statusFilter, setStatusFilter] = useState('')
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')

  const query = new URLSearchParams()
  if (taskType) query.set('task_type', taskType)
  if (statusFilter) query.set('status_filter', statusFilter)
  if (startDate) query.set('start_date', startDate)
  if (endDate) query.set('end_date', endDate)

  const { data, error, isLoading } = useApiData<TaskExecutionHistoryItem[]>(
    () => api.get(`/api/system-monitor/task-history?${query.toString()}`),
    [taskType, statusFilter, startDate, endDate],
  )

  return (
    <div>
      <div className="filter-bar">
        <select value={taskType} onChange={(e) => setTaskType(e.target.value)}>
          {TASK_TYPE_OPTIONS.map((t) => (
            <option key={t} value={t}>{t || '유형 전체'}</option>
          ))}
        </select>
        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          {TASK_STATUS_OPTIONS.map((s) => (
            <option key={s} value={s}>{s || '상태 전체'}</option>
          ))}
        </select>
        <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
        <input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
      </div>
      {isLoading && <p>불러오는 중...</p>}
      {error && <p className="form-error">{error}</p>}
      {data && (
        <table className="data-table">
          <thead>
            <tr><th>시작시각</th><th>유형</th><th>트리거</th><th>상태</th><th>결과</th></tr>
          </thead>
          <tbody>
            {data.map((row) => (
              <tr key={row.id} className={row.status === 'FAILED' ? 'row-warning' : ''}>
                <td>{new Date(row.started_at).toLocaleString()}</td>
                <td>{row.task_type}</td>
                <td>{row.trigger_type === 'SCHEDULE' ? '자동' : '수동'}</td>
                <td>{row.status}</td>
                <td>{row.status === 'FAILED' ? row.error_message : row.result_summary}</td>
              </tr>
            ))}
            {data.length === 0 && (
              <tr><td colSpan={5}>조건에 맞는 작업 이력이 없습니다.</td></tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  )
}

function StatusTab() {
  const { data, error, isLoading } = useApiData<SystemStatus>(() => api.get('/api/system-monitor/status'), [])

  if (isLoading) return <p>불러오는 중...</p>
  if (error) return <p className="form-error">{error}</p>
  if (!data) return null

  return (
    <div className="kpi-cards">
      <div className="kpi-card">
        <div className="kpi-label">DB 용량</div>
        <div className="kpi-value">{formatBytes(data.db_size_bytes)}</div>
      </div>
      <div className="kpi-card">
        <div className="kpi-label">로그 용량</div>
        <div className="kpi-value">{formatBytes(data.log_dir_size_bytes)}</div>
      </div>
      <div className="kpi-card">
        <div className="kpi-label">최근 백업</div>
        <div className="kpi-value">
          {data.latest_backup
            ? `${data.latest_backup.status} (${data.latest_backup.file_size_bytes ? formatBytes(data.latest_backup.file_size_bytes) : '-'})`
            : '백업 이력 없음'}
        </div>
        {data.latest_backup && <div className="kpi-label">{new Date(data.latest_backup.created_at).toLocaleString()}</div>}
      </div>
      <div className="kpi-card">
        <div className="kpi-label">서버 Uptime</div>
        <div className="kpi-value">{formatUptime(data.uptime_seconds)}</div>
      </div>
    </div>
  )
}

export function SystemMonitoringPage() {
  const [tab, setTab] = useState<Tab>('integrations')
  const [taskHistoryFilter, setTaskHistoryFilter] = useState('')

  function drillToTaskHistory(taskType: string) {
    setTaskHistoryFilter(taskType)
    setTab('task-history')
  }

  return (
    <div>
      <h2>시스템 모니터링</h2>
      <div className="tabs">
        <button type="button" className={'tab-button' + (tab === 'integrations' ? ' active' : '')} onClick={() => setTab('integrations')}>
          연동상태
        </button>
        <button type="button" className={'tab-button' + (tab === 'task-history' ? ' active' : '')} onClick={() => setTab('task-history')}>
          작업이력
        </button>
        <button type="button" className={'tab-button' + (tab === 'status' ? ' active' : '')} onClick={() => setTab('status')}>
          시스템상태
        </button>
      </div>
      {tab === 'integrations' && <IntegrationsTab onDrillToTaskHistory={drillToTaskHistory} />}
      {tab === 'task-history' && <TaskHistoryTab initialTaskType={taskHistoryFilter} />}
      {tab === 'status' && <StatusTab />}
    </div>
  )
}
