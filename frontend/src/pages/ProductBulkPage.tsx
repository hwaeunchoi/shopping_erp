import { useEffect, useMemo, useState } from 'react'
import { api, ApiError } from '../api/client'
import { useApiData } from '../api/useApiData'
import type {
  BulkCommandStatus,
  BulkItemResult,
  BulkOutcome,
  BulkRetryResult,
  BulkSubmitResult,
  BulkTarget,
  Platform,
} from '../api/types'

// 상용 ERP 확장(3단계, 네 번째 묶음) - 상품/옵션조합 등록과 재고/판매상태/정보수정을
// 채널별로 대량 접수하고, 항목별 결과를 독립적으로 확인·재처리하는 화면. 기존 상품
// 상세 화면(ProductDetailPage)의 단건 버튼/흐름은 그대로 두고, 이 화면은 같은
// 백엔드 서비스(api/routers/products_bulk.py -> services.product_bulk_service)를
// 재사용할 뿐 단건 흐름을 복제하지 않는다.

type Kind = 'PUBLISH' | 'OPTION_PUBLISH' | 'INVENTORY' | 'SALE_STATUS' | 'INFO_UPDATE'

const KIND_CONFIG: Record<Kind, { label: string; listPath: string; submitPath: string; usesMapping: boolean }> = {
  PUBLISH: {
    label: '신규 등록(단일 SKU)',
    listPath: '/api/products/bulk/publish-drafts',
    submitPath: '/api/products/bulk/publish-drafts/submit',
    usesMapping: false,
  },
  OPTION_PUBLISH: {
    label: '옵션조합 등록',
    listPath: '/api/products/bulk/option-publish-drafts',
    submitPath: '/api/products/bulk/option-publish-drafts/submit',
    usesMapping: false,
  },
  INVENTORY: {
    label: '재고 수량',
    listPath: '/api/products/bulk/platform-maps',
    submitPath: '/api/products/bulk/platform-map/sync-inventory/submit',
    usesMapping: true,
  },
  SALE_STATUS: {
    label: '판매상태',
    listPath: '/api/products/bulk/platform-maps',
    submitPath: '/api/products/bulk/platform-map/sync-sale-status/submit',
    usesMapping: true,
  },
  INFO_UPDATE: {
    label: '상품정보 수정',
    listPath: '/api/products/bulk/platform-maps',
    submitPath: '/api/products/bulk/platform-map/update-info/submit',
    usesMapping: true,
  },
}

const OUTCOME_LABELS: Record<BulkOutcome, string> = {
  ACCEPTED: '접수됨',
  VALIDATION_FAILED: '검증 실패',
  UNSUPPORTED: '미지원 채널',
  BLOCKED_BY_UNKNOWN: '선행 확인필요 명령으로 차단',
  DUPLICATE_OR_SUPERSEDED: '중복/대체됨',
  FAILED_TO_ENQUEUE: '접수 실패',
}

const COMMAND_STATUS_LABELS: Record<string, string> = {
  PENDING: '접수됨(전송 대기)',
  RUNNING: '전송 중',
  SUCCESS: '성공',
  FAILED: '실패',
  RETRY_WAIT: '재시도 대기',
  UNKNOWN: '확인 필요(운영자 확인 대상)',
  CANCELLED: '취소됨(더 최신 요청으로 대체)',
}

const STATUS_FILTER_OPTIONS = ['ALL', 'PENDING', 'RUNNING', 'SUCCESS', 'FAILED', 'RETRY_WAIT', 'UNKNOWN', 'CANCELLED']

