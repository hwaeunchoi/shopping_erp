import { useEffect, useState, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'
import { useApiData } from '../api/useApiData'
import type {
  ApiCredentialMasked,
  ApiCredentialUpsert,
  PlatformCapability,
  RolePermissions,
  SettingsPermission,
  SettingsRole,
  SettingsUser,
  SettingsUserCreate,
  SystemSetting,
} from '../api/types'

// capability 키 -> 화면 표시 라벨. api/routers/platforms.py의 CAPABILITY_KEYS와
// 정확히 같은 키 집합이어야 한다(새 capability가 백엔드에 추가되면 여기도 추가).
const CAPABILITY_LABELS: Record<string, string> = {
  product_create: '신규 상품 등록',
  product_option_create: '옵션조합 등록',
  product_info_update: '상품정보 수정',
  inventory_update: '재고 전송',
  sale_status_update: '판매상태 전송',
  shipment_submit: '송장 전송',
  cancellation_sync: '취소 수집',
  return_sync: '반품 수집',
  exchange_sync: '교환 수집',
  cancellation_lookup_by_order: '주문단위 취소조회',
  settlement_sync: '정산 수집',
  settlement_detail_sync: '정산 상세 수집',
}

function ChannelsTab() {
  const { data, error, isLoading } = useApiData<PlatformCapability[]>(
    () => api.get('/api/platforms/capability-matrix'),
    [],
  )

  return (
    <div>
      <p className="hint-text">
        채널별로 공식 API 계약이 실제로 확인·검증되었는지와, 검증된 채널의 기능별 지원 여부를 보여줍니다. "공식 계약
        미확인"은 개별 기능을 점검해서 전부 미지원으로 나온 것이 아니라, 아직 실 연동 자체가 없다는 뜻입니다.
      </p>
      {isLoading && <p>불러오는 중...</p>}
      {error && <p className="form-error">{error}</p>}
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>채널</th>
              <th>플랫폼 상태</th>
              <th>공식 계약</th>
              <th>지원 기능</th>
              <th>마지막 성공</th>
              <th>마지막 실패</th>
            </tr>
          </thead>
          <tbody>
            {data?.map((p) => {
              const supported = p.capabilities ? Object.entries(p.capabilities).filter(([, v]) => v) : []
              return (
                <tr key={p.id}>
                  <td>{p.name}</td>
                  <td>
                    <span className="status-badge">{p.is_active ? '활성' : '비활성'}</span>
                  </td>
                  <td>
                    {p.official_contract_verified ? (
                      <span className="status-badge">검증됨</span>
                    ) : (
                      <span className="status-badge">공식 계약 미확인</span>
                    )}
                  </td>
                  <td>
                    {p.capabilities === null
                      ? '-'
                      : supported.length === 0
                        ? '지원 기능 없음'
                        : supported.map(([k]) => CAPABILITY_LABELS[k] ?? k).join(', ')}
                  </td>
                  <td>{p.last_success_at ? new Date(p.last_success_at).toLocaleString() : '이력 없음'}</td>
                  <td>
                    {p.last_error_at ? (
                      <>
                        {new Date(p.last_error_at).toLocaleString()}
                        {p.last_error_message && ` (${p.last_error_message})`}
                      </>
                    ) : (
                      '이력 없음'
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// 설정 화면: 사용자관리/역할관리/권한관리/API Credential관리/시스템설정 5개 탭.
// 전체가 백엔드 SETTINGS_MANAGE 권한으로 보호되며, 이 화면 자체도 그 권한이 있는
// 사용자에게만 사이드바 메뉴로 노출된다(Layout.tsx의 role 기반 메뉴 숨김 참고).

function UsersTab() {
  const { data, error, isLoading, reload } = useApiData<SettingsUser[]>(() => api.get('/api/settings/users'), [])
  const { data: roles } = useApiData<SettingsRole[]>(() => api.get('/api/settings/roles'), [])
  const [form, setForm] = useState<SettingsUserCreate>({ username: '', password: '', name: '', role_id: 0 })
  const [createError, setCreateError] = useState<string | null>(null)

  const handleCreate = async (e: FormEvent) => {
    e.preventDefault()
    if (!form.username || !form.password || !form.name || !form.role_id) {
      setCreateError('아이디/비밀번호/이름/역할을 모두 입력하세요.')
      return
    }
    setCreateError(null)
    try {
      await api.post('/api/settings/users', form)
      setForm({ username: '', password: '', name: '', role_id: 0 })
      reload()
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.message : '사용자 등록 중 오류가 발생했습니다.')
    }
  }

  async function toggleActive(user: SettingsUser) {
    await api.patch(`/api/settings/users/${user.id}`, { is_active: !user.is_active })
    reload()
  }

  async function changeRole(user: SettingsUser, roleId: number) {
    await api.patch(`/api/settings/users/${user.id}`, { role_id: roleId })
    reload()
  }

  return (
    <div>
      <form className="inline-form" onSubmit={handleCreate}>
        <input value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} placeholder="아이디" />
        <input
          type="password"
          value={form.password}
          onChange={(e) => setForm({ ...form, password: e.target.value })}
          placeholder="비밀번호"
        />
        <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="이름" />
        <select value={form.role_id} onChange={(e) => setForm({ ...form, role_id: Number(e.target.value) })}>
          <option value={0}>역할 선택</option>
          {roles?.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
        </select>
        <button type="submit">사용자 등록</button>
      </form>
      {createError && <p className="form-error">{createError}</p>}

      {isLoading && <p>불러오는 중...</p>}
      {error && <p className="form-error">{error}</p>}
      {data && (
        <table className="data-table">
          <thead>
            <tr><th>ID</th><th>아이디</th><th>이름</th><th>역할</th><th>상태</th><th></th></tr>
          </thead>
          <tbody>
            {data.map((u) => (
              <tr key={u.id}>
                <td>{u.id}</td>
                <td>{u.username}</td>
                <td>{u.name}</td>
                <td>
                  <select value={u.role_id} onChange={(e) => changeRole(u, Number(e.target.value))}>
                    {roles?.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
                  </select>
                </td>
                <td>{u.is_active ? '활성' : '비활성'}</td>
                <td>
                  <button type="button" onClick={() => toggleActive(u)}>{u.is_active ? '비활성화' : '활성화'}</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

function RolesTab() {
  const { data, error, isLoading, reload } = useApiData<SettingsRole[]>(() => api.get('/api/settings/roles'), [])
  const [form, setForm] = useState({ name: '', description: '' })
  const [createError, setCreateError] = useState<string | null>(null)

  const handleCreate = async (e: FormEvent) => {
    e.preventDefault()
    if (!form.name) {
      setCreateError('역할 이름을 입력하세요.')
      return
    }
    setCreateError(null)
    try {
      await api.post('/api/settings/roles', form)
      setForm({ name: '', description: '' })
      reload()
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.message : '역할 등록 중 오류가 발생했습니다.')
    }
  }

  return (
    <div>
      <form className="inline-form" onSubmit={handleCreate}>
        <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="역할 이름" />
        <input
          value={form.description}
          onChange={(e) => setForm({ ...form, description: e.target.value })}
          placeholder="설명"
        />
        <button type="submit">역할 등록</button>
      </form>
      {createError && <p className="form-error">{createError}</p>}

      {isLoading && <p>불러오는 중...</p>}
      {error && <p className="form-error">{error}</p>}
      {data && (
        <table className="data-table">
          <thead><tr><th>ID</th><th>이름</th><th>설명</th></tr></thead>
          <tbody>
            {data.map((r) => (
              <tr key={r.id}><td>{r.id}</td><td>{r.name}</td><td>{r.description ?? '-'}</td></tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

function PermissionsTab() {
  const { data: roles } = useApiData<SettingsRole[]>(() => api.get('/api/settings/roles'), [])
  const { data: permissions } = useApiData<SettingsPermission[]>(() => api.get('/api/settings/permissions'), [])
  const [roleId, setRoleId] = useState<number>(0)
  const { data: rolePerms, reload } = useApiData<RolePermissions | null>(
    () => (roleId ? api.get(`/api/settings/roles/${roleId}/permissions`) : Promise.resolve(null)),
    [roleId],
  )
  const [saveError, setSaveError] = useState<string | null>(null)
  const [checked, setChecked] = useState<Set<string>>(new Set())

  // 서버에서 새로 받아온 매핑으로 체크박스 상태를 동기화한다.
  const currentCodes = rolePerms?.permission_codes.join(',') ?? ''
  const [syncedFor, setSyncedFor] = useState('')
  if (currentCodes !== syncedFor) {
    setSyncedFor(currentCodes)
    setChecked(new Set(rolePerms?.permission_codes ?? []))
  }

  function toggle(code: string) {
    setChecked((prev) => {
      const next = new Set(prev)
      if (next.has(code)) next.delete(code)
      else next.add(code)
      return next
    })
  }

  async function handleSave() {
    setSaveError(null)
    try {
      await api.patch(`/api/settings/roles/${roleId}/permissions`, { permission_codes: [...checked] })
      reload()
    } catch (err) {
      setSaveError(err instanceof ApiError ? err.message : '권한 저장 중 오류가 발생했습니다.')
    }
  }

  return (
    <div>
      <div className="filter-bar">
        <select value={roleId} onChange={(e) => setRoleId(Number(e.target.value))}>
          <option value={0}>역할을 선택하세요</option>
          {roles?.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
        </select>
        {roleId > 0 && <button type="button" onClick={handleSave}>권한 저장</button>}
      </div>
      {saveError && <p className="form-error">{saveError}</p>}
      {roleId > 0 && permissions && (
        <table className="data-table">
          <thead><tr><th>메뉴</th><th>권한 코드</th><th>이름</th><th>부여</th></tr></thead>
          <tbody>
            {permissions.map((p) => (
              <tr key={p.id}>
                <td>{p.menu_group ?? '-'}</td>
                <td>{p.code}</td>
                <td>{p.name}</td>
                <td>
                  <input type="checkbox" checked={checked.has(p.code)} onChange={() => toggle(p.code)} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

// 커넥터별 _get_credentials()가 실제로 조회하는 key_name과 정확히 일치해야 한다 -
// 자유 입력을 허용하면 오탈자/임의 명칭으로 등록해도 저장 자체는 성공해버려서, 실제로는
// 계속 연결정보 오류로 실패하는데도 사용자가 이를 알아챌 방법이 없다(실제로 이 문제가
// 발생했었음). 그래서 플랫폼(connector_class)별로 필요한 키만 드롭다운으로 못박는다.
// required=필수(전부 등록돼야 실제 호출), optional=선택(없어도 실제 호출은 됨).
//
// 이 맵에는 integrations/malls.SUPPORTED_CONNECTORS(실 API 연동이 검증된 채널)에 있는
// 커넥터만 등록한다 - ESM/11번가/카카오쇼핑은 팩토리(get_mall_connector)가 인스턴스화
// 자체를 차단하고(MarketplaceCapabilityUnsupportedError) 커넥터 코드에도 credential을
// 읽는 로직이 없으므로, 여기에 항목을 만들면 실제로는 아무 효과가 없는 credential을
// 등록할 수 있는 것처럼 보이게 된다(지원되는 것처럼 오인시키지 않는다).
const CREDENTIAL_KEYS_BY_CONNECTOR: Record<string, { required: string[]; optional: string[] }> = {
  NaverSmartstoreConnector: { required: ['client_id', 'client_secret'], optional: ['seller_id'] },
  CoupangConnector: { required: ['access_key', 'secret_key', 'vendor_id'], optional: [] },
}

function CredentialsTab() {
  // capability-matrix는 비활성 플랫폼(11번가 등)도 포함한다 - 선택했을 때 아래
  // "현재 실제 API 연동을 지원하지 않는 플랫폼입니다" 안내가 나오게 하려면, 애초에
  // 목록에서 골라볼 수 있어야 한다(/api/platforms는 활성 플랫폼만 반환한다).
  const { data: platforms } = useApiData<PlatformCapability[]>(() => api.get('/api/platforms/capability-matrix'), [])
  const [ownerId, setOwnerId] = useState<number>(0)
  const { data, error, isLoading, reload } = useApiData<ApiCredentialMasked[]>(
    () => (ownerId ? api.get(`/api/settings/api-credentials?owner_type=PLATFORM&owner_id=${ownerId}`) : Promise.resolve([])),
    [ownerId],
  )
  const selectedPlatform = platforms?.find((p) => p.id === ownerId)
  const keySpec = selectedPlatform ? CREDENTIAL_KEYS_BY_CONNECTOR[selectedPlatform.connector_class] : undefined
  const allKeyNames = keySpec ? [...keySpec.required, ...keySpec.optional] : []
  const [form, setForm] = useState({ key_name: '', plain_value: '' })
  const [formError, setFormError] = useState<string | null>(null)

  // 플랫폼을 바꾸면 그 플랫폼의 첫 번째 키로 선택을 초기화한다.
  useEffect(() => {
    setForm({ key_name: allKeyNames[0] ?? '', plain_value: '' })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ownerId])

  const handleSave = async (e: FormEvent) => {
    e.preventDefault()
    if (!ownerId || !form.key_name || !form.plain_value) {
      setFormError('플랫폼/키 이름/값을 모두 입력하세요.')
      return
    }
    setFormError(null)
    try {
      const payload: ApiCredentialUpsert = { owner_type: 'PLATFORM', owner_id: ownerId, key_name: form.key_name, plain_value: form.plain_value }
      await api.post('/api/settings/api-credentials', payload)
      // 값만 비우고 방금 선택한 키 이름은 유지한다(여러 키를 연달아 등록하기 쉽게).
      setForm({ key_name: form.key_name, plain_value: '' })
      reload()
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : 'API Credential 저장 중 오류가 발생했습니다.')
    }
  }

  async function handleDelete(id: number) {
    await api.del(`/api/settings/api-credentials/${id}`)
    reload()
  }

  const registeredKeyNames = new Set(data?.map((c) => c.key_name) ?? [])
  const missingRequiredKeys = (keySpec?.required ?? []).filter((k) => !registeredKeyNames.has(k))

  return (
    <div>
      <p className="hint-text">
        API 키/시크릿은 서버에 암호화(Fernet)되어 저장되며, 조회 시 마지막 4자리만 표시됩니다. 플랫폼을 선택하면
        그 플랫폼의 커넥터가 요구하는 키 목록이 나타납니다. 필수 연결정보가 모두 등록되어야 실제 API를 호출할 수
        있습니다. 누락된 정보가 있으면 동기화를 시작하지 않고 연결정보 오류를 반환합니다.
      </p>
      <p className="hint-text">쿠팡 Open API 호출 IP 허용등록이 별도로 필요합니다.</p>
      <form className="inline-form" onSubmit={handleSave}>
        <select value={ownerId} onChange={(e) => setOwnerId(Number(e.target.value))}>
          <option value={0}>플랫폼 선택</option>
          {platforms?.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        <select
          value={form.key_name}
          onChange={(e) => setForm({ ...form, key_name: e.target.value })}
          disabled={allKeyNames.length === 0}
        >
          {allKeyNames.length === 0 && <option value="">플랫폼을 먼저 선택하세요</option>}
          {allKeyNames.map((k) => (
            <option key={k} value={k}>
              {k}
              {keySpec?.optional.includes(k) ? ' (선택)' : ''}
            </option>
          ))}
        </select>
        <input
          type="password"
          autoComplete="new-password"
          value={form.plain_value}
          onChange={(e) => setForm({ ...form, plain_value: e.target.value })}
          placeholder="값"
          disabled={allKeyNames.length === 0}
        />
        <button type="submit" disabled={allKeyNames.length === 0}>저장</button>
      </form>
      {formError && <p className="form-error">{formError}</p>}
      {ownerId > 0 && !keySpec && (
        <p className="form-error">
          현재 실제 API 연동을 지원하지 않는 플랫폼입니다. 인증정보를 저장할 수 없습니다.
        </p>
      )}
      {ownerId > 0 && keySpec && missingRequiredKeys.length > 0 && (
        <p className="form-error">
          아직 등록되지 않은 필수 연결정보: {missingRequiredKeys.join(', ')} — 필요한 정보를 모두 등록하기 전에는
          동기화를 시작할 수 없습니다.
        </p>
      )}

      {isLoading && <p>불러오는 중...</p>}
      {error && <p className="form-error">{error}</p>}
      {ownerId > 0 && data && (
        <table className="data-table">
          <thead><tr><th>키 이름</th><th>값(마스킹)</th><th>수정일시</th><th></th></tr></thead>
          <tbody>
            {data.map((c) => (
              <tr key={c.id}>
                <td>{c.key_name}</td>
                <td>{c.masked_value}</td>
                <td>{new Date(c.updated_at).toLocaleString()}</td>
                <td><button type="button" onClick={() => handleDelete(c.id)}>삭제</button></td>
              </tr>
            ))}
            {data.length === 0 && <tr><td colSpan={4}>등록된 API Credential이 없습니다.</td></tr>}
          </tbody>
        </table>
      )}
    </div>
  )
}

function SystemSettingsTab() {
  const { data, error, isLoading, reload } = useApiData<SystemSetting[]>(() => api.get('/api/settings/system'), [])
  const [form, setForm] = useState({ category: 'BACKUP', key: '', value: '' })
  const [formError, setFormError] = useState<string | null>(null)

  const handleSave = async (e: FormEvent) => {
    e.preventDefault()
    if (!form.key) {
      setFormError('키를 입력하세요.')
      return
    }
    setFormError(null)
    try {
      await api.patch('/api/settings/system', form)
      setForm({ ...form, key: '', value: '' })
      reload()
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : '시스템 설정 저장 중 오류가 발생했습니다.')
    }
  }

  return (
    <div>
      <form className="inline-form" onSubmit={handleSave}>
        <select value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })}>
          <option value="BACKUP">BACKUP</option>
          <option value="REPORT">REPORT</option>
          <option value="SYSTEM">SYSTEM</option>
        </select>
        <input value={form.key} onChange={(e) => setForm({ ...form, key: e.target.value })} placeholder="키" />
        <input value={form.value} onChange={(e) => setForm({ ...form, value: e.target.value })} placeholder="값" />
        <button type="submit">저장</button>
      </form>
      {formError && <p className="form-error">{formError}</p>}

      {isLoading && <p>불러오는 중...</p>}
      {error && <p className="form-error">{error}</p>}
      {data && (
        <table className="data-table">
          <thead><tr><th>카테고리</th><th>키</th><th>값</th><th>수정일시</th></tr></thead>
          <tbody>
            {data.map((s) => (
              <tr key={s.id}><td>{s.category}</td><td>{s.key}</td><td>{s.value ?? '-'}</td><td>{new Date(s.updated_at).toLocaleString()}</td></tr>
            ))}
            {data.length === 0 && <tr><td colSpan={4}>등록된 설정이 없습니다.</td></tr>}
          </tbody>
        </table>
      )}
    </div>
  )
}

type SettingsTab = 'users' | 'roles' | 'permissions' | 'credentials' | 'channels' | 'system'

export function SettingsPage() {
  const [tab, setTab] = useState<SettingsTab>('users')

  const TABS: Array<{ key: SettingsTab; label: string }> = [
    { key: 'users', label: '사용자 관리' },
    { key: 'roles', label: '역할 관리' },
    { key: 'permissions', label: '권한 관리' },
    { key: 'credentials', label: 'API Credential 관리' },
    { key: 'channels', label: '채널 연동 현황' },
    { key: 'system', label: '시스템 설정' },
  ]

  return (
    <div>
      <h2>설정</h2>
      <div className="tabs">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            className={'tab-button' + (tab === t.key ? ' active' : '')}
            onClick={() => setTab(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>
      {tab === 'users' && <UsersTab />}
      {tab === 'roles' && <RolesTab />}
      {tab === 'permissions' && <PermissionsTab />}
      {tab === 'credentials' && <CredentialsTab />}
      {tab === 'channels' && <ChannelsTab />}
      {tab === 'system' && <SystemSettingsTab />}
    </div>
  )
}
