import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import { useApiData } from '../api/useApiData'
import type {
  CsBulkOutcomeItem,
  CsCase,
  CsCaseCreateResponse,
  CsCaseHistoryEntry,
  CsCaseMemo,
  CsDashboardSummary,
  CsReference,
  CsSyncResult,
  Platform,
} from '../api/types'

// 상용 ERP 확장(5단계, B묶음) - CS(고객문의) 통합 관리 화면. 채널 문의 조회
// 동기화(현재는 쿠팡 콜센터 문의만 공식 계약 확인됨)와 수기 생성 건을 같은
// 목록/상세에서 다룬다. 실제 채널 답변 전송은 이번 단계에 없다 - 답변 초안
// 저장까지만 지원하고 화면에 명확히 "미지원" 안내를 낸다. 내부 메모와 고객
// 답변 초안은 서로 다른 저장소를 쓰며 화면에서도 절대 같은 영역에 섞지 않는다.

const STATUS_LABELS: Record<string, string> = {
  OPEN: '접수',
  IN_PROGRESS: '처리중',
  WAITING_CUSTOMER: '고객답변대기',
  WAITING_CHANNEL: '채널응답대기',
  RESOLVED: '해결완료',
  CLOSED: '종결',
}

const PRIORITY_LABELS: Record<string, string> = { LOW: '낮음', NORMAL: '보통', HIGH: '높음', URGENT: '긴급' }

const BULK_OUTCOME_LABELS: Record<string, string> = {
  ACCEPTED: '접수됨',
  BLOCKED: '막힘',
  VALIDATION_FAILED: '검증 실패',
  NOT_FOUND: '대상 없음',
}

const STATUS_FILTER_OPTIONS = ['ALL', 'OPEN', 'IN_PROGRESS', 'WAITING_CUSTOMER', 'WAITING_CHANNEL', 'RESOLVED', 'CLOSED']

