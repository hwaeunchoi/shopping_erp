// 정확한 viewport 로 화면을 캡처하는 검수 도구(테스트 전용). Chrome DevTools Protocol 을 직접 쓰므로 새 의존성이 없다.
//
// 사용: node tests/visual/capture.mjs <jobs.json> <출력 폴더>
//   환경변수: CHROME(실행 파일 경로), BASES={"before":"http://127.0.0.1:5198","after":"http://127.0.0.1:5199"}
//   job: { name, base, path, w, h, state?, perm?, theme?, colorScheme?, actions?, fullPage? }
//   actions: [{ click: '선택자' } | { press: 'Escape' } | { wait: 300 } | { eval: '코드' }]
// 캡처 직전 브라우저 안에서 innerWidth/innerHeight/devicePixelRatio/scrollWidth 를 읽어 요청 viewport 와 일치하는지 검증하고,
// 일치하지 않거나 가로 넘침(scrollWidth > innerWidth)이 있으면 결과 JSON 에 실패로 기록한다.
import { spawn } from 'node:child_process'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

const [jobsFile, outDir] = process.argv.slice(2)
const CHROME = process.env.CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const BASES = JSON.parse(process.env.BASES || '{"after":"http://127.0.0.1:5199"}')
const PORT = 9333 + Math.floor(Math.random() * 500)
mkdirSync(outDir, { recursive: true })

const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--hide-scrollbars', `--remote-debugging-port=${PORT}`, '--user-data-dir=' + join(outDir, '.profile'), 'about:blank'], { stdio: 'ignore' })
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function connect() {
  for (let i = 0; i < 50; i++) {
    try {
      const targets = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json()
      const page = targets.find((t) => t.type === 'page')
      if (page) return page.webSocketDebuggerUrl
    } catch {}
    await sleep(200)
  }
  throw new Error('Chrome 에 연결하지 못했습니다')
}

const ws = new WebSocket(await connect())
await new Promise((r) => (ws.onopen = r))
let seq = 0
const pending = new Map()
ws.onmessage = (m) => {
  const msg = JSON.parse(m.data)
  if (msg.id && pending.has(msg.id)) {
    pending.get(msg.id)(msg)
    pending.delete(msg.id)
  }
}
const send = (method, params = {}) =>
  new Promise((resolve, reject) => {
    const id = ++seq
    pending.set(id, (msg) => (msg.error ? reject(new Error(method + ': ' + msg.error.message)) : resolve(msg.result)))
    ws.send(JSON.stringify({ id, method, params }))
  })
const evaluate = async (expression) => (await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })).result.value

await send('Page.enable')
await send('Runtime.enable')

const jobs = JSON.parse(readFileSync(jobsFile, 'utf8'))
const results = []
for (const job of jobs) {
  const base = BASES[job.base || 'after']
  await fetch(`${base}/__mock/state?mode=${job.state || 'normal'}`)
  await fetch(`${base}/__mock/perm?set=${job.perm || 'all'}`)
  await fetch(`${base}/__mock/theme?set=${job.theme || 'LIGHT'}`)
  await send('Emulation.setDeviceMetricsOverride', { width: job.w, height: job.h, deviceScaleFactor: 1, mobile: job.w < 600 })
  await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: job.colorScheme || 'light' }] })
  await send('Page.navigate', { url: base + job.path })
  await sleep(job.settle || 1800)
  for (const a of job.actions || []) {
    if (a.click) await evaluate(`document.querySelector(${JSON.stringify(a.click)})?.click()`)
    if (a.press) {
      await send('Input.dispatchKeyEvent', { type: 'keyDown', key: a.press, code: a.press })
      await send('Input.dispatchKeyEvent', { type: 'keyUp', key: a.press, code: a.press })
    }
    if (a.eval) await evaluate(a.eval)
    await sleep(a.wait || 250)
  }
  const m = await evaluate(`({ iw: innerWidth, ih: innerHeight, dpr: devicePixelRatio, sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth, title: document.querySelector('main h2, main h1')?.textContent ?? '', url: location.pathname, offenders: [...document.querySelectorAll('main *')].filter((e) => e.getBoundingClientRect().right > ${job.w} + 1).slice(0, 8).map((e) => e.tagName + '.' + (typeof e.className === 'string' ? e.className : '') + ' r=' + Math.round(e.getBoundingClientRect().right)) })`)
  const ok = m.iw === job.w && m.ih === job.h && m.sw <= m.iw
  const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: Boolean(job.fullPage) })
  const file = `${job.name}.png`
  writeFileSync(join(outDir, file), Buffer.from(shot.data, 'base64'))
  results.push({ name: job.name, file, requested: [job.w, job.h], ...m, ok })
  console.log(ok ? 'OK  ' : 'FAIL', job.name, `${m.iw}x${m.ih} dpr${m.dpr} scrollW=${m.sw}`, ok ? '' : JSON.stringify(m.offenders))
}
writeFileSync(join(outDir, 'viewport-evidence.json'), JSON.stringify(results, null, 2))
ws.close()
chrome.kill()
process.exit(results.every((r) => r.ok) ? 0 : 2)
