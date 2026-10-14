// axe-core 접근성 검사(테스트 전용, devDependency). 모의 API 서버가 떠 있어야 한다(mock-api.mjs).
// 사용: BASE=http://127.0.0.1:5199 node tests/visual/a11y.mjs <출력 json>
// 라이트·다크 × 1440/390 × 주요 화면(정상·빈·오류 상태)을 검사하고 위반을 요약한다. 위반이 있으면 종료 코드 2.
import { spawn } from 'node:child_process'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const BASE = process.env.BASE || 'http://127.0.0.1:5199'
const CHROME = process.env.CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const out = process.argv[2] || 'a11y-report.json'
const axeSource = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '../../node_modules/axe-core/axe.min.js'), 'utf8')
const PORT = 9333 + Math.floor(Math.random() * 500)
const profile = join(dirname(out), '.a11y-profile')
mkdirSync(dirname(out), { recursive: true })
const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', `--remote-debugging-port=${PORT}`, '--user-data-dir=' + profile, 'about:blank'], { stdio: 'ignore' })
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
ws.onmessage = (m) => {
  const msg = JSON.parse(m.data)
  if (msg.id && pending.has(msg.id)) pending.get(msg.id)(msg)
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
await send('Page.enable')
await send('Runtime.enable')

const ROUTES = ['/login', '/', '/orders', '/orders/1', '/products', '/products/1', '/cs-cases', '/returns', '/cancellations', '/reports', '/operations', '/settings', '/notifications', '/no-such-page']
const results = []
for (const theme of ['LIGHT', 'DARK']) {
  for (const state of ['normal', 'empty', 'error']) {
    if (theme === 'DARK' && state !== 'normal') continue
    await fetch(`${BASE}/__mock/state?mode=${state}`)
    await fetch(`${BASE}/__mock/theme?set=${theme}`)
    for (const [w, h] of [[1440, 900], [390, 844]]) {
      await send('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: 1, mobile: w < 600 })
      for (const path of ROUTES) {
        await send('Page.navigate', { url: BASE + path })
        await sleep(1600)
        await evaluate(axeSource)
        const r = await evaluate(`axe.run(document, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'] } }).then((x) => x.violations.map((v) => ({ id: v.id, impact: v.impact, count: v.nodes.length, help: v.help, sample: v.nodes[0].target.join(' ') })))`)
        results.push({ theme, state, w, path, violations: r })
        console.log(r.length ? 'VIOLATIONS' : 'ok        ', theme, state, w, path, r.map((v) => `${v.id}x${v.count}`).join(','))
      }
    }
  }
}
writeFileSync(out, JSON.stringify(results, null, 2))
ws.close()
chrome.kill()
const total = results.reduce((n, r) => n + r.violations.length, 0)
console.log('검사', results.length, '건, 위반 규칙', total)
process.exit(total ? 2 : 0)
