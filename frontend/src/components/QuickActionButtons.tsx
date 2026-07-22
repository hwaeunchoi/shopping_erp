import { useEffect, useState } from 'react'
import { useAuth } from '../auth/AuthContext'
import { api, ApiError } from '../api/client'
import type { TaskExecutionHistoryItem, TaskTriggerResult } from '../api/types'

// UI 와이어프레임 v1.1 3장: 대시보드 빠른실행 버튼 5종.
// 클릭 → 확인 모달 → POST /api/tasks/trigger → 완료 토스트 + 최근 실행 요약 갱신.
type TaskType = 'ORDER_COLLECT' | 'AD_COLLECT' | 'REPORT_GENERATE' | 'BACKUP' | 'FULL_SYNC'

// api/routers/tasks.py TASK_TYPE_PERMISSION과 동일한 매핑 - 권한 없는 버튼은 숨긴다.
const ACTIONS: Array<{ taskType: TaskType; label: string; confirmMessage: string; permission: string }> = [
  { taskType: 'ORDER_COLLECT', label: '주문수집', confirmMessage: '전체 플랫폼 주문을 지금 수집할까요?', permission: 'ORDER_EDIT' },
  { taskType: 'AD_COLLECT', label: '광고수집', confirmMessage: '전체 광고 플랫폼 성과를 지금 수집할까요?', permission: 'AD_MANAGE' },
  { taskType: 'REPORT_GENERATE', label: '보고서생성', confirmMessage: '이번 달 경영보고서를 지금 생성할까요?', permission: 'REPORT_VIEW' },
  { taskType: 'BACKUP', label: '백업', confirmMessage: 'DB를 지금 백업할까요?', permission: 'SETTINGS_MANAGE' },
  { taskType: 'FULL_SYNC', label: '전체동기화', confirmMessage: '주문/광고/정산/고객통계를 전체 동기화할까요?', permission: 'ORDER_EDIT' },
]

const STATUS_LABEL: Record<string, string> = { SUCCESS: '성공', FAILED: '실패', RUNNING: '진행중' }

export function QuickActionButtons() {
  const { user } = useAuth()
  const permissions = new Set(user?.permissions ?? [])
  const visibleActions = ACTIONS.filter((a) => permissions.has(a.permission))

  const [runningType, setRunningType] = useState<string | null>(null)
  const [toast, setToast] = useState<{ text: string; isError: boolean } | null>(null)
  const [lastRuns, setLastRuns] = useState<Record<string, TaskExecutionHistoryItem>>({})

  function loadRecent() {
    api
      .get<TaskExecutionHistoryItem[]>('/api/system-monitor/task-history')
      .then((items) => {
        const byType: Record<string, TaskExecutionHistoryItem> = {}
        for (const item of items) {
          if (!byType[item.task_type]) byType[item.task_type] = item
        }
        setLastRuns(byType)
      })
      .catch(() => {
        // 최근 실행 요약은 부가 정보라 실패해도 화면 흐름을 막지 않는다.
      })
  }

  useEffect(() => {
    loadRecent()
  }, [])

  useEffect(() => {
    if (!toast) return
    const timer = setTimeout(() => setToast(null), 4000)
    return () => clearTimeout(timer)
  }, [toast])

  async function handleClick(action: (typeof ACTIONS)[number]) {
    if (!window.confirm(action.confirmMessage)) return
    setRunningType(action.taskType)
    try {
      const result = await api.post<TaskTriggerResult>('/api/tasks/trigger', { task_type: action.taskType })
      setToast({ text: `${action.label} 완료: ${result.result_summary ?? ''}`, isError: false })
    } catch (err) {
      setToast({
        text: err instanceof ApiError ? err.message : `${action.label} 실행 중 오류가 발생했습니다.`,
        isError: true,
      })
    } finally {
      setRunningType(null)
      loadRecent()
    }
  }

  return (
    <div className="quick-actions">
      <div className="quick-actions-buttons">
        {visibleActions.map((action) => (
          <button
            key={action.taskType}
            type="button"
            onClick={() => handleClick(action)}
            disabled={runningType !== null}
          >
            {runningType === action.taskType ? '실행 중...' : `▶ ${action.label}`}
          </button>
        ))}
      </div>
      <div className="quick-actions-summary">
        최근 실행:{' '}
        {ACTIONS.map((action, i) => {
          const last = lastRuns[action.taskType]
          return (
            <span key={action.taskType}>
              {i > 0 && ' | '}
              {action.label}{' '}
              {last ? `${new Date(last.started_at).toLocaleTimeString()} ${STATUS_LABEL[last.status] ?? last.status}` : '-'}
            </span>
          )
        })}
      </div>
      {toast && <div className={'toast' + (toast.isError ? ' toast-error' : '')}>{toast.text}</div>}
    </div>
  )
}