export function ProductBulkPage() {
  const [kind, setKind] = useState<Kind>('PUBLISH')
  const [platformId, setPlatformId] = useState<number | ''>('')
  const [keyword, setKeyword] = useState('')
  const [keywordInput, setKeywordInput] = useState('')
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [confirming, setConfirming] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [submitResult, setSubmitResult] = useState<BulkSubmitResult | null>(null)

  // 항목별 목표값 - target id로 키를 잡아, 선택을 켜고 끄거나 목록을 새로고침해도
  // 다른 항목의 입력값과 섞이지 않는다.
  const [quantityById, setQuantityById] = useState<Record<number, string>>({})
  const [statusById, setStatusById] = useState<Record<number, string>>({})
  const [infoById, setInfoById] = useState<Record<number, { name: string; sale_price: string; description: string }>>(
    {},
  )

  const config = KIND_CONFIG[kind]

  const { data: platforms } = useApiData<Platform[]>(() => api.get('/api/platforms'), [])

  const query = new URLSearchParams()
  if (platformId) query.set('platform_id', String(platformId))
  if (config.usesMapping && keyword) query.set('keyword', keyword)

  const {
    data: listData,
    error: listError,
    isLoading: listLoading,
    reload: reloadList,
  } = useApiData<{ items: BulkTarget[] }>(
    () => api.get(`${config.listPath}?${query.toString()}`),
    [kind, platformId, keyword],
  )
  const rows = useMemo(() => listData?.items ?? [], [listData])

  useEffect(() => {
    // 작업 종류를 바꾸면 이전 종류의 선택/입력값/결과가 새 종류에 섞이지 않게 초기화한다.
    setSelected(new Set())
    setConfirming(false)
    setSubmitResult(null)
    setSubmitError(null)
    setQuantityById({})
    setStatusById({})
    setInfoById({})
  }, [kind])

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

  const selectedRows = rows.filter((r) => selected.has(r.id))
  const channelCounts = useMemo(() => {
    const counts = new Map<string, number>()
    for (const row of selectedRows) {
      counts.set(row.platform_code, (counts.get(row.platform_code) ?? 0) + 1)
    }
    return counts
  }, [selectedRows])

  const buildPayload = (): unknown => {
    const ids = [...selected]
    if (kind === 'PUBLISH') return { draft_ids: ids }
    if (kind === 'OPTION_PUBLISH') return { group_draft_ids: ids }
    if (kind === 'INVENTORY') {
      return {
        items: ids.map((id) => ({
          product_platform_map_id: id,
          target_quantity: Number(quantityById[id] ?? 0),
        })),
      }
    }
    if (kind === 'SALE_STATUS') {
      return {
        items: ids.map((id) => ({
          product_platform_map_id: id,
          target_status: statusById[id] ?? 'ON_SALE',
        })),
      }
    }
    // INFO_UPDATE
    return {
      items: ids.map((id) => {
        const info = infoById[id] ?? { name: '', sale_price: '', description: '' }
        return {
          product_platform_map_id: id,
          name: info.name.trim() ? info.name.trim() : null,
          sale_price: info.sale_price.trim() ? Number(info.sale_price) : null,
          description: info.description.trim() ? info.description.trim() : null,
        }
      }),
    }
  }

  const runSubmit = async () => {
    setSubmitting(true)
    setSubmitError(null)
    try {
      const result = await api.post<BulkSubmitResult>(config.submitPath, buildPayload())
      setSubmitResult(result)
      setConfirming(false)
      setSelected(new Set())
      reloadList()
    } catch (err) {
      setSubmitError(err instanceof ApiError ? err.message : '대량 접수 중 오류가 발생했습니다.')
    } finally {
      setSubmitting(false)
    }
  }

  const acceptedCommandIds = useMemo(
    () => (submitResult?.items ?? []).map((i) => i.command_id).filter((id): id is number => id !== null),
    [submitResult],
  )

  return (
    <div>
      <h2>상품 대량 처리</h2>
      <p className="hint-text">
        여러 상품/옵션을 한 번에 선택해 채널별 등록·재고·판매상태·정보수정을 접수합니다. 실제 채널 전송은 스케줄러가
        비동기로 수행하며, 여기서는 접수와 진행상태만 다룹니다.
      </p>

      <div className="tabs" role="tablist">
        {(Object.keys(KIND_CONFIG) as Kind[]).map((k) => (
          <button
            key={k}
            type="button"
            role="tab"
            aria-selected={kind === k}
            className={kind === k ? 'tab-button active' : 'tab-button'}
            onClick={() => setKind(k)}
          >
            {KIND_CONFIG[k].label}
          </button>
        ))}
      </div>

      <div className="filter-bar">
        <select value={platformId} onChange={(e) => setPlatformId(e.target.value ? Number(e.target.value) : '')}>
          <option value="">전체 채널</option>
          {(platforms ?? []).map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        {config.usesMapping && (
          <form
            onSubmit={(e) => {
              e.preventDefault()
              setKeyword(keywordInput.trim())
            }}
          >
            <input
              value={keywordInput}
              onChange={(e) => setKeywordInput(e.target.value)}
              placeholder="SKU코드/판매자상품코드 검색"
            />
            <button type="submit">검색</button>
          </form>
        )}
      </div>

      {listError && <p className="form-error">{listError}</p>}
      {listLoading ? (
        <p>불러오는 중...</p>
      ) : (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>
                  <input
                    type="checkbox"
                    checked={rows.length > 0 && selected.size === rows.length}
                    onChange={toggleAll}
                  />
                </th>
                <th>ID</th>
                <th>채널</th>
                <th>상품명</th>
                <th>SKU</th>
                {kind === 'INVENTORY' && <th>목표 재고수량</th>}
                {kind === 'SALE_STATUS' && <th>목표 판매상태</th>}
                {kind === 'INFO_UPDATE' && <th>수정할 이름/판매가/설명</th>}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} className={selected.has(row.id) ? 'sel' : ''}>
                  <td>
                    <input type="checkbox" checked={selected.has(row.id)} onChange={() => toggle(row.id)} />
                  </td>
                  <td>{row.id}</td>
                  <td>{row.platform_code}</td>
                  <td>{row.product_name ?? '-'}</td>
                  <td>{row.sku_code ?? '-'}</td>
                  {kind === 'INVENTORY' && (
                    <td>
                      <input
                        type="number"
                        min={0}
                        value={quantityById[row.id] ?? ''}
                        onChange={(e) => setQuantityById((prev) => ({ ...prev, [row.id]: e.target.value }))}
                        style={{ width: 90 }}
                      />
                    </td>
                  )}
                  {kind === 'SALE_STATUS' && (
                    <td>
                      <select
                        value={statusById[row.id] ?? 'ON_SALE'}
                        onChange={(e) => setStatusById((prev) => ({ ...prev, [row.id]: e.target.value }))}
                      >
                        <option value="ON_SALE">판매중</option>
                        <option value="SUSPENDED">판매중지</option>
                      </select>
                    </td>
                  )}
                  {kind === 'INFO_UPDATE' && (
                    <td>
                      <input
                        placeholder="상품명"
                        value={infoById[row.id]?.name ?? ''}
                        onChange={(e) =>
                          setInfoById((prev) => ({
                            ...prev,
                            [row.id]: {
                              name: e.target.value,
                              sale_price: prev[row.id]?.sale_price ?? '',
                              description: prev[row.id]?.description ?? '',
                            },
                          }))
                        }
                        style={{ width: 120, marginRight: 6 }}
                      />
                      <input
                        placeholder="판매가"
                        type="number"
                        value={infoById[row.id]?.sale_price ?? ''}
                        onChange={(e) =>
                          setInfoById((prev) => ({
                            ...prev,
                            [row.id]: {
                              name: prev[row.id]?.name ?? '',
                              sale_price: e.target.value,
                              description: prev[row.id]?.description ?? '',
                            },
                          }))
                        }
                        style={{ width: 90, marginRight: 6 }}
                      />
                      <input
                        placeholder="상세설명"
                        value={infoById[row.id]?.description ?? ''}
                        onChange={(e) =>
                          setInfoById((prev) => ({
                            ...prev,
                            [row.id]: {
                              name: prev[row.id]?.name ?? '',
                              sale_price: prev[row.id]?.sale_price ?? '',
                              description: e.target.value,
                            },
                          }))
                        }
                        style={{ width: 140 }}
                      />
                    </td>
                  )}
                </tr>
              ))}
              {rows.length === 0 && (
                <tr>
                  <td colSpan={6}>대상이 없습니다.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {selected.size > 0 && !confirming && (
        <div className="bulk-bar">
          <span className="count">{selected.size}건 선택됨 - {config.label}</span>
          <button type="button" onClick={() => setConfirming(true)}>
            실행
          </button>
          <button type="button" onClick={() => setSelected(new Set())}>
            선택 해제
          </button>
        </div>
      )}

      {confirming && (
        <div className="bulk-bar confirm-panel">
          <div>
            <strong>
              {config.label} - {selected.size}건 접수 확인
            </strong>
            <ul>
              {[...channelCounts.entries()].map(([code, count]) => (
                <li key={code}>
                  {code}: {count}건
                </li>
              ))}
            </ul>
            <p className="hint-text">실제 채널 전송은 하지 않고 접수(outbox 명령 생성)만 합니다. 계속할까요?</p>
          </div>
          <button type="button" disabled={submitting} onClick={runSubmit}>
            {submitting ? '접수 중...' : '접수 실행'}
          </button>
          <button type="button" onClick={() => setConfirming(false)}>
            취소
          </button>
        </div>
      )}

      {submitError && <p className="form-error">{submitError}</p>}

      {submitResult && (
        <div className="table-scroll">
          <h3>접수 결과 {submitResult.aborted && <span className="status-badge">중단됨(DB 오류)</span>}</h3>
          <table className="data-table">
            <thead>
              <tr>
                <th>대상 ID</th>
                <th>결과</th>
                <th>명령 ID</th>
                <th>사유</th>
              </tr>
            </thead>
            <tbody>
              {submitResult.items.map((item, idx) => (
                <BulkItemRow key={idx} item={item} />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {acceptedCommandIds.length > 0 && <ProgressPanel initialCommandIds={acceptedCommandIds} />}
    </div>
  )
}

function BulkItemRow({ item }: { item: BulkItemResult }) {
  return (
    <tr>
      <td>{item.target_id}</td>
      <td>
        <span className="status-badge">{OUTCOME_LABELS[item.outcome]}</span>
      </td>
      <td>{item.command_id ?? '-'}</td>
      <td>{item.error_code ?? '-'}</td>
    </tr>
  )
}

function ProgressPanel({ initialCommandIds }: { initialCommandIds: number[] }) {
  const [commandIds, setCommandIds] = useState<number[]>(initialCommandIds)
  const [statuses, setStatuses] = useState<BulkCommandStatus[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [statusFilter, setStatusFilter] = useState('ALL')
  const [retrySelected, setRetrySelected] = useState<Set<number>>(new Set())
  const [retryResult, setRetryResult] = useState<BulkRetryResult | null>(null)
  const [retrying, setRetrying] = useState(false)

  useEffect(() => {
    setCommandIds((prev) => Array.from(new Set([...prev, ...initialCommandIds])))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialCommandIds])

  const refresh = async () => {
    if (commandIds.length === 0) return
    setLoading(true)
    setError(null)
    try {
      const result = await api.post<{ items: BulkCommandStatus[] }>('/api/products/bulk/commands/status', {
        command_ids: commandIds,
      })
      setStatuses(result.items)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '진행상태 조회 중 오류가 발생했습니다.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [commandIds])

  const filtered = statusFilter === 'ALL' ? statuses : statuses.filter((s) => s.status === statusFilter)

  const toggleRetry = (id: number) => {
    setRetrySelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const runRetry = async () => {
    setRetrying(true)
    try {
      const result = await api.post<BulkRetryResult>('/api/products/bulk/commands/retry', {
        command_ids: [...retrySelected],
      })
      setRetryResult(result)
      setRetrySelected(new Set())
      refresh()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '재처리 중 오류가 발생했습니다.')
    } finally {
      setRetrying(false)
    }
  }

  return (
    <div className="table-scroll">
      <h3>대량 작업 진행상태</h3>
      <div className="filter-bar">
        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          {STATUS_FILTER_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {s === 'ALL' ? '전체' : COMMAND_STATUS_LABELS[s] ?? s}
            </option>
          ))}
        </select>
        <button type="button" onClick={refresh} disabled={loading}>
          새로고침
        </button>
        {retrySelected.size > 0 && (
          <button type="button" onClick={runRetry} disabled={retrying}>
            {retrying ? '재처리 중...' : `선택한 ${retrySelected.size}건 재처리`}
          </button>
        )}
      </div>
      {error && <p className="form-error">{error}</p>}
      <table className="data-table">
        <thead>
          <tr>
            <th>재처리 선택</th>
            <th>명령 ID</th>
            <th>종류</th>
            <th>상태</th>
            <th>시도횟수</th>
            <th>사유</th>
          </tr>
        </thead>
        <tbody>
          {filtered.map((s) => (
            <tr key={s.id}>
              <td>
                {s.status === 'FAILED' ? (
                  <input type="checkbox" checked={retrySelected.has(s.id)} onChange={() => toggleRetry(s.id)} />
                ) : s.status === 'UNKNOWN' ? (
                  <span className="hint-text">운영자 확인 필요(상품상세 화면에서 해소)</span>
                ) : (
                  '-'
                )}
              </td>
              <td>{s.id}</td>
              <td>{s.command_type}</td>
              <td>
                <span className="status-badge">{COMMAND_STATUS_LABELS[s.status] ?? s.status}</span>
              </td>
              <td>{s.attempt_count}</td>
              <td>{s.error_code ?? '-'}</td>
            </tr>
          ))}
          {filtered.length === 0 && (
            <tr>
              <td colSpan={6}>표시할 항목이 없습니다.</td>
            </tr>
          )}
        </tbody>
      </table>
      {retryResult && (
        <p className="hint-text">
          재처리 결과:{' '}
          {retryResult.items.map((r) => `#${r.command_id}=${r.outcome}`).join(', ')}
          {retryResult.aborted && ' (DB 오류로 중단됨)'}
        </p>
      )}
    </div>
  )
}
