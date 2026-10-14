// 화면 검수 전용 모의 API 서버(테스트 전용 경로, production 번들에 포함되지 않는다).
//
// - 실제 백엔드·운영 DB·외부 API 에 연결하지 않는다. 응답은 백엔드 OpenAPI(응답 모델)에서 만든 합성 데이터다.
// - OpenAPI 는 `venv\Scripts\python tests\visual\gen_openapi.py <출력파일>` 로 만든다(앱을 import 만 하며 DB 를 읽지 않는다).
// - 실행: OPENAPI=<파일> DIST=<frontend/dist> PORT=5199 node tests/visual/mock-api.mjs
// - 상태 전환: /__mock/state?mode=normal|empty|error|slow , /__mock/perm?set=all|limited
// - 개인정보로 오인될 값(이메일·전화번호·주소·실제 주문번호)을 만들지 않는다.
import { createServer } from 'node:http'
import { appendFileSync, createReadStream, existsSync, readFileSync, statSync } from 'node:fs'
import { extname, join, resolve } from 'node:path'

const PORT = Number(process.env.PORT || 5199)
const DIST = resolve(process.env.DIST || 'dist')
const spec = JSON.parse(readFileSync(process.env.OPENAPI, 'utf8'))
const PERMS = ['DASHBOARD_VIEW', 'ORDER_VIEW', 'ORDER_EDIT', 'SHIPMENT_VIEW', 'CS_VIEW', 'EXCHANGE_RETURN_MANAGE', 'PRODUCT_MANAGE', 'INVENTORY_VIEW', 'SUPPLIER_MANAGE', 'SETTLEMENT_VIEW', 'COST_MANAGE', 'AD_MANAGE', 'ANALYTICS_VIEW', 'REPORT_VIEW', 'SYSTEM_MONITOR_VIEW', 'NOTIFICATION_VIEW', 'SETTINGS_MANAGE']
const state = { mode: 'normal', perm: 'all', theme: 'LIGHT' }

const deref = (s) => (s && s.$ref ? deref(s.$ref.split('/').slice(1).reduce((o, k) => o[k], spec)) : s)
let curPath = ''
const STATUS_BY_PATH = [
  [new RegExp("^/api/products"), ['ACTIVE', 'DISCONTINUED']],
  [new RegExp("^/api/orders"), ['NEW', 'SHIPPING', 'DELIVERED']],
  [new RegExp("^/api/settlements"), ['SCHEDULED', 'PARTIAL', 'COMPLETED']],
  [new RegExp("^/api/(exchanges|returns)"), ['REQUESTED', 'APPROVED', 'COMPLETED']],
  [new RegExp("^/api/cancellations"), ['REQUESTED', 'COMPLETED']],
  [new RegExp("^/api/shipments"), ['READY', 'SHIPPING', 'DELIVERED']],
  [new RegExp("^/api/purchase-orders"), ['DRAFT', 'ORDERED', 'RECEIVED']],
  [new RegExp("^/api/operations/failures"), ['FAILED', 'UNKNOWN', 'RETRY_WAIT']],
  [new RegExp("^/api/(system-monitor|tasks)"), ['SUCCESS', 'FAILED', 'RUNNING']],
  [new RegExp("^/api/fulfillment"), ['READY', 'PICKING', 'PACKED']],
]
const FIELD_VALUES = {
  status: ['OPEN', 'IN_PROGRESS', 'RESOLVED'],
  order_status: ['NEW', 'SHIPPING', 'DELIVERED'],
  payment_status: ['PAID', 'UNPAID'],
  shipping_status: ['UNSHIPPED', 'SHIPPING', 'DELIVERED'],
  cs_status: ['NONE', 'EXCHANGE', 'RETURN'],
  priority: ['NORMAL', 'HIGH', 'URGENT'],
  inquiry_type: ['DELIVERY', 'PRODUCT', 'PAYMENT'],
  severity: ['INFO', 'WARNING', 'ERROR'],
  frequency: ['MONTHLY', 'WEEKLY', 'DAILY'],
  output_format: ['XLSX', 'PDF'],
  report_type: ['PROFIT_REPORT', 'SALES_REPORT'],
  command_type: ['SHIPMENT_SUBMIT', 'PRODUCT_CREATE', 'INVENTORY_UPDATE'],
  target_type: ['ORDER', 'PRODUCT'],
  integration_type: ['MALL', 'AD'],
  trigger_type: ['SCHEDULE', 'MANUAL'],
  task_type: ['ORDER_COLLECT', 'BACKUP', 'REPORT_GENERATE'],
  metric: ['REVENUE', 'NET_PROFIT', 'ORDER_COUNT'],
  type: ['IN', 'OUT', 'ADJUST'],
  category: ['SHIPPING', 'PACKAGING', 'ETC'],
  cost_type: ['FIXED', 'VARIABLE'],
}
const NAMES = {
  name: '합성 항목', product_name: '합성 상품', customer_name: '합성 고객', platform_name: '합성 쇼핑몰', supplier_name: '합성 공급처',
  option_name: '기본 옵션', brand: '합성 브랜드', manufacturer: '합성 제조사', title: '합성 문의 제목', message: '합성 안내 메시지(검수용)',
  reason: '합성 사유', memo: '합성 메모', description: '합성 설명', username: 'demo', sku_code: 'SKU-DEMO',
}