export function CsCasesPage() {
  const [statusFilter, setStatusFilter] = useState('ALL')
  const [platformId, setPlatformId] = useState<number | ''>('')
  const [inquiryType, setInquiryType] = useState('')
  const [priority, setPriority] = useState('')
  const [unassignedOnly, setUnassignedOnly] = useState(false)
  const [overdueOnly, setOverdueOnly] = useState(false)
  const [search, setSearch] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [activeCaseId, setActiveCaseId] = useState<number | null>(null)

  const [bulkAssignee, setBulkAssignee] = useState('')
  const [bulkRunning, setBulkRunning] = useState(false)
  const [bulkResults, setBulkResults] = useState<CsBulkOutcomeItem[] | null>(null)

  const [createOpen, setCreateOpen] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [createForm, setCreateForm] = useState({
    inquiry_type: '',
    customer_message: '',
    priority: 'NORMAL',
    subject: '',
    order_id: '',
    order_item_id: '',
    product_option_id: '',
    shipment_id: '',
  })
  const [duplicateWarning, setDuplicateWarning] = useState<number | null>(null)

  const { data: reference } = useApiData<CsReference>(() => api.get('/api/cs-cases/meta/reference'), [])
  const { data: platforms } = useApiData<Platform[]>(() => api.get('/api/platforms'), [])
  const { data: summary, reload: reloadSummary } = useApiData<CsDashboardSummary>(
    () => api.get('/api/cs-cases/dashboard'),
    [],
  )

  const query = new URLSearchParams()
  if (statusFilter !== 'ALL') query.set('status_filter', statusFilter)
  if (platformId) query.set('platform_id', String(platformId))
  if (inquiryType) query.set('inquiry_type', inquiryType)
  if (priority) query.set('priority', priority)
  if (unassignedOnly) query.set('unassigned_only', 'true')
  if (overdueOnly) query.set('overdue_only', 'true')
  if (search) query.set('search', search)

  const {
    data: cases,
    error: listError,
    isLoading: listLoading,
    reload: reloadList,
  } = useApiData<CsCase[]>(
    () => api.get(`/api/cs-cases?${query.toString()}`),
    [statusFilter, platformId, inquiryType, priority, unassignedOnly, overdueOnly, search],
  )
  const rows = useMemo(() => cases ?? [], [cases])

  const reloadAll = () => {
    reloadList()
    reloadSummary()
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

  const isOverdue = (c: CsCase) => c.due_at && !['RESOLVED', 'CLOSED'].includes(c.status) && new Date(c.due_at) < new Date()

  const runBulkAssign = async () => {
    if (bulkRunning || !bulkAssignee) return
    setBulkRunning(true)
    try {
      const outcomes = await api.post<CsBulkOutcomeItem[]>('/api/cs-cases/bulk/assign', {
        case_ids: [...selected],
        assignee_id: Number(bulkAssignee),
      })
      setBulkResults(outcomes)
      setSelected(new Set())
      reloadAll()
    } catch (err) {
      setBulkResults(null)
      alert(err instanceof ApiError ? err.message : '대량 배정 중 오류가 발생했습니다.')
    } finally {
      setBulkRunning(false)
    }
  }

  const runBulkStatus = async (newStatus: string) => {
    if (bulkRunning) return
    setBulkRunning(true)
    try {
      const outcomes = await api.post<CsBulkOutcomeItem[]>('/api/cs-cases/bulk/status', {
        case_ids: [...selected],
        new_status: newStatus,
      })
      setBulkResults(outcomes)
      setSelected(new Set())
      reloadAll()
    } catch (err) {
      setBulkResults(null)
      alert(err instanceof ApiError ? err.message : '대량 상태변경 중 오류가 발생했습니다.')
    } finally {
      setBulkRunning(false)
    }
  }

  const runCreate = async () => {
    if (creating) return
    if (!createForm.inquiry_type || !createForm.customer_message.trim()) {
      setCreateError('문의유형과 문의 내용을 입력하세요.')
      return
    }
    setCreating(true)
    setCreateError(null)
    try {
      const result = await api.post<CsCaseCreateResponse>('/api/cs-cases', {
        inquiry_type: createForm.inquiry_type,
        customer_message: createForm.customer_message,
        priority: createForm.priority,
        subject: createForm.subject || null,
        order_id: createForm.order_id ? Number(createForm.order_id) : null,
        order_item_id: createForm.order_item_id ? Number(createForm.order_item_id) : null,
        product_option_id: createForm.product_option_id ? Number(createForm.product_option_id) : null,
        shipment_id: createForm.shipment_id ? Number(createForm.shipment_id) : null,
      })
      setCreateOpen(false)
      setCreateForm({
        inquiry_type: '',
        customer_message: '',
        priority: 'NORMAL',
        subject: '',
        order_id: '',
        order_item_id: '',
        product_option_id: '',
        shipment_id: '',
      })
      setDuplicateWarning(result.duplicate_of_case_id)
      reloadAll()
      setActiveCaseId(result.case.id)
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.message : 'CS 케이스 생성 중 오류가 발생했습니다.')
    } finally {
      setCreating(false)
    }
  }

  return (
    <div className="cs-cases-page">
      <h2>CS 관리</h2>
      <p className="hint-text">
        채널 문의(현재 쿠팡 콜센터 문의만 조회 동기화 지원)와 수기 등록 문의를 함께 관리합니다. 실제 채널 답변
        전송은 아직 지원하지 않습니다 - 답변 초안 저장까지만 가능합니다.
      </p>

      {summary && (
        <div className="filter-bar">
          <span className="status-badge">미배정 {summary.unassigned_count}건</span>
          <span className="status-badge">지연 {summary.overdue_count}건</span>
          {Object.entries(summary.by_status).map(([s, count]) => (
            <span key={s} className="status-badge">
              {STATUS_LABELS[s] ?? s} {count}건
            </span>
          ))}
        </div>
      )}

      <div className="filter-bar">
        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          {STATUS_FILTER_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {s === 'ALL' ? '전체 상태' : STATUS_LABELS[s] ?? s}
            </option>
          ))}
        </select>
        <select value={platformId} onChange={(e) => setPlatformId(e.target.value ? Number(e.target.value) : '')}>
          <option value="">전체 채널</option>
          {(platforms ?? []).map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <select value={inquiryType} onChange={(e) => setInquiryType(e.target.value)}>
          <option value="">전체 문의유형</option>
          {(reference?.inquiry_types ?? []).map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
        <select value={priority} onChange={(e) => setPriority(e.target.value)}>
          <option value="">전체 우선순위</option>
          {(reference?.priorities ?? []).map((p) => (
            <option key={p} value={p}>
              {PRIORITY_LABELS[p] ?? p}
            </option>
          ))}
        </select>
        <label>
          <input type="checkbox" checked={unassignedOnly} onChange={(e) => setUnassignedOnly(e.target.checked)} /> 미배정만
        </label>
        <label>
          <input type="checkbox" checked={overdueOnly} onChange={(e) => setOverdueOnly(e.target.checked)} /> 지연만
        </label>
        <form
          onSubmit={(e) => {
            e.preventDefault()
            setSearch(searchInput.trim())
          }}
        >
          <input value={searchInput} onChange={(e) => setSearchInput(e.target.value)} placeholder="제목/외부ID/태그 검색" />
          <button type="submit">검색</button>
        </form>
        <button type="button" onClick={() => setCreateOpen(true)}>
          CS 건 생성
        </button>
      </div>

      {createOpen && (
        <div className="bulk-bar confirm-panel">
          <div>
            <strong>CS 건 수기 생성</strong>
            <div className="inline-form">
              <select
                value={createForm.inquiry_type}
                onChange={(e) => setCreateForm((f) => ({ ...f, inquiry_type: e.target.value }))}
              >
                <option value="">문의유형 선택</option>
                {(reference?.inquiry_types ?? []).map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
              <select value={createForm.priority} onChange={(e) => setCreateForm((f) => ({ ...f, priority: e.target.value }))}>
                {(reference?.priorities ?? []).map((p) => (
                  <option key={p} value={p}>
                    {PRIORITY_LABELS[p] ?? p}
                  </option>
                ))}
              </select>
              <input
                placeholder="제목(선택)"
                value={createForm.subject}
                onChange={(e) => setCreateForm((f) => ({ ...f, subject: e.target.value }))}
              />
            </div>
            <div className="inline-form">
              <input
                placeholder="연결할 주문 ID(선택)"
                type="number"
                value={createForm.order_id}
                onChange={(e) => setCreateForm((f) => ({ ...f, order_id: e.target.value }))}
                style={{ width: 150 }}
              />
              <input
                placeholder="주문라인 ID(선택)"
                type="number"
                value={createForm.order_item_id}
                onChange={(e) => setCreateForm((f) => ({ ...f, order_item_id: e.target.value }))}
                style={{ width: 150 }}
              />
              <input
                placeholder="상품옵션 ID(선택)"
                type="number"
                value={createForm.product_option_id}
                onChange={(e) => setCreateForm((f) => ({ ...f, product_option_id: e.target.value }))}
                style={{ width: 150 }}
              />
              <input
                placeholder="배송 ID(선택)"
                type="number"
                value={createForm.shipment_id}
                onChange={(e) => setCreateForm((f) => ({ ...f, shipment_id: e.target.value }))}
                style={{ width: 150 }}
              />
            </div>
            <textarea
              placeholder="문의 내용"
              value={createForm.customer_message}
              onChange={(e) => setCreateForm((f) => ({ ...f, customer_message: e.target.value }))}
              rows={3}
              style={{ width: '100%', marginTop: 8 }}
            />
            {createError && <p className="form-error">{createError}</p>}
          </div>
          <button type="button" disabled={creating} onClick={runCreate}>
            {creating ? '생성 중...' : '생성'}
          </button>
          <button type="button" onClick={() => setCreateOpen(false)}>
            취소
          </button>
        </div>
      )}
      {duplicateWarning !== null && (
        <p className="form-error">
          같은 주문에 대해 최근 접수된 케이스(#{duplicateWarning})가 있습니다 - 중복 문의인지 확인하세요.{' '}
          <button type="button" onClick={() => setDuplicateWarning(null)}>
            확인
          </button>
        </p>
      )}

      {listError && <p className="form-error">{listError}</p>}
      {listLoading ? (
        <p>불러오는 중...</p>
      ) : (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>
                  <input type="checkbox" checked={rows.length > 0 && selected.size === rows.length} onChange={toggleAll} />
                </th>
                <th>ID</th>
                <th>채널</th>
                <th>문의유형</th>
                <th>우선순위</th>
                <th>상태</th>
                <th>고객</th>
                <th>담당자</th>
                <th>기한</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((c) => (
                <tr key={c.id} className={[selected.has(c.id) ? 'sel' : '', activeCaseId === c.id ? 'clickable-row' : ''].join(' ')}>
                  <td>
                    <input type="checkbox" checked={selected.has(c.id)} onChange={() => toggle(c.id)} />
                  </td>
                  <td>{c.id}</td>
                  <td>{(platforms ?? []).find((p) => p.id === c.platform_id)?.name ?? '수기'}</td>
                  <td>{c.inquiry_type}</td>
                  <td>{PRIORITY_LABELS[c.priority] ?? c.priority}</td>
                  <td>
                    <span className="status-badge">{STATUS_LABELS[c.status] ?? c.status}</span>
                  </td>
                  <td>{c.customer_name_masked ?? '-'}</td>
                  <td>{c.assignee_id ?? '미배정'}</td>
                  <td>
                    {c.due_at ? new Date(c.due_at).toLocaleDateString() : '-'}
                    {isOverdue(c) && <span className="form-error"> 지연</span>}
                  </td>
                  <td>
                    <button type="button" onClick={() => setActiveCaseId(c.id)}>
                      상세
                    </button>
                  </td>
                </tr>
              ))}
              {rows.length === 0 && (
                <tr>
                  <td colSpan={10}>표시할 CS 건이 없습니다.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {selected.size > 0 && (
        <div className="bulk-bar">
          <span className="count">{selected.size}건 선택됨</span>
          <input
            placeholder="담당자 ID"
            value={bulkAssignee}
            onChange={(e) => setBulkAssignee(e.target.value)}
            style={{ width: 90 }}
          />
          <button type="button" disabled={bulkRunning || !bulkAssignee} onClick={runBulkAssign}>
            대량 배정
          </button>
          <button type="button" disabled={bulkRunning} onClick={() => runBulkStatus('IN_PROGRESS')}>
            대량 처리중 전환
          </button>
          <button type="button" disabled={bulkRunning} onClick={() => runBulkStatus('RESOLVED')}>
            대량 해결완료 전환
          </button>
          <button type="button" onClick={() => setSelected(new Set())}>
            선택 해제
          </button>
        </div>
      )}
      {bulkResults && (
        <ul>
          {bulkResults.map((r) => (
            <li key={r.case_id}>
              #{r.case_id}: {BULK_OUTCOME_LABELS[r.outcome] ?? r.outcome}
              {r.error_code ? ` (${r.error_code})` : ''}
            </li>
          ))}
        </ul>
      )}

      {activeCaseId !== null && (
        <CaseDetailPanel caseId={activeCaseId} platforms={platforms ?? []} onClose={() => setActiveCaseId(null)} onChanged={reloadAll} />
      )}
    </div>
  )
}

function CaseDetailPanel({
  caseId,
  platforms,
  onClose,
  onChanged,
}: {
  caseId: number
  platforms: Platform[]
  onClose: () => void
  onChanged: () => void
}) {
  const [actionError, setActionError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const [assigneeInput, setAssigneeInput] = useState('')
  const [statusInput, setStatusInput] = useState('')
  const [memoInput, setMemoInput] = useState('')
  const [draftInput, setDraftInput] = useState('')
  const [syncResult, setSyncResult] = useState<CsSyncResult | null>(null)
  const [syncing, setSyncing] = useState(false)

  const {
    data: caseDetail,
    error: detailError,
    isLoading: detailLoading,
    reload: reloadDetail,
  } = useApiData<CsCase>(() => api.get(`/api/cs-cases/${caseId}`), [caseId])

  const { data: history, reload: reloadHistory } = useApiData<CsCaseHistoryEntry[]>(
    () => api.get(`/api/cs-cases/${caseId}/history`),
    [caseId],
  )
  const { data: memos, reload: reloadMemos } = useApiData<CsCaseMemo[]>(
    () => api.get(`/api/cs-cases/${caseId}/memos`),
    [caseId],
  )

  const reloadAll = () => {
    reloadDetail()
    reloadHistory()
    reloadMemos()
    onChanged()
  }

  const runAssign = async () => {
    if (pending || !caseDetail) return
    setPending(true)
    setActionError(null)
    try {
      await api.post(`/api/cs-cases/${caseId}/assign`, {
        assignee_id: assigneeInput ? Number(assigneeInput) : null,
        expected_assignee_id: caseDetail.assignee_id,
      })
      setAssigneeInput('')
      reloadAll()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : '담당자 배정 중 오류가 발생했습니다.')
    } finally {
      setPending(false)
    }
  }

  const runStatusChange = async () => {
    if (pending || !caseDetail || !statusInput) return
    setPending(true)
    setActionError(null)
    try {
      await api.post(`/api/cs-cases/${caseId}/status`, {
        new_status: statusInput,
        expected_status: caseDetail.status,
      })
      setStatusInput('')
      reloadAll()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : '상태 변경 중 오류가 발생했습니다.')
    } finally {
      setPending(false)
    }
  }

  const runClose = async () => {
    if (pending || !caseDetail) return
    setPending(true)
    setActionError(null)
    try {
      await api.post(`/api/cs-cases/${caseId}/close`, { expected_status: caseDetail.status })
      reloadAll()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : '종결 처리 중 오류가 발생했습니다.')
    } finally {
      setPending(false)
    }
  }

  const runReopen = async () => {
    if (pending || !caseDetail) return
    setPending(true)
    setActionError(null)
    try {
      await api.post(`/api/cs-cases/${caseId}/reopen`, { expected_status: caseDetail.status })
      reloadAll()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : '재오픈 처리 중 오류가 발생했습니다.')
    } finally {
      setPending(false)
    }
  }

  const runAddMemo = async () => {
    if (pending || !memoInput.trim()) return
    setPending(true)
    setActionError(null)
    try {
      await api.post(`/api/cs-cases/${caseId}/memos`, { content: memoInput })
      setMemoInput('')
      reloadAll()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : '메모 추가 중 오류가 발생했습니다.')
    } finally {
      setPending(false)
    }
  }

  const runSaveDraft = async () => {
    if (pending || !caseDetail) return
    setPending(true)
    setActionError(null)
    try {
      await api.post(`/api/cs-cases/${caseId}/reply-draft`, {
        reply_draft: draftInput,
        expected_status: caseDetail.status,
      })
      reloadAll()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : '답변 초안 저장 중 오류가 발생했습니다.')
    } finally {
      setPending(false)
    }
  }

  const runSync = async () => {
    if (syncing || !caseDetail?.platform_id) return
    setSyncing(true)
    try {
      const result = await api.post<CsSyncResult>('/api/cs-cases/sync', { platform_id: caseDetail.platform_id, days: 7 })
      setSyncResult(result)
      reloadAll()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : '채널 동기화 중 오류가 발생했습니다.')
    } finally {
      setSyncing(false)
    }
  }

  if (detailLoading) return <p>불러오는 중...</p>
  if (detailError || !caseDetail) return <p className="form-error">{detailError ?? 'CS 케이스를 찾을 수 없습니다.'}</p>

  const platformName = platforms.find((p) => p.id === caseDetail.platform_id)?.name

  return (
    <div>
      <h3>
        CS #{caseId} 상세{' '}
        <button type="button" onClick={onClose}>
          닫기
        </button>
      </h3>
      {actionError && <p className="form-error">{actionError}</p>}

      <div className="filter-bar">
        <span className="status-badge">{STATUS_LABELS[caseDetail.status] ?? caseDetail.status}</span>
        <span>{platformName ?? '수기 생성'}</span>
        {caseDetail.external_inquiry_id && <span className="hint-text">외부ID: {caseDetail.external_inquiry_id}</span>}
      </div>

      <p>
        <strong>고객:</strong> {caseDetail.customer_name_masked ?? '-'} / {caseDetail.customer_phone_masked ?? '-'}
        {caseDetail.customer_phone_full && (
          <span className="hint-text"> (상세 권한 보유 - 전체: {caseDetail.customer_phone_full})</span>
        )}
      </p>
      {!caseDetail.customer_phone_full && caseDetail.customer_phone_masked && (
        <p className="hint-text">전체 전화번호/주소는 개인정보 상세 조회 권한(CS_PII_DETAIL)이 있어야 볼 수 있습니다.</p>
      )}

      <p>
        <strong>문의 내용:</strong> {caseDetail.customer_message}
      </p>

      <div className="filter-bar">
        {caseDetail.order_id && <Link to={`/orders/${caseDetail.order_id}`}>연결된 주문 #{caseDetail.order_id} 보기</Link>}
        {caseDetail.shipment_id && <span className="hint-text">연결된 배송 #{caseDetail.shipment_id}</span>}
        {caseDetail.claim_type && (
          <span className="hint-text">
            연결된 클레임: {caseDetail.claim_type} #{caseDetail.claim_id}
          </span>
        )}
      </div>

      <div className="inline-form">
        <input placeholder="담당자 ID(비우면 미배정)" value={assigneeInput} onChange={(e) => setAssigneeInput(e.target.value)} style={{ width: 140 }} />
        <button type="button" disabled={pending} onClick={runAssign}>
          담당자 배정
        </button>
      </div>

      <div className="inline-form">
        <select value={statusInput} onChange={(e) => setStatusInput(e.target.value)}>
          <option value="">상태 선택</option>
          {STATUS_FILTER_OPTIONS.filter((s) => s !== 'ALL' && s !== 'CLOSED').map((s) => (
            <option key={s} value={s}>
              {STATUS_LABELS[s] ?? s}
            </option>
          ))}
        </select>
        <button type="button" disabled={pending || !statusInput} onClick={runStatusChange}>
          상태 변경
        </button>
        {caseDetail.status === 'RESOLVED' && (
          <button type="button" disabled={pending} onClick={runClose}>
            종결 처리
          </button>
        )}
        {caseDetail.status === 'CLOSED' && (
          <button type="button" disabled={pending} onClick={runReopen}>
            재오픈
          </button>
        )}
      </div>

      <h4>답변 초안(고객에게 보낼 내용 - 내부 메모와 별개)</h4>
      <p className="form-error">
        실제 채널 답변 전송은 아직 지원하지 않습니다(UNSUPPORTED) - 여기 저장한 내용은 초안으로만 보관되며, 실제
        전송은 운영자가 채널 관리자센터에서 직접 처리해야 합니다.
      </p>
      <textarea
        value={draftInput || caseDetail.reply_draft || ''}
        onChange={(e) => setDraftInput(e.target.value)}
        rows={3}
        style={{ width: '100%' }}
      />
      <button type="button" disabled={pending} onClick={runSaveDraft}>
        답변 초안 저장
      </button>

      <h4>내부 메모(고객에게 노출되지 않음)</h4>
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>내용</th>
              <th>작성자</th>
              <th>작성시각</th>
            </tr>
          </thead>
          <tbody>
            {(memos ?? []).map((m) => (
              <tr key={m.id}>
                <td>{m.content}</td>
                <td>{m.created_by ?? '-'}</td>
                <td>{new Date(m.created_at).toLocaleString()}</td>
              </tr>
            ))}
            {(memos ?? []).length === 0 && (
              <tr>
                <td colSpan={3}>메모가 없습니다.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="inline-form">
        <input value={memoInput} onChange={(e) => setMemoInput(e.target.value)} placeholder="내부 메모 입력" style={{ flex: 1 }} />
        <button type="button" disabled={pending || !memoInput.trim()} onClick={runAddMemo}>
          메모 추가
        </button>
      </div>

      {caseDetail.platform_id && (
        <>
          <h4>채널 문의 수동 동기화</h4>
          <button type="button" disabled={syncing} onClick={runSync}>
            {syncing ? '동기화 중...' : '이 채널 문의 동기화 실행'}
          </button>
          {syncResult && (
            <p className="hint-text">
              {syncResult.platform_code}: {syncResult.status} (생성 {syncResult.created} / 갱신 {syncResult.updated} /
              실패 {syncResult.failed}){syncResult.reason_code ? ` - ${syncResult.reason_code}` : ''}
            </p>
          )}
        </>
      )}

      <h4>작업 이력</h4>
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>작업</th>
              <th>이전</th>
              <th>이후</th>
              <th>처리자</th>
              <th>시각</th>
            </tr>
          </thead>
          <tbody>
            {(history ?? []).map((h) => (
              <tr key={h.id}>
                <td>{h.action}</td>
                <td>{h.from_value ?? '-'}</td>
                <td>{h.to_value ?? '-'}</td>
                <td>{h.changed_by ?? '-'}</td>
                <td>{new Date(h.changed_at).toLocaleString()}</td>
              </tr>
            ))}
            {(history ?? []).length === 0 && (
              <tr>
                <td colSpan={5}>이력이 없습니다.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
