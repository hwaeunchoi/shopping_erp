import { useEffect, useState, type FormEvent } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { api, ApiError } from '../api/client'
import { downloadBlob } from '../api/download'
import { useApiData } from '../api/useApiData'
import type {
  BulkResult,
  OrderAlerts,
  OrderDetailBundle,
  OrderListResponse,
  OrderRow,
  Platform,
  Supplier,
} from '../api/types'

const CS_FILTER: [string, string][] = [
  ['', 'CS 전체'],
  ['NONE', '정상'],
  ['EXCHANGE', '교환'],
  ['RETURN', '반품'],
  ['CANCEL', '취소'],
]

const ORDER_STATUS = ['', 'NEW', 'PREPARING', 'SHIPPING', 'DELIVERED', 'CANCELED', 'EXCHANGED', 'RETURNED', 'REFUNDED']
const SHIPPING_STATUS: [string, string][] = [
  ['', '배송 전체'],
  ['UNSHIPPED', '미발송'],
  ['SHIPPING', '배송중'],
  ['DELIVERED', '배송완료'],
]

const PAY_LABEL: Record<string, [string, string]> = {
  PAID: ['결제완료', 'spill-ok'],
  UNPAID: ['미결제', 'spill-warn'],
}
const SHIP_LABEL: Record<string, [string, string]> = {
  UNSHIPPED: ['미발송', 'spill-warn'],
  SHIPPING: ['배송중', 'spill-info'],
  DELIVERED: ['배송완료', 'spill-muted'],
}
const CS_LABEL: Record<string, [string, string]> = {
  NONE: ['', ''],
  EXCHANGE: ['교환', 'spill-info'],
  RETURN: ['반품', 'spill-danger'],
  CANCEL: ['취소', 'spill-danger'],
}

function todayIso(offset = 0): string {
  const d = new Date()
  d.setDate(d.getDate() + offset)
  return d.toISOString().slice(0, 10)
}

function Spill({ text, cls }: { text: string; cls: string }) {
  if (!text) return null
  return <span className={`spill ${cls}`}>{text}</span>
}

