// UI 변이 검증 러너(테스트 전용, production 번들에 포함되지 않음).
// 사용: node tests/mutation/run.mjs [결과.json] [--only=<이름 일부>]
//
// 변이마다 다음을 보장한다.
//  1) 원본 해시 저장, 2) 대상 문자열이 정확히 한 번 존재, 3) 변이 전 baseline(관련 테스트) 통과,
//  4) 적용 후 해시가 실제로 달라짐, 5) 필요하면 clean production build(실패하면 실행 오류로 분류),
//  6) 관련 테스트만 실행(시간 상한), 7) 실패한 테스트 이름이 expect 와 일치해야 "검출"로 인정,
//  8) finally 에서 원복하고 해시 재검증, 9) 임시 파일·dist 정리.
// 결과 분류: DETECTED(관련 단언으로 실패) / SURVIVED(통과) / WRONG_REASON(다른 이유로 실패) / EXEC_ERROR(빌드·anchor·baseline 문제)
import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { MUTANTS, SENTINEL_FILE } from './manifest.mjs'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '../..')
const outFile = process.argv.find((a, i) => i >= 2 && !a.startsWith('--')) || join(ROOT, 'mutation-report.json')
const only = process.argv.find((a) => a.startsWith('--only='))?.slice(7)
const TIMEOUT = 180_000
const sha = (p) => createHash('sha256').update(readFileSync(p)).digest('hex')
const run = (cmd, args, timeout = TIMEOUT) => spawnSync(cmd, args, { cwd: ROOT, shell: process.platform === 'win32', encoding: 'utf8', timeout })
const cleanBuild = () => {
  rmSync(join(ROOT, 'dist'), { recursive: true, force: true })
  const r = run('npm', ['run', 'build'])
  return r.status === 0 && existsSync(join(ROOT, 'dist/index.html'))
}
// TAP 출력에서 실패한 테스트 이름과 첫 오류 줄을 뽑는다(고객정보 등은 출력에 없다).
function tests(files) {
  const r = run('node', ['--test', '--test-reporter=tap', ...files.map((f) => `tests/${f}`)])
  const out = (r.stdout || '') + (r.stderr || '')
  const failed = [...out.matchAll(/^\s*not ok \d+ - (.+)$/gm)].map((m) => m[1].trim())
  const err = out.match(/error: ['"|>-]*\s*\n?\s*(.{0,160})/)?.[1]?.trim() ?? ''
  const passed = [...out.matchAll(/^\s*ok \d+ - (.+)$/gm)].length
  return { status: r.status, failed, passed, err, out, timedOut: r.error?.code === 'ETIMEDOUT' }
}

const pristineFiles = new Map()
const results = []
const selected = MUTANTS.filter((m) => !only || m.name.includes(only))

// 공통 baseline: 변이 없이 npm test(pretest 포함)가 green 이어야 한다.
const base = run('npm', ['test'], 600_000)
if (base.status !== 0) {
  console.error('BASELINE FAIL: 변이 전 npm test 가 통과하지 않아 중단합니다.')
  process.exit(3)
}
console.log('BASELINE PASS (npm test, clean production build)')

for (const m of selected) {
  const path = join(ROOT, m.file)
  const rec = { name: m.name, file: m.file, tests: m.tests, result: 'EXEC_ERROR', reason: '' }
  const originalBytes = readFileSync(path)
  const originalHash = sha(path)
  const sentinel = join(ROOT, SENTINEL_FILE)
  try {
    const text = originalBytes.toString('utf8')
    const crlf = text.includes('\r\n')
    const lf = text.replace(/\r\n/g, '\n')
    const count = lf.split(m.find).length - 1
    if (count !== 1) {
      rec.reason = `anchor 가 ${count}번 존재합니다(정확히 1번이어야 함)`
      continue
    }
    // 변이 전 baseline(관련 테스트) - 원본 소스로 빌드한 뒤 통과해야 한다.
    if (!cleanBuild()) {
      rec.reason = '원본 소스 build 실패'
      continue
    }
    const b = tests(m.tests)
    if (b.status !== 0) {
      rec.reason = `변이별 baseline 실패: ${b.failed.join(' | ')}`
      continue
    }
    if (m.setup) writeFileSync(sentinel, "export const sentinel = 'mock-api-mutation-sentinel'\n")
    const mutated = lf.replace(m.find, () => m.replace)
    writeFileSync(path, crlf ? mutated.replace(/\n/g, '\r\n') : mutated)
    if (sha(path) === originalHash) {
      rec.reason = '변이가 파일을 실제로 바꾸지 않았습니다'
      continue
    }
    if (m.build && !cleanBuild()) {
      rec.reason = '변이 적용 후 build 실패(변이가 build 를 깨뜨림: 무관한 실패로 보고 검출로 세지 않음)'
      continue
    }
    const t = tests(m.tests)
    if (t.timedOut) {
      rec.reason = '시간 초과'
    } else if (t.status === 0) {
      rec.result = 'SURVIVED'
      rec.reason = '관련 테스트가 모두 통과했습니다'
    } else if (t.failed.length === 0) {
      rec.reason = `실패했지만 실패한 테스트를 식별하지 못했습니다(실행 오류 가능): ${t.err}`
    } else if (t.failed.some((n) => m.expect.test(n)) && (!m.msg || m.msg.test(t.out))) {
      rec.result = 'DETECTED'
      rec.reason = `실패한 단언: ${t.failed.filter((n) => m.expect.test(n)).join(' | ')}`
      rec.message = t.err.slice(0, 160)
    } else {
      rec.result = 'WRONG_REASON'
      rec.reason = `예상과 다른 테스트가 실패: ${t.failed.join(' | ')}`
    }
  } catch (e) {
    rec.reason = `러너 예외: ${String(e.message).slice(0, 160)}`
  } finally {
    writeFileSync(path, originalBytes)
    rmSync(sentinel, { force: true })
    rec.restored = sha(path) === originalHash
    if (!rec.restored) {
      console.error('복원 실패!', m.file)
      process.exitCode = 4
    }
    pristineFiles.set(m.file, originalHash)
    results.push(rec)
    console.log(rec.result.padEnd(12), m.name, '-', rec.reason.slice(0, 110))
  }
}
// 정리: 원본 소스로 clean build 를 한 번 더 만들어 dist 를 원상태로 둔다.
cleanBuild()
const summary = {
  total: results.length,
  detected: results.filter((r) => r.result === 'DETECTED').length,
  survived: results.filter((r) => r.result === 'SURVIVED').length,
  wrongReason: results.filter((r) => r.result === 'WRONG_REASON').length,
  execError: results.filter((r) => r.result === 'EXEC_ERROR').length,
  allRestored: results.every((r) => r.restored),
}
writeFileSync(outFile, JSON.stringify({ summary, results }, null, 2))
console.log(JSON.stringify(summary))
process.exit(summary.detected === summary.total && summary.allRestored ? 0 : 2)