function gen(schemaIn, key = '', depth = 0, idx = 0) {
  const s = deref(schemaIn)
  if (!s || depth > 6) return null
  if (s.anyOf || s.oneOf) {
    const opts = (s.anyOf || s.oneOf).map(deref).filter((o) => o && o.type !== 'null')
    return opts.length ? gen(opts[0], key, depth + 1, idx) : null
  }
  if (s.allOf) return gen(s.allOf[0], key, depth + 1, idx)
  if (s.enum) return s.enum[idx % s.enum.length]
  switch (s.type) {
    case 'object': {
      const out = {}
      for (const [k, v] of Object.entries(s.properties || {})) out[k] = gen(v, k, depth + 1, idx)
      if (!s.properties && s.additionalProperties && typeof s.additionalProperties === 'object') out['합성'] = gen(s.additionalProperties, key, depth + 1, idx)
      return out
    }
    case 'array': {
      if (state.mode === 'empty') return []
      return [0, 1, 2].map((i) => gen(s.items, key, depth + 1, i))
    }
    case 'integer':
      if (/(^|_)id$/.test(key)) return 1 + idx
      if (/count|total|qty|quantity/.test(key)) return 3 + idx
      if (key === 'page') return 1
      if (key === 'page_size') return 20
      return 100 * (idx + 1)
    case 'number':
      return 12345.5 * (idx + 1)
    case 'boolean':
      return false
    case 'string':
      if (s.format === 'date-time') return '2026-10-10T09:00:00Z'
      if (s.format === 'date') return '2026-10-10'
      if (key === 'status') {
        const hit = STATUS_BY_PATH.find(([re]) => re.test(curPath))
        if (hit) return hit[1][idx % hit[1].length]
      }
      if (key === 'category' && !curPath.startsWith('/api/costs')) return `합성 분류 ${idx + 1}`
      if (key === 'type' && !curPath.startsWith('/api/inventory')) return `합성 유형 ${idx + 1}`
      if (FIELD_VALUES[key]) return FIELD_VALUES[key][idx % FIELD_VALUES[key].length]
      if (NAMES[key]) return `${NAMES[key]} ${idx + 1}`
      if (/_no$|code$/.test(key)) return `DEMO-${1000 + idx}`
      return `합성값 ${idx + 1}`
    default:
      return null
  }
}