function DetailPanel({ orderId, onClose, onChanged }: { orderId: number; onClose: () => void; onChanged: () => void }) {
  const { data, error, reload } = useApiData<OrderDetailBundle>(
    () => api.get(`/api/orders/${orderId}/detail`),
    [orderId],
  )
  const [tab, setTab] = useState('info')
  const [memo, setMemo] = useState('')
  const [carrier, setCarrier] = useState('')
  const [tracking, setTracking] = useState('')

  useEffect(() => setTab('info'), [orderId])

  if (error) return <div className="order-panel"><div className="order-tab-body form-error">{error}</div></div>
  if (!data) return <div className="order-panel"><div className="order-tab-body">불러오는 중...</div></div>

  const addMemo = async () => {
    if (!memo.trim()) return
    await api.post(`/api/orders/${orderId}/memos`, { content: memo })
    setMemo('')
    reload()
  }
  const registerShipment = async () => {
    await api.post('/api/orders/bulk/ship', {
      items: [{ order_id: orderId, carrier: carrier || null, tracking_no: tracking || null }],
    })
    setCarrier('')
    setTracking('')
    reload()
    onChanged()
  }
  const createCs = async (csType: string) => {
    const reason = window.prompt(`${csType} 사유`) ?? ''
    await api.post(`/api/orders/${orderId}/cs`, { type: csType, reason })
    reload()
    onChanged()
  }

  return (
    <div className="order-panel">
      <div className="order-panel-head">
        <div>
          <h3 className="mono">{data.platform_order_no}</h3>
          <div className="sub">{data.platform_name} · {new Date(data.order_date).toLocaleString()}</div>
        </div>
        <button className="order-panel-close" onClick={onClose} aria-label="닫기">✕</button>
      </div>
      <div className="order-tabs">
        {[
          ['info', '주문정보'], ['items', '상품'], ['stock', '재고'], ['po', '발주'], ['ship', '배송'],
          ['pnl', '정산·손익'], ['cs', `CS${data.cs_history.length ? ' ' + data.cs_history.length : ''}`],
          ['cust', '고객'], ['memo', `메모 ${data.memos.length}`], ['log', '로그'],
        ].map(([k, label]) => (
          <div key={k} className={`order-tab${tab === k ? ' active' : ''}`} onClick={() => setTab(k)}>{label}</div>
        ))}
      </div>
      <div className="order-tab-body">
        {tab === 'info' && (
          <>
            <div className="sec-title">상태</div>
            <div>
              <Spill text={`주문 · ${data.order_status}`} cls="spill-muted" />
              <Spill text={PAY_LABEL[data.payment_status]?.[0] ?? data.payment_status} cls={PAY_LABEL[data.payment_status]?.[1] ?? 'spill-muted'} />
              <Spill text={SHIP_LABEL[data.shipping_status]?.[0] ?? data.shipping_status} cls={SHIP_LABEL[data.shipping_status]?.[1] ?? 'spill-muted'} />
              {data.cs_status !== 'NONE' && <Spill text={`CS · ${CS_LABEL[data.cs_status]?.[0]}`} cls={CS_LABEL[data.cs_status]?.[1] ?? 'spill-muted'} />}
            </div>
            <div className="sec-title">주문 / 금액</div>
            <dl className="kv">
              <dt>쇼핑몰</dt><dd>{data.platform_name}</dd>
              <dt>주문일</dt><dd>{new Date(data.order_date).toLocaleString()}</dd>
              <dt>결제일</dt><dd>{data.payment_date ? new Date(data.payment_date).toLocaleString() : '-'}</dd>
              <dt>배송완료</dt><dd>{data.delivery_completed_date ? new Date(data.delivery_completed_date).toLocaleString() : '-'}</dd>
              <dt>판매금액</dt><dd>{data.total_amount.toLocaleString()}원</dd>
              <dt>할인</dt><dd>{data.discount_amount.toLocaleString()}원</dd>
            </dl>
          </>
        )}
        {tab === 'items' && (
          <>
            <div className="sec-title">주문 품목 {data.items.length}건</div>
            {data.items.map((it) => (
              <div className="panel-item" key={it.id}>
                <div className="order-cell-stack">
                  <span>{it.product_name ?? '-'}{it.option_name ? ` (${it.option_name})` : ''}</span>
                  <span className="sub">SKU {it.sku_code ?? '-'} · 채널상품 {it.channel_product_no ?? '-'} · 채널옵션 {it.channel_option_no ?? '-'}</span>
                </div>
                <div className="order-cell-stack" style={{ textAlign: 'right' }}>
                  <span>{it.quantity}개</span>
                  <span className="sub">{it.unit_price.toLocaleString()} / 개</span>
                </div>
              </div>
            ))}
          </>
        )}
        {tab === 'stock' && (
          <>
            <div className="sec-title">SKU 재고 현황</div>
            {data.inventory.length === 0 && <p className="sub">연결된 재고 레코드가 없습니다.</p>}
            {data.inventory.map((inv, i) => (
              <dl className="kv" key={i} style={{ marginBottom: 12 }}>
                <dt>SKU</dt><dd className="mono">{inv.sku_code}</dd>
                <dt>현재/예약</dt><dd>{inv.sellable_stock} / {inv.reserved_stock}</dd>
                <dt>가용재고</dt><dd>{inv.available_stock}</dd>
                <dt>안전재고</dt><dd>{inv.safety_stock}</dd>
              </dl>
            ))}
          </>
        )}
        {tab === 'po' && (
          <>
            <div className="sec-title">연결된 발주 (후보)</div>
            {data.purchase_links.length === 0 && <p className="sub">이 주문 품목을 포함하는 발주가 없습니다.</p>}
            {data.purchase_links.map((po) => (
              <div className="panel-item" key={po.purchase_order_id}>
                <div className="order-cell-stack">
                  <span>발주 #{po.purchase_order_id} · {po.supplier_name ?? '-'}</span>
                  <span className="sub">{po.order_date ? new Date(po.order_date).toLocaleDateString() : '작성중'}</span>
                </div>
                <Spill text={po.status} cls={po.status === 'RECEIVED' ? 'spill-ok' : 'spill-info'} />
              </div>
            ))}
            {data.purchase_links.length > 0 && (
              <p className="sub" style={{ marginTop: 8, color: 'var(--text-muted)' }}>발주 1건이 여러 주문 재고를 함께 채우므로 정확한 귀속이 아닌 연결 후보입니다.</p>
            )}
          </>
        )}
        {tab === 'ship' && (
          <>
            <div className="sec-title">배송</div>
            <dl className="kv">
              <dt>택배사</dt><dd>{data.shipment?.carrier ?? '미등록'}</dd>
              <dt>송장번호</dt><dd className="mono">{data.shipment?.tracking_no ?? '미등록'}</dd>
              <dt>배송상태</dt><dd>{data.shipment?.status ?? '미발송'}</dd>
            </dl>
            <div className="sec-title">송장 등록 / 발송처리</div>
            <div className="inline-form">
              <input placeholder="택배사" value={carrier} onChange={(e) => setCarrier(e.target.value)} />
              <input placeholder="송장번호" value={tracking} onChange={(e) => setTracking(e.target.value)} />
              <button type="button" onClick={registerShipment}>등록·발송</button>
            </div>
          </>
        )}
        {tab === 'pnl' && (
          <>
            <div className="sec-title">주문 단위 공헌이익 (광고 전)</div>
            <table className="money">
              <tbody>
                <tr><td>판매가</td><td>{data.contribution.sale_amount.toLocaleString()}</td></tr>
                <tr><td>원가(스냅샷)</td><td>-{data.contribution.cost_of_goods.toLocaleString()}</td></tr>
                <tr><td>쇼핑몰수수료</td><td>-{data.contribution.platform_fee.toLocaleString()}</td></tr>
                <tr className="tot"><td>공헌이익 ({data.contribution.contribution_rate}%)</td><td style={{ color: 'var(--done)' }}>{data.contribution.contribution_margin.toLocaleString()}</td></tr>
              </tbody>
            </table>
            <p className="sub" style={{ marginTop: 8, color: 'var(--text-muted)' }}>{data.contribution.ad_cost_note}</p>
            <div className="sec-title">정산 대사</div>
            {data.settlement ? (
              <dl className="kv">
                <dt>정산회차</dt><dd>{data.settlement.settlement_cycle ?? '-'}</dd>
                <dt>정산상태</dt><dd>{data.settlement.status ?? '-'}</dd>
                <dt>정산액(순)</dt><dd>{data.settlement.net_amount.toLocaleString()}</dd>
              </dl>
            ) : <p className="sub">연결된 정산 회차가 없습니다(미도래).</p>}
          </>
        )}
        {tab === 'cs' && (
          <>
            <div className="sec-title">CS 이력</div>
            {data.cs_history.length === 0 && <p className="sub">접수된 교환·반품·취소가 없습니다.</p>}
            {data.cs_history.map((cs) => (
              <div className="panel-item" key={`${cs.type}-${cs.id}`}>
                <div className="order-cell-stack">
                  <span>{cs.type === 'EXCHANGE' ? '교환' : cs.type === 'RETURN' ? '반품' : '취소'} · {cs.reason ?? '-'}</span>
                  <span className="sub">{new Date(cs.requested_at).toLocaleString()}</span>
                </div>
                <Spill text={cs.status} cls="spill-muted" />
              </div>
            ))}
            <div className="sec-title">CS 접수</div>
            <div className="inline-form">
              <button type="button" onClick={() => createCs('EXCHANGE')}>교환</button>
              <button type="button" onClick={() => createCs('RETURN')}>반품</button>
              <button type="button" onClick={() => createCs('CANCEL')}>취소</button>
            </div>
          </>
        )}
        {tab === 'cust' && (
          <>
            <div className="sec-title">고객</div>
            {data.customer ? (
              <dl className="kv">
                <dt>이름</dt><dd>{data.customer.name ?? '-'}</dd>
                <dt>연락처</dt><dd>{data.customer.phone ?? '-'}</dd>
                <dt>주소</dt><dd>{data.customer.address ?? '-'}</dd>
                <dt>등급</dt><dd>{data.customer.is_vip ? 'VIP' : (data.customer.grade ?? '일반')} · 누적 {data.customer.order_count}건</dd>
              </dl>
            ) : <p className="sub">연결된 고객 정보가 없습니다.</p>}
          </>
        )}
        {tab === 'memo' && (
          <>
            {data.memos.map((m) => (
              <div className="memo-item" key={m.id}>{m.content}<div className="meta">{new Date(m.created_at).toLocaleString()}</div></div>
            ))}
            <textarea placeholder="메모 추가" value={memo} onChange={(e) => setMemo(e.target.value)} rows={2}
              style={{ width: '100%', border: '1px solid var(--border)', borderRadius: 7, padding: 8, marginTop: 8, background: 'var(--bg)', color: 'var(--text)' }} />
            <button type="button" onClick={addMemo} style={{ marginTop: 6 }}>메모 등록</button>
          </>
        )}
        {tab === 'log' && (
          <>
            <div className="sec-title">상태 변경 이력</div>
            {data.status_history.length === 0 && <p className="sub">이력이 없습니다.</p>}
            {data.status_history.map((h, i) => (
              <div className="tl-item" key={i}>
                <div className="tl-dot" />
                <div><div>{h.from_status ?? '(신규)'} → {h.to_status}</div><div className="t">{new Date(h.changed_at).toLocaleString()}</div></div>
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  )
}

export function OrdersPage() {
  const { user } = useAuth()
  const canEdit = user?.permissions.includes('ORDER_EDIT') ?? false
  const [params] = useSearchParams()

  const [status, setStatus] = useState(params.get('status_filter') ?? '')
  const [platformId, setPlatformId] = useState(params.get('platform_id') ?? '')
  const [shippingStatus, setShippingStatus] = useState('')
  const [supplierId, setSupplierId] = useState('')
  const [csStatus, setCsStatus] = useState('')
  const [mineOnly, setMineOnly] = useState(false)
  const [sku, setSku] = useState('')
  const [startDate, setStartDate] = useState(params.get('start_date') ?? '')
  const [endDate, setEndDate] = useState(params.get('end_date') ?? '')
  const [keyword, setKeyword] = useState(params.get('keyword') ?? '')
  const [keywordInput, setKeywordInput] = useState(keyword)
  const [page, setPage] = useState(1)

  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [openId, setOpenId] = useState<number | null>(null)
  const [actionMsg, setActionMsg] = useState<string | null>(null)

  const q = new URLSearchParams()
  if (status) q.set('status_filter', status)
  if (platformId) q.set('platform_id', platformId)
  if (shippingStatus) q.set('shipping_status', shippingStatus)
  if (supplierId) q.set('supplier_id', supplierId)
  if (csStatus) q.set('cs_status', csStatus)
  if (mineOnly && user?.id) q.set('assignee_id', String(user.id))
  if (sku) q.set('sku', sku)
  if (startDate) q.set('start_date', startDate)
  if (endDate) q.set('end_date', endDate)
  if (keyword) q.set('keyword', keyword)
  q.set('page', String(page))
  q.set('page_size', '50')

  const { data, error, isLoading, reload } = useApiData<OrderListResponse>(
    () => api.get(`/api/orders?${q.toString()}`),
    [status, platformId, shippingStatus, supplierId, csStatus, mineOnly, sku, startDate, endDate, keyword, page],
  )
  const { data: alerts } = useApiData<OrderAlerts>(() => api.get('/api/orders/alerts'), [])
  const { data: platforms } = useApiData<Platform[]>(() => api.get('/api/platforms'), [])
  const { data: suppliers } = useApiData<Supplier[]>(() => api.get('/api/suppliers'), [])

  const rows = data?.rows ?? []
  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1

  const submitSearch = (e: FormEvent) => {
    e.preventDefault()
    setPage(1)
    setKeyword(keywordInput.trim())
  }
  const toggle = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }
  const toggleAll = () => {
    setSelected((prev) => (prev.size === rows.length ? new Set() : new Set(rows.map((r) => r.id))))
  }

  const runBulk = async (label: string, fn: () => Promise<BulkResult>) => {
    setActionMsg(null)
    try {
      const res = await fn()
      setActionMsg(`${label} 완료 — 성공 ${res.succeeded}건, 실패 ${res.failed}건`)
      setSelected(new Set())
      reload()
      if (openId) reload()
    } catch (err) {
      setActionMsg(err instanceof ApiError ? err.message : `${label} 중 오류가 발생했습니다.`)
    }
  }
  const bulkStatus = () => {
    const to = window.prompt('변경할 주문상태 (NEW/PREPARING/SHIPPING/DELIVERED/CANCELED)')
    if (!to) return
    runBulk('상태변경', () => api.post('/api/orders/bulk/status', { order_ids: [...selected], status: to }))
  }
  const bulkMemo = () => {
    const content = window.prompt('추가할 메모 내용')
    if (!content) return
    runBulk('메모추가', () => api.post('/api/orders/bulk/memo', { order_ids: [...selected], content }))
  }
  const bulkShip = () => {
    const carrier = window.prompt('택배사') ?? ''
    const items = [...selected].map((id) => ({ order_id: id, carrier, tracking_no: null }))
    runBulk('송장등록', () => api.post('/api/orders/bulk/ship', { items }))
  }
  const bulkAssign = () => {
    if (!user?.id) return
    runBulk('담당자 지정', () =>
      api.patch('/api/orders/bulk/assign', { order_ids: [...selected], assignee_id: user.id }),
    )
  }
  const exportExcel = () => downloadBlob(`/api/orders/export?${q.toString()}`, `orders_${todayIso()}.xlsx`)

  return (
    <div>
      <h2>주문 워크벤치</h2>

      {alerts && (
        <div className="kpi-strip-o">
          <div className={`kpi-o${alerts.delayed_unshipped_count > 0 ? ' alert' : ''}`}
            onClick={() => { setStatus(''); setShippingStatus('UNSHIPPED'); setPage(1) }}>
            <div className="klab">미발송</div>
            <div className="kval">{alerts.unshipped_count}</div>
            <div className="ksub">2일↑ 지연 {alerts.delayed_unshipped_count}건</div>
          </div>
          <div className={`kpi-o${alerts.exchange_pending_count > 0 ? ' alert' : ''}`}
            onClick={() => { setCsStatus('EXCHANGE'); setPage(1) }}>
            <div className="klab">교환 대기</div><div className="kval">{alerts.exchange_pending_count}</div>
          </div>
          <div className={`kpi-o${alerts.return_pending_count > 0 ? ' alert' : ''}`}
            onClick={() => { setCsStatus('RETURN'); setPage(1) }}>
            <div className="klab">반품 대기</div><div className="kval">{alerts.return_pending_count}</div>
          </div>
          <div className={`kpi-o${alerts.cancellation_pending_count > 0 ? ' alert' : ''}`}
            onClick={() => { setCsStatus('CANCEL'); setPage(1) }}>
            <div className="klab">취소 대기</div><div className="kval">{alerts.cancellation_pending_count}</div>
          </div>
          <div className="kpi-o" onClick={() => { setMineOnly(true); setPage(1) }}>
            <div className="klab">내 담당</div><div className="kval">{mineOnly ? '필터중' : '보기'}</div>
          </div>
        </div>
      )}

      <div className="order-toolbar">
        <form className="order-search-row" onSubmit={submitSearch}>
          <input className="search" value={keywordInput} onChange={(e) => setKeywordInput(e.target.value)}
            placeholder="통합검색: 주문번호·송장번호·고객명·연락처·주소·상품명·옵션·SKU·공급처·메모" />
          <button type="submit">검색</button>
          {keyword && <button type="button" onClick={() => { setKeyword(''); setKeywordInput('') }}>초기화</button>}
          <button type="button" onClick={exportExcel}>엑셀</button>
        </form>
        <div className="order-search-hint">통합검색 1개 입력창으로 9개 항목을 동시에 조회합니다.</div>
        <div className="order-filters">
          <span className="flabel">기간</span>
          <input type="date" value={startDate} onChange={(e) => { setPage(1); setStartDate(e.target.value) }} />
          <span>~</span>
          <input type="date" value={endDate} onChange={(e) => { setPage(1); setEndDate(e.target.value) }} />
          <span className="flabel">쇼핑몰</span>
          <select value={platformId} onChange={(e) => { setPage(1); setPlatformId(e.target.value) }}>
            <option value="">전체</option>
            {platforms?.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
          <span className="flabel">공급처</span>
          <select value={supplierId} onChange={(e) => { setPage(1); setSupplierId(e.target.value) }}>
            <option value="">전체</option>
            {suppliers?.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
          <span className="flabel">주문상태</span>
          <select value={status} onChange={(e) => { setPage(1); setStatus(e.target.value) }}>
            {ORDER_STATUS.map((s) => <option key={s || 'all'} value={s}>{s || '전체'}</option>)}
          </select>
          <span className="flabel">배송상태</span>
          <select value={shippingStatus} onChange={(e) => { setPage(1); setShippingStatus(e.target.value) }}>
            {SHIPPING_STATUS.map(([v, l]) => <option key={v || 'all'} value={v}>{l}</option>)}
          </select>
          <span className="flabel">CS상태</span>
          <select value={csStatus} onChange={(e) => { setPage(1); setCsStatus(e.target.value) }}>
            {CS_FILTER.map(([v, l]) => <option key={v || 'all'} value={v}>{l}</option>)}
          </select>
          <span className="flabel">SKU</span>
          <input value={sku} onChange={(e) => { setPage(1); setSku(e.target.value) }} placeholder="SKU" style={{ width: 110 }} />
          <label style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 4 }}>
            <input type="checkbox" checked={mineOnly} onChange={(e) => { setPage(1); setMineOnly(e.target.checked) }} /> 내 담당만
          </label>
        </div>
      </div>

      {canEdit && selected.size > 0 && (
        <div className="bulk-bar">
          <span className="count">{selected.size}건 선택됨</span>
          <button type="button" onClick={bulkShip}>송장등록</button>
          <button type="button" onClick={bulkStatus}>상태변경</button>
          <button type="button" onClick={bulkAssign}>담당자 지정</button>
          <button type="button" onClick={bulkMemo}>메모추가</button>
          <button type="button" onClick={exportExcel}>엑셀다운로드</button>
        </div>
      )}
      {actionMsg && <p className="form-info">{actionMsg}</p>}
      {error && <p className="form-error">{error}</p>}

      <div className={`order-workarea${openId ? ' with-panel' : ''}`}>
        <div className="order-table-card">
          <div className="order-table-scroll">
            <table className="order-table">
              <thead>
                <tr className="grp">
                  <th></th>
                  <th colSpan={2}>주문정보</th>
                  <th colSpan={2}>고객정보</th>
                  <th colSpan={3}>상품정보</th>
                  <th>공급</th>
                  <th>금액</th>
                  <th colSpan={2}>배송</th>
                  <th>기타</th>
                </tr>
                <tr className="col">
                  <th><input type="checkbox" checked={rows.length > 0 && selected.size === rows.length} onChange={toggleAll} /></th>
                  <th>주문번호 / 쇼핑몰</th>
                  <th>상태</th>
                  <th>고객명</th>
                  <th>연락처</th>
                  <th>상품 / 옵션</th>
                  <th>SKU / 채널</th>
                  <th>수량</th>
                  <th>공급처</th>
                  <th>금액</th>
                  <th>택배사</th>
                  <th>송장번호</th>
                  <th>담당자 / 태그</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r: OrderRow) => (
                  <tr key={r.id} className={`${selected.has(r.id) || openId === r.id ? 'sel' : ''}${r.is_delayed ? ' delayed' : ''}`}
                    onClick={() => setOpenId(r.id)}>
                    <td onClick={(e) => e.stopPropagation()}>
                      <input type="checkbox" checked={selected.has(r.id)} onChange={() => toggle(r.id)} />
                    </td>
                    <td className="order-cell-stack">
                      <span className="mono">{r.platform_order_no}</span>
                      <span className="sub">{r.platform_name}</span>
                    </td>
                    <td>
                      <Spill text={r.order_status} cls="spill-muted" />
                      <Spill text={PAY_LABEL[r.payment_status]?.[0] ?? ''} cls={PAY_LABEL[r.payment_status]?.[1] ?? 'spill-muted'} />
                      <Spill text={SHIP_LABEL[r.shipping_status]?.[0] ?? ''} cls={SHIP_LABEL[r.shipping_status]?.[1] ?? 'spill-muted'} />
                      {r.cs_status !== 'NONE' && <Spill text={CS_LABEL[r.cs_status]?.[0] ?? ''} cls={CS_LABEL[r.cs_status]?.[1] ?? 'spill-muted'} />}
                      {r.is_delayed && <Spill text="지연" cls="spill-danger" />}
                    </td>
                    <td>{r.customer_name ?? '-'}</td>
                    <td>{r.customer_phone ?? '-'}</td>
                    <td className="order-cell-stack">
                      <span>{r.product_name ?? '-'}{r.item_count > 1 ? ` 외 ${r.item_count - 1}건` : ''}</span>
                      <span className="sub">{r.option_name ?? ''}</span>
                    </td>
                    <td className="order-cell-stack">
                      <span className="mono">{r.sku_code ?? '-'}</span>
                      <span className="sub mono">{r.channel_option_no ?? ''}</span>
                    </td>
                    <td className="num">{r.total_quantity}</td>
                    <td>{r.supplier_name ?? '-'}</td>
                    <td className="num">{r.total_amount.toLocaleString()}</td>
                    <td>{r.carrier ?? '-'}</td>
                    <td className="mono">{r.tracking_no ?? '-'}</td>
                    <td className="order-cell-stack">
                      <span>{r.assignee_name ?? '-'}</span>
                      {r.tags && <span className="sub">{r.tags}</span>}
                    </td>
                  </tr>
                ))}
                {!isLoading && rows.length === 0 && (
                  <tr><td colSpan={13} style={{ textAlign: 'center', padding: 24, color: 'var(--text-muted)' }}>조건에 맞는 주문이 없습니다.</td></tr>
                )}
              </tbody>
            </table>
          </div>
          <div className="order-foot">
            <span>전체 {data?.total ?? 0}건 · {page} / {totalPages} 페이지</span>
            <div className="pager">
              <button disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>‹ 이전</button>
              <button disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>다음 ›</button>
            </div>
          </div>
        </div>

        {openId && <DetailPanel orderId={openId} onClose={() => setOpenId(null)} onChanged={reload} />}
      </div>
    </div>
  )
}
