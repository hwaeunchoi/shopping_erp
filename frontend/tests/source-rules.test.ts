// 화면 소스 규칙 검사: 흑백 디자인, 한국어 표시, 접근성, 원문 enum 비노출.
import assert from 'node:assert/strict'
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src/', import.meta.url))

function walk(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => (e.isDirectory() ? walk(join(dir, e.name)) : [join(dir, e.name)]))
}
const files = walk(SRC)
const tsx = files.filter((f) => f.endsWith('.tsx'))
const css = files.filter((f) => f.endsWith('.css'))
const read = (f: string) => readFileSync(f, 'utf8')
const rel = (f: string) => f.slice(SRC.length).replace(/\\/g, '/')

test('색상값(#hex, rgb)은 tokens.css 에만 있다', () => {
  for (const f of [...css, ...tsx]) {
    if (rel(f) === 'styles/tokens.css') continue
    const text = read(f)
    assert.doesNotMatch(text, /#[0-9a-fA-F]{3,8}\b(?![-\w])/, `${rel(f)} 에 하드코딩된 색상이 있습니다`)
    assert.doesNotMatch(text, /\brgba?\(/, `${rel(f)} 에 rgb() 색상이 있습니다`)
  }
})

test('그라데이션과 바깥 그림자가 없다(왼쪽 표시선 inset 만 허용)', () => {
  for (const f of css) {
    const text = read(f)
    assert.doesNotMatch(text, /gradient\(/, `${rel(f)} 그라데이션`)
    for (const m of text.matchAll(/box-shadow:\s*([^;]+);/g)) {
      assert.match(m[1], /^inset /, `${rel(f)} 바깥 그림자: ${m[1]}`)
    }
  }
})

test('모든 border-radius 는 토큰이거나 원(50%)이다', () => {
  for (const f of css) {
    for (const m of read(f).matchAll(/border-radius:\s*([^;]+);/g)) {
      assert.match(m[1].trim(), /^(var\(--radius(-sm|-lg)?\)( var\(--radius(-sm|-lg)?\) 0 0)?|50%|0)$/, `${rel(f)}: ${m[1]}`)
    }
  }
})

test('초점 윤곽선을 제거하지 않는다', () => {
  const all = css.map(read).join('\n')
  assert.match(all, /:focus-visible\s*\{[^}]*outline:\s*2px solid/)
  for (const f of css) {
    for (const m of read(f).matchAll(/([^{}]+)\{[^}]*outline:\s*none[^}]*\}/g)) {
      assert.match(m[1].trim(), /^\.content:focus$/, `${rel(f)}: ${m[1].trim()} 에서 outline 을 제거했습니다`)
    }
  }
})

test('이모지가 없다(아이콘은 Icon 컴포넌트 한 종류만 쓴다)', () => {
  for (const f of [...tsx, ...files.filter((x) => x.endsWith('.ts')), ...css]) {
    const code = read(f)
      .split('\n')
      .filter((line) => !/^\s*(\/\/|\*|\/\*)/.test(line))
      .join('\n')
    assert.doesNotMatch(code, /\p{Extended_Pictographic}/u, `${rel(f)} 에 이모지가 있습니다`)
  }
})

test('아이콘만 있는 버튼에는 aria-label 이 있다', () => {
  for (const f of tsx) {
    const text = read(f)
    for (const m of text.matchAll(/<button\b([^>]*)>\s*<Icon\b[^>]*\/>\s*<\/button>/g)) {
      assert.match(m[1], /aria-label=/, `${rel(f)}: 아이콘 버튼에 aria-label 이 없습니다`)
    }
  }
})

test('화면에 상태 필드를 원문 그대로 출력하지 않는다', () => {
  const FIELD = /\{[A-Za-z_.?]+\.(status|severity|inquiry_type|cost_type|category|trigger_type|task_type|command_type|target_type|integration_type|frequency|output_format|metric|operator|claim_type|priority|reference_type|outcome|internal_status|order_status|payment_status|shipping_status|cs_status)\}/g
  for (const f of tsx) {
    for (const [i, line] of read(f).split('\n').entries()) {
      for (const m of line.matchAll(FIELD)) {
        const ctx = line.slice(Math.max(0, m.index! - 12), m.index!)
        // value={...}, key={...}, 템플릿 문자열 key 안의 사용은 화면 출력이 아니다.
        if (/\w+=$/.test(ctx) || /`[^`]*$/.test(line.slice(0, m.index!)) && /key=/.test(line)) continue
        assert.fail(`${rel(f)}:${i + 1} 원문 값을 그대로 출력합니다: ${m[0]}`)
      }
    }
  }
})

