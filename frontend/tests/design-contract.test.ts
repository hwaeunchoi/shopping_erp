// 디자인 토큰 명암비, 번들(지연 로딩·크기·테스트 코드 제외), 모바일 규칙 검증.
import assert from 'node:assert/strict'
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

const read = (p: string) => readFileSync(new URL(p, import.meta.url), 'utf8').replace(/\r\n/g, '\n')
const tokens = read('../src/styles/tokens.css')

function block(selector: string): Record<string, string> {
  const start = tokens.indexOf(selector + ' {')
  const body = tokens.slice(start, tokens.indexOf('}', start))
  const vars: Record<string, string> = {}
  for (const m of body.matchAll(/--([\w-]+):\s*(#[0-9a-fA-F]{6})/g)) vars[m[1]] = m[2]
  return vars
}
const light = block(':root')
const dark = { ...light, ...block(":root[data-theme='dark']") }

function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4))
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}
function ratio(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

test('라이트·다크 모두 흰색·검정·회색 계열만 쓴다(색조 없음)', () => {
  for (const [mode, vars] of Object.entries({ light, dark })) {
    for (const [name, hex] of Object.entries(vars)) {
      const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16))
      assert.ok(r === g && g === b, `${mode} --${name}: ${hex} 는 회색 계열이 아닙니다`)
    }
  }
})

test('본문·보조 글자와 버튼·초점의 명암비가 WCAG AA 를 만족한다', () => {
  for (const [mode, v] of Object.entries({ light, dark })) {
    for (const bg of ['bg', 'panel-bg', 'bg-subtle', 'hover', 'selected']) {
      assert.ok(ratio(v.text, v[bg]) >= 7, `${mode} text/${bg} ${ratio(v.text, v[bg]).toFixed(2)}`)
      assert.ok(ratio(v['text-muted'], v[bg]) >= 4.5, `${mode} text-muted/${bg} ${ratio(v['text-muted'], v[bg]).toFixed(2)}`)
    }
    assert.ok(ratio(v['primary-text'], v.primary) >= 7, `${mode} 주요 버튼 글자`)
    assert.ok(ratio(v['secondary-text'], v['secondary-bg']) >= 7, `${mode} 보조 버튼 글자`)
    assert.ok(ratio(v['focus-ring'], v.bg) >= 3, `${mode} 초점 윤곽선(비텍스트 3:1)`)
    assert.ok(ratio(v['border-strong'], v.bg) >= 1.4, `${mode} 입력 테두리가 배경과 구분되지 않습니다`)
  }
})

test('활성 탭: 글자색이 배경색과 같아지지 않는다(다크 모드 회귀 방지)', () => {
  const css = read('../src/index.css')
  const rule = css.match(/\.tab-button\.active,\s*\.tab-button\.active:hover\s*\{([^}]*)\}/)
  assert.ok(rule, '활성 탭 규칙이 없습니다')
  assert.match(rule![1], /background:\s*none/)
  assert.match(rule![1], /color:\s*var\(--text\)/)
  assert.match(rule![1], /border-bottom-color:\s*var\(--primary\)/)
  // 활성 탭 글자(text)는 배경(bg/panel-bg)과 충분히 달라야 한다.
  for (const v of [light, dark]) assert.ok(ratio(v.text, v['panel-bg']) >= 7)
})

test('모바일 규칙: 표는 표 안에서만 가로 스크롤하고, 누르는 영역은 40px 이상', () => {
  const css = read('../src/index.css') + read('../src/styles/layout.css')
  assert.match(css, /\.content table:not\(\.order-table\)\s*\{[^}]*overflow-x:\s*auto/)
  // 최상위(미디어쿼리 밖) 규칙이어야 한다: 줄 맨 앞에서 시작하는 선택자만 본다.
  assert.match(css, /^\.kpi-cards\s*\{[^}]*flex-wrap:\s*wrap/m, 'KPI 카드 줄은 모든 viewport 에서 줄바꿈되어야 합니다')
  assert.match(css, /min-height:\s*40px/)
  assert.match(css, /@media \(max-width: 900px\)/)
  assert.match(css, /\.topbar \.global-search\s*\{[^}]*flex:\s*1 1 100%/)
})

const distDir = fileURLToPath(new URL('../dist/assets/', import.meta.url))
const distExists = existsSync(distDir)

test('route 단위 지연 로딩: 모든 화면이 lazyPage 로 불러와지고 Suspense 한국어 문구가 있다', () => {
  const app = read('../src/App.tsx')
  const pages = readdirSync(fileURLToPath(new URL('../src/pages/', import.meta.url))).filter((f) => f.endsWith('.tsx'))
  for (const page of pages) {
    const name = page.replace('.tsx', '')
    assert.match(app, new RegExp(`lazyPage\\(\\(\\) => import\\('\\./pages/${name}'\\), '${name}'\\)`), `${name} 가 지연 로딩이 아닙니다`)
    assert.doesNotMatch(app, new RegExp(`^import \\{ ${name} \\} from`, 'm'), `${name} 를 정적으로 import 하고 있습니다`)
  }
  assert.match(app, /<Suspense fallback=\{<PageLoading \/>\}>/)
  assert.match(app, /화면을 불러오는 중입니다/)
  const layout = read('../src/components/Layout.tsx')
  assert.match(layout, /<ErrorBoundary key=\{pathname\}>/, '오류 경계가 화면 단위로 초기화되어야 합니다')
})

test('빌드 산출물: 초기 JS 가 500kB 미만이고 테스트·모의·접근성 도구가 포함되지 않는다', { skip: !distExists && 'dist 없음(빌드 후 실행)' }, () => {
  const files = readdirSync(distDir).filter((f) => f.endsWith('.js'))
  const entry = files.find((f) => f.startsWith('index-'))
  assert.ok(entry)
  const entrySize = statSync(distDir + entry).size
  assert.ok(entrySize < 500 * 1000, `초기 JS ${entrySize} bytes`)
  assert.ok(files.length > 20, '화면별 chunk 로 나뉘어 있어야 합니다')
  for (const f of files) {
    const text = readFileSync(distDir + f, 'utf8')
    assert.doesNotMatch(text, /__mock|mock-api|axe-core|axe\.run|node:test|OPENAPI/i, `${f} 에 테스트·모의 코드가 있습니다`)
  }
})
