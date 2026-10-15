// UI 변이(mutation) 목록. 각 변이는 production 소스 한 곳을 일부러 망가뜨리고, 관련 테스트가 "예상한 단언"으로
// 실패하는지 확인한다. 테스트 전용 경로라 production 번들에 포함되지 않는다.
//
// 필드: name, file(frontend 기준), find(정확히 한 번만 있어야 하는 원문), replace, tests(실행할 테스트 파일들),
//       expect(실패한 테스트 이름이 맞아야 하는 정규식), msg?(실패 출력에 있어야 하는 문구), setup?(보조 파일)
const SENTINEL_JWT = ['ey', 'JzeW50aGV0aWNTZW50', 'aW5lbEhlYWRlcg', '.ey', 'JzeW50aGV0aWNQYXlsb2FkWFhYWA', '.c3ludGhl', 'dGljU2lnbmF0dXJl'].join('') // 합성 값(실제 토큰 아님)
const PAD = 'x'.repeat(520_000) // 상수 접기로 사라지지 않게 아래에서 런타임 값과 이어 붙인다

export const SENTINEL_FILE = 'tests/visual/mutation-sentinel.mjs'

export const MUTANTS = [
  // --- 메뉴·권한·route ---
  { name: 'nav permission removed from a menu item', file: 'src/navigation.ts', find: "to: '/settings', label: '설정', permission: 'SETTINGS_MANAGE'", replace: "to: '/settings', label: '설정'", tests: ['navigation.test.ts'], expect: /route와 권한/ },
  { name: 'duplicate route menu allowed', file: 'src/navigation.ts', find: "{ to: '/shipments', label: '배송', permission: 'SHIPMENT_VIEW' },", replace: "{ to: '/shipments', label: '배송', permission: 'SHIPMENT_VIEW' },\n      { to: '/shipments', label: '배송 목록', permission: 'SHIPMENT_VIEW' },", tests: ['navigation.test.ts'], expect: /중복/ },
  { name: 'duplicate top-level group allowed', file: 'src/navigation.ts', find: "id: 'orders',", replace: "id: 'home',", tests: ['navigation.test.ts'], expect: /중복/ },
  { name: 'English menu label', file: 'src/navigation.ts', find: "label: '고객 문의'", replace: "label: 'CS_CASES'", tests: ['navigation.test.ts'], expect: /한국어/ },
  { name: 'dead menu link', file: 'src/navigation.ts', find: "{ to: '/costs', label: '비용', permission: 'COST_MANAGE' },", replace: "{ to: '/costs', label: '비용', permission: 'COST_MANAGE' },\n      { to: '/does-not-exist', label: '없는 화면' },", tests: ['navigation.test.ts'], expect: /죽은 링크|route와 권한/ },
  { name: 'permission check ignored (unauthorized menus shown)', file: 'src/navigation.ts', find: '!item.permission || granted.has(item.permission)', replace: 'true', tests: ['navigation.test.ts'], expect: /권한이 없으면/ },
  { name: 'empty top-level group still shown', file: 'src/navigation.ts', find: '})).filter((g) => g.items.length > 0)', replace: '}))', tests: ['navigation.test.ts'], expect: /권한이 없으면|빈 대분류/ },
  { name: 'page title differs from menu name', file: 'src/pages/CostsPage.tsx', find: '<h2>비용</h2>', replace: '<h2>비용관리</h2>', tests: ['navigation.test.ts'], expect: /페이지 제목/ },
  { name: '404 route removed', file: 'src/App.tsx', find: 'path="*"', replace: 'path="/__x"', tests: ['navigation.test.ts', 'source-rules.test.ts'], expect: /404|죽은 링크|도달/ },
  // --- 한국어·원문 enum ---
  { name: 'Korean label shown as raw enum', file: 'src/labels.ts', find: "OPEN: { label: '접수', tone: 'warn' },", replace: "OPEN: { label: 'OPEN', tone: 'warn' },", tests: ['labels.test.ts'], expect: /내부 코드|기본 상태 문구|한국어/ },
  { name: 'Korean label mapping removed', file: 'src/labels.ts', find: "IN_PROGRESS: { label: '처리 중', tone: 'info' },", replace: '', tests: ['labels.test.ts'], expect: /기본 상태 문구/ },
  { name: 'unknown value printed raw', file: 'src/labels.ts', find: 'return entry ? entry.label : UNKNOWN_LABEL', replace: 'return entry ? entry.label : value', tests: ['labels.test.ts'], expect: /알 수 없는 값/ },
  { name: 'unknown channel status printed raw', file: 'src/labels.ts', find: 'return known ? known.label : UNKNOWN_CHANNEL_STATUS_LABEL', replace: 'return known ? known.label : value', tests: ['labels.test.ts'], expect: /채널 상태/ },
  { name: 'raw status field rendered directly', file: 'src/pages/SettlementsPage.tsx', find: '<td><StatusBadge domain="settlementStatus" value={s.status} /></td>', replace: '<td>{s.status}</td>', tests: ['source-rules.test.ts'], expect: /원문/ },
  { name: 'html lang changed to en', file: 'index.html', find: '<html lang="ko">', replace: '<html lang="en">', tests: ['source-rules.test.ts'], expect: /문서 언어/ },
  // --- 디자인·접근성 ---
  { name: 'hardcoded color', file: 'src/pages/CostsPage.tsx', find: '<h2>비용</h2>', replace: "<h2 style={{ color: '#ff0000' }}>비용</h2>", tests: ['source-rules.test.ts'], expect: /색상값/ },
  { name: 'drop shadow added', file: 'src/index.css', find: '.empty-state {', replace: '.empty-state {\n  box-shadow: 0 2px 6px var(--border);', tests: ['source-rules.test.ts'], expect: /그림자/ },
  { name: 'focus outline removed', file: 'src/styles/layout.css', find: '.skip-link:focus {', replace: '.skip-link:focus {\n  outline: none;', tests: ['source-rules.test.ts'], expect: /초점/ },
  { name: 'emoji added', file: 'src/pages/NotFoundPage.tsx', find: '대시보드로 이동', replace: '🏠 대시보드로 이동', tests: ['source-rules.test.ts'], expect: /이모지/ },
  { name: 'icon button without aria-label', file: 'src/components/Layout.tsx', find: 'aria-label="메뉴 열기"\n            aria-expanded={drawerOpen}', replace: 'aria-expanded={drawerOpen}', tests: ['source-rules.test.ts'], expect: /서랍|접근성/ },
  { name: 'active tab text color regression', file: 'src/index.css', find: '  border-bottom-color: var(--primary);\n  color: var(--text);\n  font-weight: 700;', replace: '  border-bottom-color: var(--primary);\n  color: var(--primary-text);\n  font-weight: 700;', tests: ['design-contract.test.ts'], expect: /활성 탭/ },
  { name: 'low-contrast muted text token', file: 'src/styles/tokens.css', find: '--text-muted: #666666;', replace: '--text-muted: #aaaaaa;', tests: ['design-contract.test.ts'], expect: /명암비/ },
  { name: 'mobile table overflow rule removed', file: 'src/index.css', find: '    max-width: 100%;\n    overflow-x: auto;\n  }\n\n  .content table:not(.order-table) th,', replace: '    max-width: 100%;\n  }\n\n  .content table:not(.order-table) th,', tests: ['design-contract.test.ts'], expect: /모바일 규칙/ },
  { name: 'mobile KPI wrapping removed', file: 'src/index.css', find: '  display: flex;\n  flex-wrap: wrap;\n  gap: 16px;\n  margin-bottom: 24px;', replace: '  display: flex;\n  gap: 16px;\n  margin-bottom: 24px;', tests: ['design-contract.test.ts'], expect: /모바일 규칙/ },
  // --- 행 작업 메뉴 ---
  { name: 'danger action not moved to the end', file: 'src/rowActionMenu.ts', find: 'return [...shown.filter((a) => !a.danger), ...shown.filter((a) => a.danger)]', replace: 'return shown', tests: ['row-actions.test.ts'], expect: /위험 작업은 항상/ },
  { name: 'unauthorized (hidden) action still shown', file: 'src/rowActionMenu.ts', find: 'actions.filter((a) => !a.hidden)', replace: 'actions', tests: ['row-actions.test.ts'], expect: /hidden/ },
  { name: 'danger action confirmation removed', file: 'src/components/RowActionMenu.tsx', find: 'if (action.danger && !action.skipConfirm && !window.confirm(', replace: 'if (false && !window.confirm(', tests: ['row-actions.test.ts'], expect: /확인창/ },
  { name: 'menu Esc handling removed', file: 'src/components/RowActionMenu.tsx', find: "if (e.key === 'Escape') {", replace: "if (e.key === 'F24') {", tests: ['row-actions.test.ts'], expect: /Esc 로 닫고/ },
  { name: 'menu outside-click close removed', file: 'src/components/RowActionMenu.tsx', find: "document.addEventListener('mousedown', onMouseDown)", replace: '', tests: ['row-actions.test.ts'], expect: /바깥 클릭과 Tab/ },
  { name: 'danger actions placed directly in rows (menu unused)', file: 'src/pages/ProductsPage.tsx', find: '<RowActionMenu', replace: '<span data-removed="RowActionMenu"', tests: ['row-actions.test.ts'], expect: /실제 화면 적용/, msg: /\(<RowActionMenu>\)를 쓰지 않습니다/ },
  // --- 계약 ---
  { name: 'report internal value replaced by Korean label', file: 'src/pages/ReportsPage.tsx', find: "report_type: 'PROFIT_REPORT',", replace: "report_type: '손익 보고서',", tests: ['row-actions.test.ts'], expect: /보고서 구분 이름/ },
  // --- production bundle / import graph ---
  { name: 'route lazy import removed', file: 'src/App.tsx', find: "const OrdersPage = lazyPage(() => import('./pages/OrdersPage'), 'OrdersPage')", replace: "import { OrdersPage } from './pages/OrdersPage'", tests: ['design-contract.test.ts'], expect: /지연 로딩/, build: true },
  { name: 'production entry imports a mock module', file: 'src/main.tsx', find: "import './index.css'", replace: "import './index.css'\nimport '../tests/visual/mutation-sentinel.mjs'", tests: ['design-contract.test.ts'], expect: /import graph|테스트·모의/, build: true, setup: true },
  { name: 'production entry imports a visual-audit module', file: 'src/App.tsx', find: "import { Route, Routes } from 'react-router-dom'", replace: "import { Route, Routes } from 'react-router-dom'\nimport '../tests/visual/mutation-sentinel.mjs'", tests: ['design-contract.test.ts'], expect: /import graph|테스트·모의/, build: true, setup: true },
  { name: 'production entry imports axe-core', file: 'src/labels.ts', find: 'export const UNKNOWN_LABEL', replace: "import 'axe-core'\nexport const UNKNOWN_LABEL", tests: ['design-contract.test.ts'], expect: /import graph|테스트·모의/, build: true },
  { name: 'mock marker inside the production bundle', file: 'src/main.tsx', find: 'createRoot(', replace: "console.log('mock-api'); createRoot(", tests: ['design-contract.test.ts'], expect: /초기 JS|테스트·모의/, build: true },
  { name: 'initial bundle exceeds 500 kB', file: 'src/main.tsx', find: 'createRoot(', replace: `console.log('${PAD}' + Math.random()); createRoot(`, tests: ['design-contract.test.ts'], expect: /초기 JS/, build: true },
  { name: 'secret-shaped token inside the bundle', file: 'src/main.tsx', find: 'createRoot(', replace: `console.log('${SENTINEL_JWT}'); createRoot(`, tests: ['source-rules.test.ts'], expect: /비밀값|JWT/, build: true },
]
