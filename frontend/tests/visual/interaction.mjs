// 실제 브라우저(Chrome, CDP)에서 키보드·마우스 상호작용을 검증한다(테스트 전용). 모의 API 서버가 떠 있어야 한다.
// 사용: BASE=http://127.0.0.1:5199 node tests/visual/interaction.mjs
// 검증: 행 작업 메뉴(열기·방향키·Esc·바깥 클릭·Tab·위험 작업 확인창·모바일 위치), 서랍 메뉴(열기·Esc·초점),
//       키보드만으로 메뉴 이동, 새로고침/직접 URL 접근, 권한별 메뉴, 뒤로가기.
import { spawn } from 'node:child_process'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const BASE = process.env.BASE || 'http://127.0.0.1:5199'
const CHROME = process.env.CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const PORT = 9333 + Math.floor(Math.random() * 500)
const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', `--remote-debugging-port=${PORT}`, '--user-data-dir=' + mkdtempSync(join(tmpdir(), 'cdp-')), 'about:blank'], { stdio: 'ignore' })
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
let wsUrl
for (let i = 0; i < 50 && !wsUrl; i++) {
  try {
    wsUrl = (await (await fetch(`http://127.0.0.1:${PORT}/json`)).json()).find((t) => t.type === 'page')?.webSocketDebuggerUrl
  } catch {}
  if (!wsUrl) await sleep(200)
}
const ws = new WebSocket(wsUrl)
await new Promise((r) => (ws.onopen = r))
let seq = 0
const pending = new Map()
const dialogs = []
ws.onmessage = (m) => {
  const msg = JSON.parse(m.data)
  if (msg.id && pending.has(msg.id)) pending.get(msg.id)(msg)
  if (msg.method === 'Page.javascriptDialogOpening') {
    dialogs.push(msg.params.message)
    send('Page.handleJavaScriptDialog', { accept: globalThis.__accept ?? true })
  }
}
const send = (method, params = {}) =>
  new Promise((resolve, reject) => {
    const id = ++seq
    pending.set(id, (msg) => (msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result)))
    ws.send(JSON.stringify({ id, method, params }))
  })
const evaluate = async (expression) => {
  const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.text + ' ' + (r.exceptionDetails.exception?.description ?? ''))
  return r.result.value
}
const key = async (k, code = k) => {
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: k, code, ...(k === 'Enter' ? { text: String.fromCharCode(13) } : {}), windowsVirtualKeyCode: { Tab: 9, Enter: 13, Escape: 27, ArrowDown: 40, ArrowUp: 38, Home: 36, End: 35 }[k] })
  await send('Input.dispatchKeyEvent', { type: 'keyUp', key: k, code })
  await sleep(120)
}
await send('Page.enable')
await send('Runtime.enable')