test('사용자에게 보이는 문구에 개발 용어가 없다', () => {
  const FORBIDDEN = /\b(outbox|scheduler|enum|payload|endpoint|traceback|stack ?trace|null|undefined|NaN)\b/i
  for (const f of tsx) {
    for (const [i, line] of read(f).split('\n').entries()) {
      if (/^\s*(\/\/|\*|\/\*|import )/.test(line)) continue
      // JSX 텍스트와 문자열 리터럴 중 한글이 섞인 줄만 사용자 문구로 본다.
      if (!/[가-힣]/.test(line)) continue
      const visible = [...line.matchAll(/(?:>|['"`])([^<>'"`{}]*[가-힣][^<>'"`{}]*)(?:<|['"`])/g)].map((m) => m[1]).join(' ')
      assert.doesNotMatch(visible, FORBIDDEN, `${rel(f)}:${i + 1} ${visible.trim().slice(0, 60)}`)
    }
  }
})

test('레이아웃: 서랍 메뉴·현재 위치·건너뛰기 링크의 접근성 속성이 있다', () => {
  const layout = read(join(SRC, 'components/Layout.tsx'))
  for (const needle of ['aria-label="메뉴 열기"', 'aria-label="메뉴 닫기"', 'aria-expanded', 'aria-controls', 'aria-current', "e.key === 'Escape'", 'className="skip-link"', 'aria-label="현재 위치"', 'aria-label="주 메뉴"']) {
    assert.ok(layout.includes(needle), `Layout.tsx 에 ${needle} 이 없습니다`)
  }
  const userMenu = read(join(SRC, 'components/UserMenu.tsx'))
  assert.ok(userMenu.includes('aria-haspopup') && userMenu.includes("e.key === 'Escape'"))
})

test('문서 언어는 한국어이고 404·오류 화면이 있다', () => {
  assert.match(readFileSync(new URL('../index.html', import.meta.url), 'utf8'), /<html lang="ko">/)
  assert.ok(read(join(SRC, 'pages/NotFoundPage.tsx')).includes('페이지를 찾을 수 없습니다'))
  assert.ok(read(join(SRC, 'components/ErrorBoundary.tsx')).includes('화면을 표시하지 못했습니다'))
  assert.match(read(join(SRC, 'App.tsx')), /path="\*"/)
})

test('서버 오류 문구는 한국어일 때만 그대로 쓰고 아니면 안전한 문구로 바꾼다', () => {
  const client = read(join(SRC, 'api/client.ts'))
  assert.ok(client.includes('friendlyHttpError') && client.includes('[가-힣]'))
  assert.doesNotMatch(read(join(SRC, 'api/useApiData.ts')), /HTTP \$\{/)
})

test('빌드 산출물(dist)에 비밀값·운영 식별자 형태가 없다', () => {
  const distUrl = new URL('../dist/', import.meta.url)
  assert.ok(existsSync(fileURLToPath(distUrl)), 'production build 후 테스트를 실행해야 합니다(npm test 는 build 를 먼저 실행합니다)')
  const distFiles = walk(fileURLToPath(distUrl))
  assert.ok(distFiles.length > 0, 'dist 가 비어 있습니다')
  for (const f of distFiles.filter((x) => /\.(js|css|html)$/.test(x))) {
    const text = readFileSync(f, 'utf8')
    assert.doesNotMatch(text, /eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\./, 'JWT 형태')
    assert.doesNotMatch(text, /(secret|api)[_-]?key["']?\s*[:=]\s*["'][A-Za-z0-9/+]{16,}/i, '키 형태')
    assert.doesNotMatch(text, /\bA0\d{7}\b/, '업체 코드 형태')
    assert.doesNotMatch(text, /(10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+)/, '내부 IP')
  }
})