// 백엔드가 응답 모델 없이 dict 를 돌려주는 엔드포인트는 OpenAPI 로 형태를 알 수 없으므로, 서비스 코드가 실제로
// 만드는 키 구조(services/order_view_service.py build_detail, cs_case_service.dashboard_summary,
// operations_dashboard_service.summary)에 맞춘 합성 데이터를 직접 둔다. 개인정보로 오인될 값은 null 로 둔다.
const T0 = '2026-10-10T09:00:00Z'
const empty = () => state.mode === 'empty'
const OVERRIDES = [
  [/^\/api\/orders\/[^/]+\/detail$/, () => ({
    id: 1, platform_order_no: 'DEMO-1000', platform_name: '합성 쇼핑몰', order_status: 'SHIPPING', payment_status: 'PAID', shipping_status: 'SHIPPING', cs_status: 'EXCHANGE',
    order_date: T0, payment_date: T0, delivery_completed_date: null, total_amount: 59000, discount_amount: 1000,
    assignee_id: null, assignee_name: null, tags: null,
    purchase_links: [{ purchase_order_id: 1, supplier_name: '합성 공급처', status: 'ORDERED', order_date: T0 }],
    contribution: { sale_amount: 59000, cost_of_goods: 30000, platform_fee: 5000, contribution_margin: 24000, contribution_rate: 40.7, ad_cost_note: '합성 안내 문구' },
    settlement: { settlement_id: 1, settlement_cycle: '2026-10', status: 'SCHEDULED', gross_amount: 59000, fee_amount: 5000, net_amount: 54000 },
    cs_history: empty() ? [] : [{ type: 'EXCHANGE', id: 1, reason: '합성 사유', status: 'REQUESTED', requested_at: T0 }],
    customer: { id: 1, name: '합성 고객', phone: null, email: null, address: null, grade: 'NORMAL', is_vip: false, order_count: 2 },
    items: empty() ? [] : [{ id: 1, product_option_id: 1, product_name: '합성 상품', option_name: '기본 옵션', sku_code: 'SKU-DEMO-1', channel_product_no: null, channel_option_no: null, quantity: 1, unit_price: 59000, line_amount: 59000 }],
    inventory: empty() ? [] : [{ sku_code: 'SKU-DEMO-1', warehouse_id: 1, sellable_stock: 10, reserved_stock: 1, available_stock: 9, safety_stock: 2 }],
    shipment: { carrier: '합성택배', tracking_no: null, status: 'SHIPPING', shipped_at: T0, delivered_at: null },
    memos: empty() ? [] : [{ id: 1, content: '합성 메모', created_by: 1, created_at: T0 }],
    status_history: [{ from_status: null, to_status: 'NEW', changed_at: T0 }, { from_status: 'NEW', to_status: 'SHIPPING', changed_at: T0 }],
  })],
  [/^\/api\/operations\/timeseries$/, () => (empty() ? [] : ['2026-10-04', '2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08', '2026-10-09', '2026-10-10'].map((date, i) => ({ date, success: 5 + i, failed: i % 3 })))],
  [/^\/api\/operations\/integrations$/, () => (empty() ? [] : [
    { integration_type: 'MALL', integration_code: 'DEMO1', status: 'NORMAL', last_success_at: T0, last_error_at: null, last_error_message: null, severity: 'INFO' },
    { integration_type: 'MALL', integration_code: 'DEMO2', status: 'TOKEN_EXPIRING', last_success_at: T0, last_error_at: T0, last_error_message: '합성 오류 안내', severity: 'WARNING' },
    { integration_type: 'AD', integration_code: 'DEMO3', status: 'ERROR', last_success_at: null, last_error_at: T0, last_error_message: '합성 오류 안내', severity: 'CRITICAL' },
  ])],
  [/^\/api\/operations\/scheduler-jobs$/, () => (empty() ? [] : [
    { target: 'demo_collect_job', task_type: 'ORDER_COLLECT', status: 'SUCCESS', started_at: T0, finished_at: T0, error_message: null, severity: 'INFO' },
    { target: 'demo_backup_job', task_type: 'BACKUP', status: 'FAILED', started_at: T0, finished_at: T0, error_message: '합성 오류 안내', severity: 'ERROR' },
  ])],
  [/^\/api\/cs-cases\/dashboard$/, () => ({ by_status: empty() ? {} : { OPEN: 3, IN_PROGRESS: 2, RESOLVED: 8 }, unassigned_count: empty() ? 0 : 2, overdue_count: empty() ? 0 : 1 })],
  [/^\/api\/operations\/summary$/, () => ({
    generated_at: T0,
    window_definition: { today_utc: [T0, T0], last_24h_utc: [T0, T0], last_7d_utc: [T0, T0], note: '합성 안내 문구' },
    orders: { collected_today: empty() ? 0 : 12, unshipped: empty() ? 0 : 5, delayed_unshipped: empty() ? 0 : 1 },
    fulfillment: { by_kpi_bucket: { READY_TO_PICK: 2, PICKING: 1, INSPECTING: 1, PACKED: 3 }, raw_by_status: { READY: 2, PICKING: 1 } },
    shipment_commands_by_status: { PENDING: 1, RUNNING: 0, RETRY_WAIT: 1, FAILED: 2, UNKNOWN: empty() ? 0 : 1 },
    product_commands_by_status: { PENDING: 0, RUNNING: 0, RETRY_WAIT: 0, FAILED: 1, UNKNOWN: 0 },
    order_status_conflicts_unresolved: empty() ? 0 : 2,
    cs: { by_status: { OPEN: 3, RESOLVED: 8 }, unassigned: 2, overdue: 1 },
    claims_pending: { exchange: 2, return: 1, cancellation: 0 },
    settlement_by_status: { SCHEDULED: 4, COMPLETED: 10 },
    success_rate: { last_24h: { success: 9, failed: 1, total: 10, rate_percent: 90 }, last_7d: { success: 40, failed: 2, total: 42, rate_percent: 95.2 }, scope_note: '합성 안내 문구' },
  })],
]

const routes = []
for (const [path, item] of Object.entries(spec.paths)) {
  const re = new RegExp('^' + path.replace(/\{[^}]+\}/g, '[^/]+') + '$')
  for (const [method, op] of Object.entries(item)) {
    if (!['get', 'post', 'patch', 'put', 'delete'].includes(method)) continue
    const resp = op.responses?.['200'] || op.responses?.['201'] || op.responses?.['202']
    const schema = resp?.content?.['application/json']?.schema
    routes.push({ method: method.toUpperCase(), re, path, schema, wildcard: (path.match(/\{/g) || []).length })
  }
}
routes.sort((a, b) => a.wildcard - b.wildcard)