const results = []
const check = (name, ok, detail = '') => {
  results.push({ name, ok })
  console.log(ok ? 'PASS' : 'FAIL', name, detail)
}
async function open(path, w, h, perm = 'all') {
  await fetch(`${BASE}/__mock/state?mode=normal`)
  await fetch(`${BASE}/__mock/perm?set=${perm}`)
  await fetch(`${BASE}/__mock/theme?set=LIGHT`)
  await send('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: 1, mobile: w < 600 })
  await send('Page.navigate', { url: BASE + path })
  await sleep(1500)
}
const menuState = () => evaluate(`(() => { const m = document.querySelector('[role=menu]'); const t = document.querySelector('.row-action-trigger'); const r = m && m.getBoundingClientRect(); return { open: !!m, expanded: t?.getAttribute('aria-expanded'), haspopup: t?.getAttribute('aria-haspopup'), items: [...document.querySelectorAll('[role=menuitem]')].map((x) => x.textContent.trim()), focus: document.activeElement?.textContent?.trim(), focusIsTrigger: document.activeElement === t, inView: r ? r.left >= 0 && r.right <= innerWidth && r.top >= 0 && r.bottom <= innerHeight : null, rect: r && [Math.round(r.left), Math.round(r.top), Math.round(r.right), Math.round(r.bottom)] } })()`)

for (const [w, h] of [[1440, 900], [390, 844]]) {
  await open('/products', w, h)
  await evaluate(`document.querySelector('.row-action-trigger').focus()`)
  let s = await menuState()
  check(`[${w}] 더보기 버튼: aria-haspopup=menu, 닫힘 상태`, s.haspopup === 'menu' && s.expanded === 'false' && !s.open)
  await key('Enter')
  s = await menuState()
  check(`[${w}] Enter 로 메뉴가 열리고 aria-expanded=true`, s.open && s.expanded === 'true', JSON.stringify(s.items))
  check(`[${w}] 일반 작업(복제)이 먼저, 위험 작업(삭제)이 마지막`, s.items[0] === '복제' && s.items[s.items.length - 1].includes('삭제'))
  check(`[${w}] 메뉴가 화면 안에 있다`, s.inView === true, JSON.stringify(s.rect))
  check(`[${w}] 열리면 첫 항목에 초점`, s.focus === '복제')
  await key('ArrowDown')
  s = await menuState()
  check(`[${w}] ArrowDown 으로 위험 작업으로 이동`, s.focus.includes('삭제'))
  await key('Home')
  s = await menuState()
  check(`[${w}] Home 으로 첫 항목`, s.focus === '복제')
  await key('Escape')
  s = await menuState()
  check(`[${w}] Esc 로 닫히고 버튼으로 초점 복귀`, !s.open && s.focusIsTrigger)
  await key('ArrowDown')
  s = await menuState()
  check(`[${w}] 버튼에서 ArrowDown 으로도 열린다`, s.open)
  await evaluate(`document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))`)
  await sleep(150)
  s = await menuState()
  check(`[${w}] 바깥 클릭으로 닫힌다`, !s.open)
  await key('Enter')
  await key('Tab')
  s = await menuState()
  check(`[${w}] Tab 으로 닫힌다`, !s.open)
  // 위험 작업: 확인창을 거치고, 취소하면 실행되지 않는다.
  await open('/notifications', w, h)
  await evaluate(`[...document.querySelectorAll('.tab-button')].find((b) => b.textContent.includes('규칙'))?.click()`)
  await sleep(700)
  const rules = await evaluate(`document.querySelectorAll('.row-action-trigger').length`)
  if (rules > 0) {
    globalThis.__accept = false
    dialogs.length = 0
    await evaluate(`document.querySelector('.row-action-trigger').click()`)
    await sleep(150)
    await evaluate(`[...document.querySelectorAll('[role=menuitem]')].find((x) => x.textContent.includes('삭제')).click()`)
    await sleep(300)
    check(`[${w}] 위험 작업(삭제)은 확인창을 띄운다`, dialogs.length === 1, dialogs[0] ?? '')
    globalThis.__accept = true
  } else {
    check(`[${w}] 알림 규칙 행 확인(데이터 없음)`, false, '규칙 행 없음')
  }
}

// 서랍 메뉴(모바일)
await open('/orders', 390, 844)
await evaluate(`document.querySelector('.menu-button').click()`)
await sleep(300)
let d = await evaluate(`({ open: document.querySelector('.sidebar').classList.contains('is-open'), expanded: document.querySelector('.menu-button').getAttribute('aria-expanded'), focus: document.activeElement?.getAttribute('aria-label'), vis: getComputedStyle(document.querySelector('.sidebar')).visibility })`)
check('서랍이 열리고 aria-expanded=true, 닫기 버튼에 초점', d.open && d.expanded === 'true' && d.focus === '메뉴 닫기', JSON.stringify(d))
await key('Escape')
d = await evaluate(`({ open: document.querySelector('.sidebar').classList.contains('is-open'), focusLabel: document.activeElement?.getAttribute('aria-label'), vis: getComputedStyle(document.querySelector('.sidebar')).visibility })`)
check('Esc 로 서랍이 닫히고 메뉴 버튼으로 초점 복귀, 닫힌 서랍은 숨김', !d.open && d.focusLabel === '메뉴 열기' && d.vis === 'hidden', JSON.stringify(d))
await evaluate(`document.querySelector('.menu-button').click()`)
await sleep(200)
await evaluate(`document.querySelector('.drawer-overlay').click()`)
await sleep(200)
d = await evaluate(`document.querySelector('.sidebar').classList.contains('is-open')`)
check('오버레이 클릭으로 서랍이 닫힌다', d === false)
await evaluate(`document.querySelector('.menu-button').click()`)
await sleep(200)
await evaluate(`[...document.querySelectorAll('.nav-group-button')].find((b) => b.textContent.includes('고객지원')).click()`)
await sleep(100)
await evaluate(`[...document.querySelectorAll('.nav-link')].find((a) => a.textContent === '반품').click()`)
await sleep(600)
d = await evaluate(`({ path: location.pathname, open: document.querySelector('.sidebar').classList.contains('is-open'), h2: document.querySelector('main h2')?.textContent })`)
check('메뉴로 이동하면 서랍이 닫히고 해당 화면이 열린다', d.path === '/returns' && !d.open && d.h2 === '반품', JSON.stringify(d))

// 키보드만으로 대분류 펼침·이동 (데스크톱)
await open('/', 1440, 900)
await evaluate(`[...document.querySelectorAll('.nav-group-button')].find((b) => b.textContent.includes('주문·배송')).focus()`)
await key('Enter')
d = await evaluate(`({ exp: document.activeElement.getAttribute('aria-expanded'), links: document.querySelectorAll('#nav-orders .nav-link').length })`)
check('대분류 버튼을 Enter 로 펼치면 aria-expanded=true 와 소분류가 보인다', d.exp === 'true' && d.links >= 3, JSON.stringify(d))

// 직접 URL·새로고침·뒤로가기, 알 수 없는 경로
for (const [path, title] of [['/orders/1', '주문 상세 #1'], ['/reports', '보고서'], ['/operations', '운영 현황'], ['/cs-cases', '고객 문의'], ['/no-such-page', '페이지를 찾을 수 없습니다']]) {
  await open(path, 1280, 720)
  const t = await evaluate(`document.querySelector('main h2')?.textContent ?? ''`)
  check(`직접 URL ${path} -> ${title}`, t.startsWith(title.split(' ')[0]), t)
}
await open('/orders', 1280, 720)
await send('Page.reload')
await sleep(1500)
let p = await evaluate(`location.pathname + '|' + (document.querySelector('main h2')?.textContent ?? '')`)
check('새로고침 후에도 같은 화면', p === '/orders|주문', p)
await evaluate(`[...document.querySelectorAll('.nav-group-button')].find((b) => b.textContent.includes('고객지원')).click()`)
await evaluate(`[...document.querySelectorAll('.nav-link')].find((a) => a.textContent === '취소').click()`)
await sleep(700)
await evaluate(`history.back()`)
await sleep(700)
p = await evaluate(`location.pathname`)
check('브라우저 뒤로가기로 이전 화면', p === '/orders', p)

// 권한별 메뉴: 제한 권한 사용자는 주문·배송만 보인다
await open('/orders', 1440, 900, 'limited')
const labels = await evaluate(`[...document.querySelectorAll('.nav-group-button, .nav-group-single')].map((x) => x.textContent.trim())`)
check('제한 권한: 설정·정산·상품·연동 메뉴는 숨기고 주문·배송과 권한 없이 보이는 고객만 남는다', ['설정', '정산·매출', '상품·재고', '연동·작업'].every((x) => !labels.includes(x)) && labels.includes('주문·배송') && labels.includes('고객지원'), labels.join(','))
await fetch(`${BASE}/__mock/perm?set=all`)

ws.close()
chrome.kill()
const failed = results.filter((r) => !r.ok)
console.log(`\n검증 ${results.length}건, 실패 ${failed.length}건`)
process.exit(failed.length ? 2 : 0)
