import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'
import { useApiData } from '../api/useApiData'
import type { AlertRule, AlertRuleCreate, AppNotification } from '../api/types'

// UI v1.0 알림센터: 알림 목록 + 사용자 정의 알림 규칙(alert_rules) 관리.
const METRIC_OPTIONS = ['ORDER_COUNT_TODAY', 'AD_COST', 'ROAS', 'RETURN_RATE', 'UNSHIPPED_DAYS', 'API_FAILURE', 'BACKUP_FAILURE']
const OPERATOR_OPTIONS = ['LT', 'LTE', 'GT', 'GTE', 'EQ']

function NotificationsTab() {
  const { data, error, isLoading, reload } = useApiData<AppNotification[]>(() => api.get('/api/notifications'), [])

  async function handleMarkRead(id: number) {
    await api.patch(`/api/notifications/${id}/read`)
    reload()
  }

  async function handleMarkAllRead() {
    await api.post('/api/notifications/read-all')
    reload()
  }

  return (
    <div>
      <div className="filter-bar">
        <button type="button" onClick={handleMarkAllRead}>전체 읽음 처리</button>
      </div>
      {isLoading && <p>불러오는 중...</p>}
      {error && <p className="form-error">{error}</p>}
      {data && (
        <table className="data-table">
          <thead>
            <tr><th>심각도</th><th>유형</th><th>메시지</th><th>발생시각</th><th>상태</th><th></th></tr>
          </thead>
          <tbody>
            {data.map((n) => (
              <tr key={n.id} className={!n.is_read ? 'row-warning' : ''}>
                <td>{n.severity}</td>
                <td>{n.type}</td>
                <td>{n.message}</td>
                <td>{new Date(n.created_at).toLocaleString()}</td>
                <td>{n.is_read ? '읽음' : '안읽음'}</td>
                <td>{!n.is_read && <button type="button" onClick={() => handleMarkRead(n.id)}>읽음 처리</button>}</td>
              </tr>
            ))}
            {data.length === 0 && <tr><td colSpan={6}>알림이 없습니다.</td></tr>}
          </tbody>
        </table>
      )}
    </div>
  )
}

function AlertRulesTab() {
  const { data, error, isLoading, reload } = useApiData<AlertRule[]>(() => api.get('/api/alert-rules'), [])
  const [form, setForm] = useState<AlertRuleCreate>({ name: '', metric: METRIC_OPTIONS[0], operator: 'GT', threshold_value: undefined })
  const [createError, setCreateError] = useState<string | null>(null)

  const handleCreate = async (e: FormEvent) => {
    e.preventDefault()
    if (!form.name) {
      setCreateError('규칙 이름을 입력하세요.')
      return
    }
    setCreateError(null)
    try {
      await api.post('/api/alert-rules', form)
      setForm({ name: '', metric: METRIC_OPTIONS[0], operator: 'GT', threshold_value: undefined })
      reload()
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.message : '알림 규칙 등록 중 오류가 발생했습니다.')
    }
  }

  async function handleToggle(rule: AlertRule) {
    await api.patch(`/api/alert-rules/${rule.id}`, { is_enabled: !rule.is_enabled })
    reload()
  }

  async function handleDelete(id: number) {
    await api.del(`/api/alert-rules/${id}`)
    reload()
  }

  return (
    <div>
      <form className="inline-form" onSubmit={handleCreate}>
        <input
          value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
          placeholder="규칙 이름"
        />
        <select value={form.metric} onChange={(e) => setForm({ ...form, metric: e.target.value })}>
          {METRIC_OPTIONS.map((m) => <option key={m} value={m}>{m}</option>)}
        </select>
        <select value={form.operator} onChange={(e) => setForm({ ...form, operator: e.target.value })}>
          {OPERATOR_OPTIONS.map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
        <input
          type="number"
          value={form.threshold_value ?? ''}
          onChange={(e) => setForm({ ...form, threshold_value: e.target.value ? Number(e.target.value) : undefined })}
          placeholder="기준값"
        />
        <button type="submit">규칙 등록</button>
      </form>
      {createError && <p className="form-error">{createError}</p>}

      {isLoading && <p>불러오는 중...</p>}
      {error && <p className="form-error">{error}</p>}
      {data && (
        <table className="data-table">
          <thead>
            <tr><th>이름</th><th>지표</th><th>조건</th><th>주기</th><th>활성</th><th></th></tr>
          </thead>
          <tbody>
            {data.map((rule) => (
              <tr key={rule.id}>
                <td>{rule.name}</td>
                <td>{rule.metric}</td>
                <td>{rule.operator} {rule.threshold_value}</td>
                <td>{rule.check_frequency}</td>
                <td><span className="status-badge">{rule.is_enabled ? 'ON' : 'OFF'}</span></td>
                <td>
                  <button type="button" onClick={() => handleToggle(rule)}>{rule.is_enabled ? '비활성화' : '활성화'}</button>{' '}
                  <button type="button" onClick={() => handleDelete(rule.id)}>삭제</button>
                </td>
              </tr>
            ))}
            {data.length === 0 && <tr><td colSpan={6}>등록된 알림 규칙이 없습니다.</td></tr>}
          </tbody>
        </table>
      )}
    </div>
  )
}

export function NotificationsPage() {
  const [tab, setTab] = useState<'notifications' | 'rules'>('notifications')

  return (
    <div>
      <h2>알림센터</h2>
      <div className="tabs">
        <button type="button" className={'tab-button' + (tab === 'notifications' ? ' active' : '')} onClick={() => setTab('notifications')}>
          알림 목록
        </button>
        <button type="button" className={'tab-button' + (tab === 'rules' ? ' active' : '')} onClick={() => setTab('rules')}>
          알림 규칙
        </button>
      </div>
      {tab === 'notifications' && <NotificationsTab />}
      {tab === 'rules' && <AlertRulesTab />}
    </div>
  )
}