function emptyLike(v) {
  if (Array.isArray(v)) return []
  if (v && typeof v === 'object') {
    const out = {}
    for (const [k, x] of Object.entries(v)) out[k] = Array.isArray(x) ? [] : /^(total|total_count|count)$/.test(k) ? 0 : x
    return out
  }
  return v
}

function respond(res, status, body) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' })
  res.end(JSON.stringify(body))
}

const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' }
const handler = (req, res) => {
  const url = new URL(req.url, 'http://x')
  const p = url.pathname
  if (p === '/__mock/state') { state.mode = url.searchParams.get('mode') || 'normal'; return respond(res, 200, state) }
  if (p === '/__mock/perm') { state.perm = url.searchParams.get('set') || 'all'; return respond(res, 200, state) }
  if (p === '/__mock/theme') { state.theme = url.searchParams.get('set') || 'LIGHT'; return respond(res, 200, state) }
  if (p === '/__metrics' && req.method === 'POST') {
    let raw = ''
    req.on('data', (c) => (raw += c))
    req.on('end', () => {
      if (process.env.METRICS_FILE) appendFileSync(process.env.METRICS_FILE, raw + '\n')
      respond(res, 200, {})
    })
    return
  }
  if (p === '/__qa.js' && process.env.QA_SCRIPT) { res.writeHead(200, { 'Content-Type': 'text/javascript' }); return res.end(readFileSync(process.env.QA_SCRIPT)) }
  if (p.startsWith('/api/')) {
    if (p === '/api/auth/me') {
      return respond(res, 200, { id: 1, username: 'demo', name: '데모 사용자', role_id: 1, is_active: true, theme_preference: state.theme, permissions: state.perm === 'limited' ? ['ORDER_VIEW', 'SHIPMENT_VIEW'] : PERMS })
    }
    if (state.mode === 'error') return respond(res, 500, { detail: 'Internal Server Error: Traceback (most recent call last)' })
    const route = routes.find((r) => r.method === req.method && r.re.test(p))
    if (!route) return respond(res, 404, { detail: 'Not Found' })
    if (!route.schema && !OVERRIDES.some(([re]) => re.test(p))) return respond(res, 200, {})
    curPath = p
    let body = gen(route.schema)
    const ov = OVERRIDES.find(([re]) => re.test(p))
    if (ov) body = ov[1]()
    if (p === '/api/notifications/unread-count') body = { unread_count: state.mode === 'empty' ? 0 : 3 }
    if (state.mode === 'empty' && body && !Array.isArray(body)) body = emptyLike(body)
    return respond(res, 200, body)
  }
  let file = join(DIST, p === '/' ? 'index.html' : p)
  if (!resolve(file).startsWith(DIST) || !existsSync(file) || statSync(file).isDirectory()) file = join(DIST, 'index.html')
  if (file.endsWith('index.html') && (process.env.AUTOLOGIN || process.env.CAPTURE)) {
    // 캡처 전용: 로그인 토큰(합성 값)을 심고, 페이지가 그려진 뒤 실제 viewport 값을 서버에 보고한다.
    const login = process.env.AUTOLOGIN && !p.startsWith('/login') ? "localStorage.setItem('erp_access_token','demo');" : "localStorage.removeItem('erp_access_token');"
    const beacon = process.env.CAPTURE
      ? "addEventListener('load',()=>setTimeout(()=>fetch('/__metrics',{method:'POST',body:JSON.stringify({path:location.pathname,iw:innerWidth,ih:innerHeight,dpr:devicePixelRatio,sw:document.documentElement.scrollWidth,cw:document.documentElement.clientWidth})}),1800));"
      : ''
    const html = readFileSync(file, 'utf8').replace('<head>', '<head><script>' + login + beacon + '</script>')
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' })
    return res.end(html)
  }
  res.writeHead(200, { 'Content-Type': types[extname(file)] || 'application/octet-stream' })
  createReadStream(file).pipe(res)
}
// mode=slow: 로딩 상태 검수용으로 API 응답(인증 제외)을 4초 늦춘다.
createServer((req, res) => {
  if (state.mode === 'slow' && req.url.startsWith('/api/') && !req.url.startsWith('/api/auth/me')) setTimeout(() => handler(req, res), 4000)
  else handler(req, res)
}).listen(PORT, '127.0.0.1', () => console.log('mock listening', PORT, 'routes', routes.length))
