// CI 설정(.github/workflows/ci.yml) 정적 계약 검사. 모든 frontend 검사가 CI 에서 실패를 숨기지 않고 실행되는지 확인한다.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

const read = (p: string) => readFileSync(new URL(p, import.meta.url), 'utf8').replace(/\r\n/g, '\n')
const yaml = read('../../.github/workflows/ci.yml')
const pkg = JSON.parse(read('../package.json'))
const strip = (t: string) =>
  t
    .split('\n')
    .filter((l) => !/^\s*#/.test(l))
    .join('\n') // 주석은 검사 대상이 아니다
const code = strip(yaml)
const job = strip(yaml.slice(yaml.indexOf('\n  frontend-quality:')))

test('frontend-quality job 이 있고 backend job 과 독립이다', () => {
  assert.ok(yaml.includes('\n  quality:'), '기존 backend job 이 유지되어야 합니다')
  assert.ok(job.length > 100, 'frontend-quality job 이 없습니다')
  assert.doesNotMatch(job, /\n\s+needs:/, 'backend job 에 의존하면 안 됩니다')
  assert.match(yaml, /on:\s*\n\s+push:\s*\n\s+branches: \["\*\*"\]\s*\n\s+pull_request:/, 'push·PR trigger 가 유지되어야 합니다')
})

test('working directory, npm ci, lockfile 무결성, npm cache 경로가 정확하다', () => {
  assert.match(job, /working-directory: frontend/)
  assert.match(job, /run: npm ci\b/)
  assert.match(job, /git diff --exit-code -- package\.json package-lock\.json/)
  assert.match(job, /cache: npm/)
  assert.match(job, /cache-dependency-path: frontend\/package-lock\.json/)
  assert.match(job, /actions\/checkout@v4/)
})

test('실행하는 npm script 가 package.json 에 실제로 있다', () => {
  for (const m of job.matchAll(/run: npm run (\w+)/g)) assert.ok(pkg.scripts[m[1]], `package.json 에 ${m[1]} script 가 없습니다`)
  assert.match(job, /run: npm test\b/)
  assert.ok(pkg.scripts.test && pkg.scripts.pretest)
  assert.match(pkg.scripts.pretest, /rmSync\('dist'/)
  assert.match(pkg.scripts.pretest, /npm run build/)
  assert.match(pkg.scripts.build, /tsc -b && vite build/)
  assert.match(job, /run: npx tsc -b/)
})

test('실패를 숨기지 않는다: continue-on-error·조건부 step·|| true 금지', () => {
  assert.doesNotMatch(job, /continue-on-error/)
  assert.doesNotMatch(job, /\n\s+if:/)
  assert.doesNotMatch(job, /\|\|\s*true/)
  assert.doesNotMatch(job, /--passWithNoTests|--no-bail|--silent/)
})

test('deploy·SSH·이미지 push·migration 단계가 없다(job 과 파일 전체)', () => {
  assert.doesNotMatch(code, /\b(ssh|scp|rsync|docker push|docker login|kubectl|alembic upgrade|deploy)\b/i)
  assert.doesNotMatch(job, /docker|services:|postgres/i, 'frontend job 이 Docker·PostgreSQL 자원을 쓰면 안 됩니다')
  assert.doesNotMatch(code, /secrets\./)
})

test('Node 버전: 테스트는 22(type stripping 필요), production build 는 Dockerfile 과 같은 20', () => {
  const dockerfile = read('../Dockerfile')
  const docker = dockerfile.match(/FROM node:(\d+)/)
  assert.ok(docker, 'Dockerfile 의 Node 버전을 찾지 못했습니다')
  const versions = [...job.matchAll(/node-version: "(\d+)\.x"/g)].map((m) => m[1])
  assert.deepEqual(versions, ['22', docker![1]], `예상 순서: 테스트 Node 22 → Dockerfile Node ${docker![1]}`)
  assert.ok(job.indexOf('run: npm test') < job.indexOf('Production build on the production image Node major'))
})

test('vite 경고 기준을 바꾸지 않는다', () => {
  assert.doesNotMatch(read('../vite.config.ts'), /chunkSizeWarningLimit/)
})
