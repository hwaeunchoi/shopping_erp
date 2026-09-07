import { Fragment, useEffect, useRef, useState, type ChangeEvent, type FormEvent, type KeyboardEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import { useApiData } from '../api/useApiData'
import { useRecentView } from '../api/useRecentView'
import { FavoriteStar } from '../components/FavoriteStar'
import type {
  OptionRegistrationStatus,
  ProductCostHistory,
  ProductDetail,
  ProductImage,
  ProductOption,
  ProductOptionCreate,
  ProductOptionDetail,
  ProductOptionGroupDraft,
  ProductOptionGroupItem,
  ProductOptionUpdate,
  ProductPlatformMap,
  ProductPublishDraft,
  ProductSyncCommand,
  ProductSyncExternalCommand,
  RegistrationStatus,
  SaleStatusValue,
} from '../api/types'

const OPTIONS_TABLE_COLUMNS = 11
const OPTION_NAME_MAX_LENGTH = 255

function ProductInfoEdit({ product, onSaved }: { product: ProductDetail; onSaved: () => void }) {
  const navigate = useNavigate()
  const [form, setForm] = useState({
    name: product.name,
    category: product.category ?? '',
    brand: product.brand ?? '',
    manufacturer: product.manufacturer ?? '',
    base_price: product.base_price ?? undefined,
    status: product.status,
  })
  const [error, setError] = useState<string | null>(null)
  const [isSaving, setIsSaving] = useState(false)

  const handleSave = async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    setIsSaving(true)
    try {
      await api.patch(`/api/products/${product.id}`, {
        name: form.name,
        category: form.category || null,
        brand: form.brand || null,
        manufacturer: form.manufacturer || null,
        base_price: form.base_price ?? null,
        status: form.status,
      })
      onSaved()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '상품 수정 중 오류가 발생했습니다.')
    } finally {
      setIsSaving(false)
    }
  }

  const handleDelete = async () => {
    if (!window.confirm(`'${product.name}' 상품을 삭제하시겠습니까?`)) {
      return
    }
    setError(null)
    try {
      await api.del(`/api/products/${product.id}`)
      navigate('/products')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '상품 삭제 중 오류가 발생했습니다.')
    }
  }

  const handleDuplicate = async () => {
    setError(null)
    try {
      const duplicated = await api.post<{ id: number }>(`/api/products/${product.id}/duplicate`, {})
      navigate(`/products/${duplicated.id}`)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '상품 복제 중 오류가 발생했습니다.')
    }
  }

  return (
    <form className="inline-form" onSubmit={handleSave}>
      <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="상품명" />
      <input
        value={form.category}
        onChange={(e) => setForm({ ...form, category: e.target.value })}
        placeholder="카테고리"
      />
      <input value={form.brand} onChange={(e) => setForm({ ...form, brand: e.target.value })} placeholder="브랜드" />
      <input
        value={form.manufacturer}
        onChange={(e) => setForm({ ...form, manufacturer: e.target.value })}
        placeholder="제조사"
      />
      <input
        type="number"
        value={form.base_price ?? ''}
        onChange={(e) => setForm({ ...form, base_price: e.target.value ? Number(e.target.value) : undefined })}
        placeholder="판매가"
        min={0}
      />
      <select value={form.status} onChange={(e) => setForm({ ...form, status: e.target.value })}>
        <option value="ACTIVE">판매중</option>
        <option value="DISCONTINUED">단종</option>
      </select>
      <button type="submit" disabled={isSaving}>{isSaving ? '저장 중...' : '상품정보 저장'}</button>
      <button type="button" onClick={handleDuplicate}>상품 복제</button>
      <button type="button" onClick={handleDelete}>상품 삭제</button>
      {error && <span className="form-error">{error}</span>}
    </form>
  )
}

function deriveOptionForm(option: ProductOption) {
  return {
    option_name: option.option_name ?? '',
    color: option.color ?? '',
    size: option.size ?? '',
    barcode: option.barcode ?? '',
    unit_cost_price: option.unit_cost_price ?? undefined,
  }
}

// 저장 시 보낼 문자열을 정규화한다 - 앞뒤 공백을 지우고, 그 결과가 빈 문자열이면
// 명시적 null로 보낸다(서버가 "생략"과 "명시적 null"을 구분하므로, 지운다는
// 의도가 실제로 반영되게 하려면 빈 값도 null로 보내야 한다).
function normalizeOptionText(value: string): string | null {
  const trimmed = value.trim()
  return trimmed === '' ? null : trimmed
}

// 옵션(SKU) 한 행 - 옵션명/색상/사이즈/바코드/단가를 셀에서 바로 편집하고 행마다
// 저장한다(예전엔 '수정'을 눌러 아래에 폼을 펼쳐야 해서 번거로웠음).
//
// prop 동기화 정책: 사용자가 편집 중(dirty)인 동안에는 option prop이 바뀌어도
// (다른 행 순서변경 등으로 인한 목록 재조회 포함) 입력값을 덮어쓰지 않는다.
// dirty가 아닐 때만 prop 값으로 폼을 다시 맞춘다. 저장 성공 시에는 서버가 실제로
// 반영한 응답값을 폼에 직접 대입하므로, 뒤이어 도착하는 option prop 갱신은 이미
// 같은 값이라 재동기화되어도 화면이 다시 바뀌지 않는다(낡은 props로 덮이지 않음).
function OptionRow({
  option,
  index,
  total,
  onReload,
  onMove,
  onToggleActive,
  onDelete,
  onManage,
  isManaging,
}: {
  option: ProductOptionDetail
  index: number
  total: number
  onReload: () => void
  onMove: (index: number, dir: -1 | 1) => void
  onToggleActive: (o: ProductOptionDetail) => void
  onDelete: (o: ProductOptionDetail) => void
  onManage: (id: number) => void
  isManaging: boolean
}) {
  const [form, setForm] = useState(() => deriveOptionForm(option))
  const [error, setError] = useState<string | null>(null)
  const [isSaving, setIsSaving] = useState(false)
  const [savedAt, setSavedAt] = useState(false)
  const savingRef = useRef(false)

  const dirty =
    form.option_name !== (option.option_name ?? '') ||
    form.color !== (option.color ?? '') ||
    form.size !== (option.size ?? '') ||
    form.barcode !== (option.barcode ?? '') ||
    (form.unit_cost_price ?? undefined) !== (option.unit_cost_price ?? undefined)

  useEffect(() => {
    if (!dirty) {
      setForm(deriveOptionForm(option))
    }
    // dirty는 매 렌더 재계산되는 파생값이라 의도적으로 deps에서 제외한다 -
    // option이 바뀔 때만 재동기화 여부를 판단하면 된다.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [option])

  const priceInvalid = form.unit_cost_price !== undefined && form.unit_cost_price < 0

  const handlePriceChange = (e: ChangeEvent<HTMLInputElement>) => {
    const raw = e.target.value
    const parsed = raw ? Number(raw) : undefined
    // NaN이 폼/페이로드에 들어가지 않도록 방어한다(예: "-"만 입력된 중간 상태).
    setForm({ ...form, unit_cost_price: parsed !== undefined && Number.isFinite(parsed) ? parsed : undefined })
  }

  const handleSave = async () => {
    // useRef 기반 잠금 - isSaving(state)만으로는 같은 렌더 프레임 안에서 Enter를
    // 연타할 때 중복 요청을 완전히 막지 못할 수 있어, 동기적으로 즉시 반영되는
    // ref로 이중 방어한다. 버튼 클릭과 Enter 저장 모두 이 handleSave를 거친다.
    if (savingRef.current || priceInvalid) return
    savingRef.current = true
    setError(null)
    setIsSaving(true)
    try {
      const payload: ProductOptionUpdate = {
        option_name: normalizeOptionText(form.option_name),
        color: normalizeOptionText(form.color),
        size: normalizeOptionText(form.size),
        barcode: normalizeOptionText(form.barcode),
        unit_cost_price: form.unit_cost_price ?? null,
      }
      const updated = await api.patch<ProductOption>(`/api/products/options/${option.id}`, payload)
      setForm(deriveOptionForm(updated))  // 서버가 실제로 반영한 값으로만 재동기화한다
      setSavedAt(true)
      setTimeout(() => setSavedAt(false), 1500)
      onReload()
    } catch (err) {
      // 실패 시 사용자가 입력한 값(form)은 그대로 유지한다 - 재입력할 필요 없음.
      setError(err instanceof ApiError ? err.message : '옵션 수정 중 오류가 발생했습니다.')
    } finally {
      savingRef.current = false
      setIsSaving(false)
    }
  }

  // Enter 키로도 저장되게 한다(편집 편의) - 클릭과 동일한 handleSave를 타므로
  // savingRef 가드도 그대로 적용된다.
  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key === 'Enter') handleSave()
  }

  return (
    <tr>
      <td>
        <button type="button" disabled={index === 0} onClick={() => onMove(index, -1)}>▲</button>
        <button type="button" disabled={index === total - 1} onClick={() => onMove(index, 1)}>▼</button>
      </td>
      <td>{option.sku_code}</td>
      <td>
        <input
          className="cell-input"
          value={form.option_name}
          onChange={(e) => setForm({ ...form, option_name: e.target.value })}
          onKeyDown={onKeyDown}
          placeholder="옵션명"
          maxLength={OPTION_NAME_MAX_LENGTH}
          aria-label={`${option.sku_code} 옵션명`}
        />
      </td>
      <td>
        <input
          className="cell-input sm"
          value={form.color}
          onChange={(e) => setForm({ ...form, color: e.target.value })}
          onKeyDown={onKeyDown}
          placeholder="색상"
          aria-label={`${option.sku_code} 색상`}
        />
      </td>
      <td>
        <input
          className="cell-input sm"
          value={form.size}
          onChange={(e) => setForm({ ...form, size: e.target.value })}
          onKeyDown={onKeyDown}
          placeholder="사이즈"
          aria-label={`${option.sku_code} 사이즈`}
        />
      </td>
      <td>
        <input
          className="cell-input"
          value={form.barcode}
          onChange={(e) => setForm({ ...form, barcode: e.target.value })}
          onKeyDown={onKeyDown}
          placeholder="바코드"
          aria-label={`${option.sku_code} 바코드`}
        />
      </td>
      <td>
        <input
          className="cell-input sm"
          type="number"
          min={0}
          value={form.unit_cost_price ?? ''}
          onChange={handlePriceChange}
          onKeyDown={onKeyDown}
          placeholder="단가"
          aria-label={`${option.sku_code} 단가(매입원가)`}
        />
        {priceInvalid && <div className="form-error">단가는 0 이상이어야 합니다</div>}
      </td>
      <td><span className="status-badge">{option.is_active ? '판매중' : '미판매'}</span></td>
      <td>
        <button
          type="button"
          className={`option-save-btn${dirty ? ' is-dirty' : ''}`}
          disabled={isSaving || !dirty || priceInvalid}
          onClick={handleSave}
        >
          {isSaving ? '저장 중...' : savedAt ? '저장됨 ✓' : '저장'}
        </button>
      </td>
      <td><button type="button" onClick={() => onToggleActive(option)}>{option.is_active ? '미판매로' : '판매중으로'}</button></td>
      <td>
        <button type="button" onClick={() => onDelete(option)}>삭제</button>
        <button type="button" onClick={() => onManage(option.id)}>{isManaging ? '닫기' : '관리'}</button>
        {error && <span className="form-error">{error}</span>}
      </td>
    </tr>
  )
}

function PlatformMapEditRow({ mapping, onSaved, onCancel }: { mapping: ProductPlatformMap; onSaved: () => void; onCancel: () => void }) {
  const [form, setForm] = useState({
    display_name: mapping.display_name ?? '',
    seller_product_code: mapping.seller_product_code ?? '',
    platform_product_id: mapping.platform_product_id ?? '',
  })
  const [error, setError] = useState<string | null>(null)

  const handleSave = async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    try {
      await api.patch(`/api/products/platform-map/${mapping.id}`, {
        display_name: form.display_name || null,
        seller_product_code: form.seller_product_code || null,
        platform_product_id: form.platform_product_id || null,
      })
      onSaved()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '매핑 수정 중 오류가 발생했습니다.')
    }
  }

  return (
    <tr>
      <td colSpan={8}>
        <form className="inline-form" onSubmit={handleSave}>
          <input
            value={form.display_name}
            onChange={(e) => setForm({ ...form, display_name: e.target.value })}
            placeholder="쇼핑몰 노출상품명"
          />
          <input
            value={form.seller_product_code}
            onChange={(e) => setForm({ ...form, seller_product_code: e.target.value })}
            placeholder="판매자상품코드"
          />
          <input
            value={form.platform_product_id}
            onChange={(e) => setForm({ ...form, platform_product_id: e.target.value })}
            placeholder="플랫폼 상품번호"
          />
          <button type="submit">저장</button>
          <button type="button" onClick={onCancel}>취소</button>
          {error && <span className="form-error">{error}</span>}
        </form>
      </td>
    </tr>
  )
}

const SYNC_STATUS_LABELS: Record<string, string> = {
  PENDING: '접수됨(전송 대기)',
  RUNNING: '전송 중',
  SUCCESS: '성공',
  FAILED: '실패',
  RETRY_WAIT: '재시도 대기',
  UNKNOWN: '확인 필요(운영자 확인 대상)',
  CANCELLED: '취소됨(더 최신 요청으로 대체)',
}

// 상용 ERP 확장(3단계, 첫 묶음) - 기존 채널 상품(옵션) 하나의 재고/판매상태를
// 채널에 전송하는 최소한의 UI. 이 화면은 "채널의 현재 값을 읽어 보여주는" 기능이
// 없으므로(별도 조회 API 없음) 목표값 입력·전송·명령 상태 폴링만 제공하고,
// 채널의 현재 값은 항상 "확인되지 않음"으로 고정 표기한다(0이나 최신값으로
// 추정해 보여주지 않는다).
function ProductSyncControls({ mapping, onClose }: { mapping: ProductPlatformMap; onClose: () => void }) {
  // 부모가 key={mapping.id}로 매핑마다 새로 마운트한다 - 다른 매핑으로 전환하면
  // 이 아래 모든 useState(입력값·명령 상태)가 초기화되어 이전 대상의 값이
  // 섞이지 않는다. 반대로 같은 매핑을 가리키는 채로 목록만 새로고침되면
  // (reloadMaps) mapping.id는 그대로이므로 key가 바뀌지 않아 이 컴포넌트는
  // 리마운트되지 않고 입력 중이던 값도 그대로 유지된다.
  const headingRef = useRef<HTMLHeadingElement>(null)
  useEffect(() => {
    headingRef.current?.focus()
  }, [])

  const [quantity, setQuantity] = useState('')
  const [saleStatus, setSaleStatus] = useState<SaleStatusValue>('ON_SALE')
  const [infoName, setInfoName] = useState('')
  const [infoPrice, setInfoPrice] = useState('')
  const [infoDescription, setInfoDescription] = useState('')
  const [command, setCommand] = useState<ProductSyncExternalCommand | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [isSubmittingQty, setIsSubmittingQty] = useState(false)
  const [isSubmittingStatus, setIsSubmittingStatus] = useState(false)
  const [isSubmittingInfo, setIsSubmittingInfo] = useState(false)

  const pollCommand = async (commandId: number) => {
    try {
      const result = await api.get<ProductSyncExternalCommand>(`/api/products/sync-commands/${commandId}`)
      setCommand(result)
    } catch {
      // 폴링 실패는 조용히 무시한다 - 다음 수동 새로고침으로 다시 시도할 수 있다.
    }
  }

  const handleSyncInventory = async (e: FormEvent) => {
    e.preventDefault()
    if (quantity === '') {
      setError('전송할 재고 수량을 입력하세요.')
      return
    }
    setError(null)
    setIsSubmittingQty(true)
    try {
      const result = await api.post<ProductSyncCommand>(`/api/products/platform-map/${mapping.id}/sync-inventory`, {
        target_quantity: Number(quantity),
      })
      await pollCommand(result.command_id)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '재고 전송 요청 중 오류가 발생했습니다.')
    } finally {
      setIsSubmittingQty(false)
    }
  }

  const handleSyncSaleStatus = async () => {
    setError(null)
    setIsSubmittingStatus(true)
    try {
      const result = await api.post<ProductSyncCommand>(
        `/api/products/platform-map/${mapping.id}/sync-sale-status`,
        { target_status: saleStatus },
      )
      await pollCommand(result.command_id)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '판매상태 전송 요청 중 오류가 발생했습니다.')
    } finally {
      setIsSubmittingStatus(false)
    }
  }

  const handleUpdateInfo = async (e: FormEvent) => {
    e.preventDefault()
    if (!infoName && !infoPrice && !infoDescription) {
      setError('수정할 항목(상품명/판매가/상세설명)을 하나 이상 입력하세요.')
      return
    }
    if (
      !window.confirm(
        '입력한 항목만 채널에 전송됩니다(빈 항목은 채널의 현재값을 그대로 유지). 전송하시겠습니까?',
      )
    ) {
      return
    }
    setError(null)
    setIsSubmittingInfo(true)
    try {
      const result = await api.post<ProductSyncCommand>(`/api/products/platform-map/${mapping.id}/update-info`, {
        name: infoName || null,
        sale_price: infoPrice ? Number(infoPrice) : null,
        description: infoDescription || null,
      })
      await pollCommand(result.command_id)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '정보 수정 요청 중 오류가 발생했습니다.')
    } finally {
      setIsSubmittingInfo(false)
    }
  }

  return (
    <div className="option-manage-panel">
      <div className="option-manage-panel-header">
        <h3 ref={headingRef} tabIndex={-1}>
          관리 중: 매핑 #{mapping.id} · 플랫폼 {mapping.platform_id} · 옵션번호 {mapping.platform_option_id}
        </h3>
        <button type="button" onClick={onClose}>닫기</button>
      </div>
      {mapping.sibling_mapping_ids.length > 0 && (
        <p className="hint-text" style={{ color: '#b45309' }}>
          ⚠️ 이 매핑은 같은 원상품(origin product)을 매핑 #{mapping.sibling_mapping_ids.join(', #')}과(와)
          공유합니다 - 판매상태 변경은 해당 매핑들에도 함께 반영됩니다.
        </p>
      )}
      <p className="hint-text">채널 현재 값: 확인되지 않음(이 화면은 목표값 전송 전용)</p>
      <form className="inline-form" onSubmit={handleSyncInventory} style={{ marginBottom: 4 }}>
        <input
          type="number"
          value={quantity}
          onChange={(e) => setQuantity(e.target.value)}
          placeholder="목표 재고수량"
          min={0}
        />
        <button type="submit" disabled={isSubmittingQty}>
          {isSubmittingQty ? '전송 중...' : '재고 전송'}
        </button>
      </form>
      <div className="inline-form" style={{ marginBottom: 4 }}>
        <select value={saleStatus} onChange={(e) => setSaleStatus(e.target.value as SaleStatusValue)}>
          <option value="ON_SALE">판매중으로</option>
          <option value="SUSPENDED">판매중지로</option>
        </select>
        <button type="button" onClick={handleSyncSaleStatus} disabled={isSubmittingStatus}>
          {isSubmittingStatus ? '전송 중...' : '판매상태 전송'}
        </button>
      </div>
      <form className="inline-form" onSubmit={handleUpdateInfo} style={{ marginBottom: 4, flexWrap: 'wrap' }}>
        <input value={infoName} onChange={(e) => setInfoName(e.target.value)} placeholder="새 상품명(선택)" />
        <input
          type="number"
          value={infoPrice}
          onChange={(e) => setInfoPrice(e.target.value)}
          placeholder="새 판매가(선택)"
          min={0}
        />
        <input
          value={infoDescription}
          onChange={(e) => setInfoDescription(e.target.value)}
          placeholder="새 상세설명(선택)"
          className="publish-field-sm"
        />
        <button type="submit" disabled={isSubmittingInfo}>
          {isSubmittingInfo ? '전송 중...' : '정보 수정 전송'}
        </button>
      </form>
      {error && <p className="form-error">{error}</p>}
      {command && (
        <p>
          명령 #{command.id}: <span className="status-badge">{SYNC_STATUS_LABELS[command.status] ?? command.status}</span>
          {command.error_code && ` (사유: ${command.error_code})`}
          {' '}
          <button type="button" onClick={() => pollCommand(command.id)}>상태 새로고침</button>
        </p>
      )}
    </div>
  )
}

// 상용 ERP 확장(3단계, 두 번째 묶음) - 옵션 조합 없는 단순 상품의 신규 등록 초안
// 입력·저장·전송 전 확인·등록 요청·명령 상태 조회를 한 화면에서 처리한다. 공통
// 핵심 항목(상품명/판매가/상세설명/카테고리/이미지/재고)은 각각 입력란을 두고,
// 채널마다 크게 다른 배송/반품/원산지/인증/상품정보제공고시 등은 "채널별 세부
// 계약 정보(JSON)"로 받는다 - 실제 필수 여부 검증은 전송 시점에 커넥터가 공식
// 계약 기준으로 한다(이 화면은 핵심 항목의 누락만 미리 표시한다).
function ProductPublishControls({ optionId, platformId }: { optionId: number; platformId: number }) {
  const [draft, setDraft] = useState<ProductPublishDraft | null>(null)
  const [name, setName] = useState('')
  const [salePrice, setSalePrice] = useState('')
  const [descriptionHtml, setDescriptionHtml] = useState('')
  const [categoryCode, setCategoryCode] = useState('')
  const [imageUrls, setImageUrls] = useState('')
  const [stockQuantity, setStockQuantity] = useState('')
  const [channelFields, setChannelFields] = useState('{}')
  const [command, setCommand] = useState<ProductSyncExternalCommand | null>(null)
  const [registrationStatus, setRegistrationStatus] = useState<RegistrationStatus | null>(null)
  const [confirmOptionId, setConfirmOptionId] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [isSavingDraft, setIsSavingDraft] = useState(false)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [isConfirmingEtcNotice, setIsConfirmingEtcNotice] = useState(false)

  useEffect(() => {
    let cancelled = false
    api
      .get<ProductPublishDraft>(`/api/products/options/${optionId}/publish-draft/${platformId}`)
      .then((d) => {
        if (cancelled) return
        setDraft(d)
        setName(d.name ?? '')
        setSalePrice(d.sale_price?.toString() ?? '')
        setDescriptionHtml(d.description_html ?? '')
        setCategoryCode(d.category_code ?? '')
        setImageUrls(d.image_urls.join(', '))
        setStockQuantity(d.stock_quantity?.toString() ?? '')
        setChannelFields(JSON.stringify(d.channel_fields, null, 2))
      })
      .catch(() => {
        // 초안이 아직 없으면(404) 빈 폼을 그대로 둔다 - 오류로 취급하지 않는다.
      })
    return () => {
      cancelled = true
    }
  }, [optionId, platformId])

  const missingCoreFields = [
    !name && '상품명',
    !salePrice && '판매가',
    !descriptionHtml && '상세설명',
    !categoryCode && '카테고리 코드',
    !imageUrls.trim() && '이미지',
    !stockQuantity && '재고수량',
  ].filter((v): v is string => Boolean(v))

  const pollCommand = async (commandId: number) => {
    try {
      setCommand(await api.get<ProductSyncExternalCommand>(`/api/products/sync-commands/${commandId}`))
    } catch {
      // 폴링 실패는 조용히 무시한다.
    }
  }

  const handleSaveDraft = async (e: FormEvent) => {
    e.preventDefault()
    let parsedChannelFields: Record<string, unknown>
    try {
      parsedChannelFields = channelFields.trim() ? JSON.parse(channelFields) : {}
    } catch {
      setError('채널별 세부 계약 정보(JSON) 형식이 올바르지 않습니다.')
      return
    }
    setError(null)
    setIsSavingDraft(true)
    try {
      const saved = await api.post<ProductPublishDraft>(`/api/products/options/${optionId}/publish-draft`, {
        platform_id: platformId,
        name: name || null,
        sale_price: salePrice ? Number(salePrice) : null,
        description_html: descriptionHtml || null,
        category_code: categoryCode || null,
        image_urls: imageUrls.trim()
          ? imageUrls
              .split(',')
              .map((s) => s.trim())
              .filter(Boolean)
          : null,
        stock_quantity: stockQuantity ? Number(stockQuantity) : null,
        channel_fields: parsedChannelFields,
      })
      setDraft(saved)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '초안 저장 중 오류가 발생했습니다.')
    } finally {
      setIsSavingDraft(false)
    }
  }

  const handleSubmit = async () => {
    if (!draft) return
    if (missingCoreFields.length > 0) {
      setError(`아직 저장되지 않은 항목이 있습니다: ${missingCoreFields.join(', ')} (먼저 초안 저장을 누르세요)`)
      return
    }
    if (
      !window.confirm(
        `'${draft.name ?? ''}' 상품을 이 초안 내용 그대로 채널에 등록 요청하시겠습니까?\n` +
          '실제 채널 호출은 잠시 후 비동기로 처리되며, 등록 요청은 취소할 수 없습니다.',
      )
    ) {
      return
    }
    setError(null)
    setIsSubmitting(true)
    try {
      const result = await api.post<ProductSyncCommand>(`/api/products/publish-drafts/${draft.id}/submit`, {})
      await pollCommand(result.command_id)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '등록 요청 중 오류가 발생했습니다.')
    } finally {
      setIsSubmitting(false)
    }
  }

  const handleCheckStatus = async () => {
    if (!draft) return
    setError(null)
    try {
      setRegistrationStatus(
        await api.get<RegistrationStatus>(`/api/products/publish-drafts/${draft.id}/registration-status`),
      )
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '심사상태 조회 중 오류가 발생했습니다.')
    }
  }

  const handleConfirmMapping = async () => {
    if (!draft || !confirmOptionId) return
    if (!window.confirm(`옵션 식별자 '${confirmOptionId}'로 매핑을 확정하시겠습니까? 확정 후에는 되돌릴 수 없습니다.`)) {
      return
    }
    setError(null)
    try {
      await api.post(`/api/products/publish-drafts/${draft.id}/confirm-mapping`, { channel_option_id: confirmOptionId })
      setDraft({ ...draft, pending_platform_product_id: null, registered_at: new Date().toISOString() })
      setRegistrationStatus(null)
      setConfirmOptionId('')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '매핑 확정 중 오류가 발생했습니다.')
    }
  }

  // 채널별 세부 계약 정보 텍스트에서 "지금 화면에 입력된" 고시유형만 읽는다(저장
  // 여부와 무관 - 저장 전에도 안내 문구가 맞게 보이도록). 파싱 실패 시 조용히
  // undefined(안내 블록을 표시하지 않음) - 이 값 자체가 서버에 전송되는 것은
  // 아니다(실제 전송값은 services.product_publish_service._draft_snapshot이
  // 서버에 저장된 확인 기록으로부터 다시 계산한다).
  let currentNoticeType: string | undefined
  try {
    const parsed = channelFields.trim() ? JSON.parse(channelFields) : {}
    const notice = parsed?.productInfoProvidedNotice
    if (notice && typeof notice === 'object') {
      currentNoticeType = (notice as Record<string, unknown>).productInfoProvidedNoticeType as string | undefined
    }
  } catch {
    currentNoticeType = undefined
  }

  const handleConfirmEtcNotice = async () => {
    if (!draft) return
    if (
      !window.confirm(
        `카테고리 코드 '${draft.category_code ?? ''}'에 네이버 ETC(기타 재화) 상품정보제공고시 양식을 ` +
          '쓰는 것이 맞는지 판매자센터에서 직접 확인했습니까?\n' +
          '이 확인은 시스템이 대신 검증한 것이 아니라 운영자 본인의 확인 기록으로 남습니다.',
      )
    ) {
      return
    }
    setError(null)
    setIsConfirmingEtcNotice(true)
    try {
      const updated = await api.post<ProductPublishDraft>(
        `/api/products/publish-drafts/${draft.id}/confirm-etc-notice`,
        {},
      )
      setDraft(updated)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'ETC 카테고리 적합성 확인 기록 중 오류가 발생했습니다.')
    } finally {
      setIsConfirmingEtcNotice(false)
    }
  }

  if (draft?.registered_at) {
    return (
      <p className="hint-text">✅ 등록 완료(매핑 생성됨) - {new Date(draft.registered_at).toLocaleString()}</p>
    )
  }

  return (
    <div>
      <form className="inline-form" onSubmit={handleSaveDraft} style={{ flexWrap: 'wrap', marginBottom: 4 }}>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="상품명" />
        <input type="number" value={salePrice} onChange={(e) => setSalePrice(e.target.value)} placeholder="판매가" min={0} />
        <input value={categoryCode} onChange={(e) => setCategoryCode(e.target.value)} placeholder="카테고리 코드" />
        <input
          type="number"
          value={stockQuantity}
          onChange={(e) => setStockQuantity(e.target.value)}
          placeholder="등록 재고수량"
          min={0}
        />
        <input
          value={imageUrls}
          onChange={(e) => setImageUrls(e.target.value)}
          placeholder="이미지 URL(쉼표 구분, 첫 번째=대표, http/https만)"
          className="publish-field-md"
        />
        <textarea
          value={descriptionHtml}
          onChange={(e) => setDescriptionHtml(e.target.value)}
          placeholder="상세설명(HTML)"
          rows={2}
          className="publish-field-md"
        />
        <textarea
          value={channelFields}
          onChange={(e) => setChannelFields(e.target.value)}
          placeholder="채널별 세부 계약 정보(JSON) - 배송/반품/원산지/인증/상품정보제공고시 등"
          rows={3}
          className="publish-field-lg"
        />
        <button type="submit" disabled={isSavingDraft}>
          {isSavingDraft ? '저장 중...' : '초안 저장'}
        </button>
      </form>
      {missingCoreFields.length > 0 && (
        <p className="hint-text" style={{ color: '#b45309' }}>입력 필요: {missingCoreFields.join(', ')}</p>
      )}
      {currentNoticeType === 'ETC' && (
        <div className="hint-text" style={{ marginBottom: 8, padding: 8, border: '1px solid var(--border)', borderRadius: 6 }}>
          <p style={{ margin: '0 0 4px' }}>
            ⚠️ 이 채널은 상품정보제공고시로 <strong>ETC(기타 재화)</strong> 양식을 사용합니다. ETC 입력 형식
            자체는 이 시스템이 지원하지만, <strong>이 카테고리에 ETC 고시가 실제로 맞는지는 공식 API로 검증할
            방법이 없어</strong> 운영자가 네이버 판매자센터에서 직접 확인해야 합니다. 아래 확인은 그 사실을
            기록할 뿐이며 "공식 적합성 검증 완료"를 의미하지 않습니다 - 카테고리 코드나 고시유형을 바꾸면
            이 확인은 자동으로 무효화됩니다.
          </p>
          {draft?.etc_notice_confirmation_valid ? (
            <p style={{ margin: '0 0 4px', color: '#15803d' }}>
              ✅ 확인됨 - 사용자 #{draft.etc_notice_confirmed_by}
              {draft.etc_notice_confirmed_at && `, ${new Date(draft.etc_notice_confirmed_at).toLocaleString()}`}
              (카테고리 {draft.etc_notice_confirmed_category_code})
            </p>
          ) : draft?.etc_notice_confirmed_at ? (
            <p style={{ margin: '0 0 4px', color: '#b45309' }}>
              ⚠️ 이전 확인(카테고리 {draft.etc_notice_confirmed_category_code})이 카테고리/고시유형 변경으로
              무효화되었습니다 - 다시 확인이 필요합니다.
            </p>
          ) : (
            <p style={{ margin: '0 0 4px', color: '#b45309' }}>❌ 아직 확인되지 않았습니다 - 확인 전에는 등록이 차단됩니다.</p>
          )}
          <button type="button" onClick={handleConfirmEtcNotice} disabled={!draft || isConfirmingEtcNotice}>
            {isConfirmingEtcNotice ? '기록 중...' : '이 카테고리에 ETC 고시가 맞음을 확인'}
          </button>
          {!draft && <span> (먼저 초안을 저장하세요)</span>}
        </div>
      )}
      {draft && (
        <div style={{ marginBottom: 4 }}>
          <button type="button" onClick={handleSubmit} disabled={isSubmitting}>
            {isSubmitting ? '요청 중...' : '채널에 등록 요청'}
          </button>
          {draft.pending_platform_product_id && (
            <>
              {' '}
              <button type="button" onClick={handleCheckStatus}>심사상태 조회</button>
              {registrationStatus && (
                <span className="hint-text">
                  {' '}상태: {registrationStatus.status_name ?? '확인되지 않음'}
                  {registrationStatus.channel_option_ids.length > 0 && (
                    <>
                      {' '}옵션 후보: {registrationStatus.channel_option_ids.join(', ')}{' '}
                      <input
                        value={confirmOptionId}
                        onChange={(e) => setConfirmOptionId(e.target.value)}
                        placeholder="확정할 옵션 식별자"
                      />
                      <button type="button" onClick={handleConfirmMapping}>매핑 확정</button>
                    </>
                  )}
                </span>
              )}
            </>
          )}
        </div>
      )}
      {error && <p className="form-error">{error}</p>}
      {command && (
        <p>
          명령 #{command.id}: <span className="status-badge">{SYNC_STATUS_LABELS[command.status] ?? command.status}</span>
          {command.error_code && ` (사유: ${command.error_code})`}
          {' '}
          <button type="button" onClick={() => pollCommand(command.id)}>상태 새로고침</button>
        </p>
      )}
    </div>
  )
}

function OptionSubDetail({
  option,
  warehousePlatformId,
  onClose,
}: {
  option: ProductOptionDetail
  warehousePlatformId: number
  onClose: () => void
}) {
  // 패널이 열릴 때(=이 컴포넌트가 새로 마운트될 때 - 부모가 key={option.id}로
  // 옵션마다 새로 마운트하므로 옵션 전환 시에도 다시 실행된다) 키보드 포커스를
  // 패널 제목으로 옮긴다 - 패널이 표 밖으로 분리되면서 "관리" 버튼과 DOM 위치가
  // 멀어졌기 때문에, 자연스러운 탭 순서만으로는 포커스가 패널로 이어지지 않는다.
  const headingRef = useRef<HTMLHeadingElement>(null)
  useEffect(() => {
    headingRef.current?.focus()
  }, [])

  const { data: maps, reload: reloadMaps } = useApiData<ProductPlatformMap[]>(
    () => api.get(`/api/products/options/${option.id}/platform-map`),
    [option.id],
  )
  const { data: history, reload: reloadHistory } = useApiData<ProductCostHistory[]>(
    () => api.get(`/api/products/options/${option.id}/cost-history`),
    [option.id],
  )

  const [platformId, setPlatformId] = useState(warehousePlatformId)
  const [platformCode, setPlatformCode] = useState('')
  const [platformProductId, setPlatformProductId] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [sellerProductCode, setSellerProductCode] = useState('')
  const [mapError, setMapError] = useState<string | null>(null)
  const [editingMapId, setEditingMapId] = useState<number | null>(null)

  // 재고/판매상태/정보수정 컨트롤(ProductSyncControls)의 관리 대상 매핑 선택 -
  // 옵션 관리 패널과 같은 이유로 표 밖의 독립 블록으로 뺀다(표 안에 있으면
  // 표의 auto-layout 폭 계산에 다시 갇힌다). manageMappingButtonRef는 패널을
  // 닫을 때 포커스를 원래 "관리" 버튼으로 되돌리기 위함이다.
  const [selectedMappingId, setSelectedMappingId] = useState<number | null>(null)
  const manageMappingButtonRef = useRef<HTMLElement | null>(null)

  const handleManageMapping = (id: number) => {
    if (selectedMappingId === id) {
      setSelectedMappingId(null)
      return
    }
    manageMappingButtonRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    setSelectedMappingId(id)
  }

  const handleCloseMappingPanel = () => {
    setSelectedMappingId(null)
    manageMappingButtonRef.current?.focus()
  }

  const [costPrice, setCostPrice] = useState('')
  const [effectiveFrom, setEffectiveFrom] = useState(new Date().toISOString().slice(0, 10))
  const [costError, setCostError] = useState<string | null>(null)

  const handleAddMap = async (e: FormEvent) => {
    e.preventDefault()
    if (!platformCode) {
      setMapError('플랫폼 옵션번호를 입력하세요.')
      return
    }
    setMapError(null)
    try {
      await api.post(`/api/products/options/${option.id}/platform-map`, {
        platform_id: platformId,
        platform_option_id: platformCode,
        platform_product_id: platformProductId || null,
        display_name: displayName || null,
        seller_product_code: sellerProductCode || null,
      })
      setPlatformCode('')
      setPlatformProductId('')
      setDisplayName('')
      setSellerProductCode('')
      reloadMaps()
    } catch (err) {
      setMapError(err instanceof ApiError ? err.message : '매핑 등록 중 오류가 발생했습니다.')
    }
  }

  const handleDeleteMap = async (mappingId: number) => {
    try {
      await api.del(`/api/products/platform-map/${mappingId}`)
      reloadMaps()
    } catch (err) {
      setMapError(err instanceof ApiError ? err.message : '매핑 삭제 중 오류가 발생했습니다.')
    }
  }

  const handleAddCost = async (e: FormEvent) => {
    e.preventDefault()
    if (!costPrice) {
      setCostError('원가를 입력하세요.')
      return
    }
    setCostError(null)
    try {
      await api.post(`/api/products/options/${option.id}/cost-history`, {
        cost_price: Number(costPrice),
        effective_from: new Date(effectiveFrom).toISOString(),
      })
      setCostPrice('')
      reloadHistory()
    } catch (err) {
      setCostError(err instanceof ApiError ? err.message : '원가 등록 중 오류가 발생했습니다.')
    }
  }

  return (
    <div className="option-manage-panel">
      <div className="option-manage-panel-header">
        <h3 ref={headingRef} tabIndex={-1}>관리 중: SKU {option.sku_code}</h3>
        <button type="button" onClick={onClose}>닫기</button>
      </div>
      <h3>통계</h3>
      <div className="table-scroll">
          <table className="data-table nested">
            <thead>
              <tr><th>총 판매수량</th><th>최근 주문일</th><th>현재원가</th><th>현재재고</th></tr>
            </thead>
            <tbody>
              <tr>
                <td>{option.stats.total_quantity_sold}</td>
                <td>{option.stats.last_order_date ? new Date(option.stats.last_order_date).toLocaleDateString() : '-'}</td>
                <td>{option.stats.current_cost_price?.toLocaleString() ?? '-'}</td>
                <td>{option.stats.sellable_stock}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <h3>플랫폼 매핑</h3>
        <form className="inline-form" onSubmit={handleAddMap}>
          <input
            type="number"
            value={platformId}
            onChange={(e) => setPlatformId(Number(e.target.value))}
            placeholder="플랫폼 ID"
            min={1}
          />
          <input
            value={platformCode}
            onChange={(e) => setPlatformCode(e.target.value)}
            placeholder="플랫폼 옵션번호"
          />
          <input
            value={platformProductId}
            onChange={(e) => setPlatformProductId(e.target.value)}
            placeholder="플랫폼 상품번호"
          />
          <input
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder="쇼핑몰 노출상품명"
          />
          <input
            value={sellerProductCode}
            onChange={(e) => setSellerProductCode(e.target.value)}
            placeholder="판매자상품코드"
          />
          <button type="submit">매핑 등록</button>
        </form>
        {mapError && <p className="form-error">{mapError}</p>}
        <div className="table-scroll">
          <table className="data-table nested">
            <thead>
              <tr>
                <th>ID</th><th>플랫폼ID</th><th>옵션번호</th><th>상품번호</th><th>노출상품명</th><th>판매자상품코드</th><th></th><th>관리</th>
              </tr>
            </thead>
            <tbody>
              {maps?.map((m) => (
                <Fragment key={m.id}>
                  <tr>
                    <td>{m.id}</td>
                    <td>{m.platform_id}</td>
                    <td>{m.platform_option_id}</td>
                    <td>{m.platform_product_id ?? '-'}</td>
                    <td>{m.display_name ?? '-'}</td>
                    <td>{m.seller_product_code ?? '-'}</td>
                    <td>
                      <button type="button" onClick={() => setEditingMapId(editingMapId === m.id ? null : m.id)}>
                        수정
                      </button>
                      <button type="button" onClick={() => handleDeleteMap(m.id)}>삭제</button>
                    </td>
                    <td>
                      <button type="button" onClick={() => handleManageMapping(m.id)}>
                        {selectedMappingId === m.id ? '닫기' : '관리'}
                      </button>
                    </td>
                  </tr>
                  {editingMapId === m.id && (
                    <PlatformMapEditRow
                      mapping={m}
                      onCancel={() => setEditingMapId(null)}
                      onSaved={() => {
                        setEditingMapId(null)
                        reloadMaps()
                      }}
                    />
                  )}
                </Fragment>
              ))}
              {maps?.length === 0 && <tr><td colSpan={8}>등록된 매핑이 없습니다.</td></tr>}
            </tbody>
          </table>
        </div>

        {/* 반드시 .filter().map()로 렌더링한다 - `{selectedMapping && <X key={id}/>}`
            형태(단일 조건부 슬롯에 key만 바꿔주는 방식)로도 등가로 보이지만, 다른
            매핑으로 전환할 때(state는 정확히 갱신되는데도) 이전 key의 DOM이 정리되지
            않고 새 key의 DOM과 함께 남는 실제 렌더링 결함이 있었다(React 19, 개발
            빌드와 production 빌드 양쪽에서 재현됨). 이 컴포넌트의 옵션 표
            `.map((o) => <OptionRow key={o.id} .../>)` 등 이 파일의 다른 목록들과
            동일한 리스트 기반 재조정 방식으로 바꾸자 문제없이 동작했다 - 원인은
            끝까지 특정하지 못했지만(React 자체 이슈로 추정) 이 패턴을 유지할 것. */}
        {maps
          ?.filter((m) => m.id === selectedMappingId)
          .map((m) => (
            <ProductSyncControls key={m.id} mapping={m} onClose={handleCloseMappingPanel} />
          ))}

        <h3>신규 채널 등록(초안)</h3>
        <p className="hint-text">
          위 "플랫폼 매핑"은 이미 채널에 등록된 상품을 수동으로 연결하는 기능이고, 아래는 아직
          등록되지 않은 상품을 채널에 실제로 등록 요청하는 기능이다. 대상 플랫폼 ID는 위 매핑 등록
          폼의 "플랫폼 ID" 입력값을 그대로 사용한다(현재: {platformId}).
        </p>
        <ProductPublishControls key={platformId} optionId={option.id} platformId={platformId} />

        <h3>원가 이력</h3>
        <form className="inline-form" onSubmit={handleAddCost}>
          <input
            type="number"
            value={costPrice}
            onChange={(e) => setCostPrice(e.target.value)}
            placeholder="원가"
            min={0}
          />
          <input type="date" value={effectiveFrom} onChange={(e) => setEffectiveFrom(e.target.value)} />
          <button type="submit">원가 등록</button>
        </form>
        {costError && <p className="form-error">{costError}</p>}
        <div className="table-scroll">
          <table className="data-table nested">
            <thead><tr><th>ID</th><th>원가</th><th>적용시작</th><th>적용종료</th></tr></thead>
            <tbody>
              {history?.map((h) => (
                <tr key={h.id}>
                  <td>{h.id}</td>
                  <td>{h.cost_price.toLocaleString()}</td>
                  <td>{new Date(h.effective_from).toLocaleDateString()}</td>
                  <td>{h.effective_to ? new Date(h.effective_to).toLocaleDateString() : '적용중'}</td>
                </tr>
              ))}
              {history?.length === 0 && <tr><td colSpan={4}>등록된 원가 이력이 없습니다.</td></tr>}
            </tbody>
          </table>
        </div>
    </div>
  )
}

function ProductImages({ productId, images, onChanged }: { productId: string; images: ProductImage[]; onChanged: () => void }) {
  const [imageUrl, setImageUrl] = useState('')
  const [isThumbnail, setIsThumbnail] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleAdd = async (e: FormEvent) => {
    e.preventDefault()
    if (!imageUrl) {
      setError('이미지 URL을 입력하세요.')
      return
    }
    setError(null)
    try {
      await api.post(`/api/products/${productId}/images`, { image_url: imageUrl, is_thumbnail: isThumbnail })
      setImageUrl('')
      setIsThumbnail(false)
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '이미지 등록 중 오류가 발생했습니다.')
    }
  }

  const handleSetThumbnail = async (imageId: number) => {
    try {
      await api.patch(`/api/products/images/${imageId}/thumbnail`, {})
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '대표이미지 지정 중 오류가 발생했습니다.')
    }
  }

  const handleDelete = async (imageId: number) => {
    try {
      await api.del(`/api/products/images/${imageId}`)
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '이미지 삭제 중 오류가 발생했습니다.')
    }
  }

  return (
    <div>
      <h3>이미지</h3>
      <form className="inline-form" onSubmit={handleAdd}>
        <input value={imageUrl} onChange={(e) => setImageUrl(e.target.value)} placeholder="이미지 URL" />
        <label>
          <input type="checkbox" checked={isThumbnail} onChange={(e) => setIsThumbnail(e.target.checked)} />
          대표이미지로 지정
        </label>
        <button type="submit">이미지 등록</button>
      </form>
      {error && <p className="form-error">{error}</p>}
      <div className="table-scroll">
        <table className="data-table">
          <thead><tr><th>미리보기</th><th>URL</th><th>구분</th><th></th><th></th></tr></thead>
          <tbody>
            {images.map((img) => (
              <tr key={img.id}>
                <td><img src={img.image_url} alt="" style={{ width: 60, height: 60, objectFit: 'cover' }} /></td>
                <td>{img.image_url}</td>
                <td>{img.is_thumbnail ? '대표이미지' : '추가이미지'}</td>
                <td>
                  {!img.is_thumbnail && (
                    <button type="button" onClick={() => handleSetThumbnail(img.id)}>대표로 지정</button>
                  )}
                </td>
                <td><button type="button" onClick={() => handleDelete(img.id)}>삭제</button></td>
              </tr>
            ))}
            {images.length === 0 && <tr><td colSpan={5}>등록된 이미지가 없습니다.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// 상용 ERP 확장(3단계, 세 번째 묶음) - 하나의 상품에 속한 여러 SKU를 채널 옵션
// 조합 상품 하나로 묶어 등록한다(단일 SKU 등록인 ProductPublishControls와는
// 별개 기능·별개 API). SKU마다 다른 값(옵션축 값/가격/재고)은 SKU별 입력
// 필드로 받는다(원시 JSON 없이 조작 가능해야 한다는 요구사항) - 축 "이름"만
// 상품 전체에 공통으로 한 번 입력해 모든 SKU에 함께 적용한다(네이버 조합형
// 옵션은 SKU마다 축 순서가 같아야 하므로 이 방식이 실수를 줄인다). 배송/반품/
// 원산지/인증/상품정보제공고시 등 채널 공통 계약 정보는 ProductPublishControls와
// 동일하게 "채널별 세부 계약 정보(JSON)"로 받는다(카테고리별 속성 자체를
// 추측하지 않기 위함 - 값의 의미가 채널마다 달라 개별 입력란으로 일반화할 수
// 없다).
type OptionGroupRowState = {
  included: boolean
  itemId: number | null
  v1: string
  v2: string
  v3: string
  price: string
  stock: string
  code: string
}

function ProductOptionGroupPublishControls({
  productId,
  platformId,
  options,
}: {
  productId: number
  platformId: number
  options: ProductOptionDetail[]
}) {
  const [draft, setDraft] = useState<ProductOptionGroupDraft | null>(null)
  const [name, setName] = useState('')
  const [descriptionHtml, setDescriptionHtml] = useState('')
  const [categoryCode, setCategoryCode] = useState('')
  const [imageUrls, setImageUrls] = useState('')
  const [baseSalePrice, setBaseSalePrice] = useState('')
  const [channelFields, setChannelFields] = useState('{}')
  const [axis1, setAxis1] = useState('옵션1')
  const [axis2, setAxis2] = useState('')
  const [axis3, setAxis3] = useState('')
  const [rows, setRows] = useState<Record<number, OptionGroupRowState>>({})
  const [error, setError] = useState<string | null>(null)
  const [isSavingDraft, setIsSavingDraft] = useState(false)
  const [savingRowId, setSavingRowId] = useState<number | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [isConfirmingEtcNotice, setIsConfirmingEtcNotice] = useState(false)
  const [command, setCommand] = useState<ProductSyncExternalCommand | null>(null)
  const [registrationStatus, setRegistrationStatus] = useState<OptionRegistrationStatus | null>(null)
  const [confirmInputs, setConfirmInputs] = useState<Record<number, string>>({})

  const emptyRow = (): OptionGroupRowState => ({
    included: false,
    itemId: null,
    v1: '',
    v2: '',
    v3: '',
    price: '',
    stock: '',
    code: '',
  })

  const applyDraft = (d: ProductOptionGroupDraft) => {
    setDraft(d)
    setName(d.name ?? '')
    setDescriptionHtml(d.description_html ?? '')
    setCategoryCode(d.category_code ?? '')
    setImageUrls(d.image_urls.join(', '))
    setBaseSalePrice(d.base_sale_price?.toString() ?? '')
    setChannelFields(JSON.stringify(d.channel_fields, null, 2))
    if (d.items.length > 0) {
      const axes = d.items[0].option_values.map(([axisName]) => axisName)
      setAxis1(axes[0] ?? '옵션1')
      setAxis2(axes[1] ?? '')
      setAxis3(axes[2] ?? '')
    }
    const nextRows: Record<number, OptionGroupRowState> = {}
    for (const option of options) {
      const item = d.items.find((i) => i.product_option_id === option.id)
      nextRows[option.id] = item
        ? {
            included: true,
            itemId: item.id,
            v1: item.option_values[0]?.[1] ?? '',
            v2: item.option_values[1]?.[1] ?? '',
            v3: item.option_values[2]?.[1] ?? '',
            price: item.sale_price?.toString() ?? '',
            stock: item.stock_quantity?.toString() ?? '',
            code: item.seller_product_code ?? '',
          }
        : emptyRow()
    }
    setRows(nextRows)
  }

  const loadDraft = () => {
    api
      .get<ProductOptionGroupDraft>(`/api/products/${productId}/option-publish-draft/${platformId}`)
      .then(applyDraft)
      .catch(() => {
        // 초안이 아직 없으면(404) 빈 폼을 그대로 둔다 - 오류로 취급하지 않는다.
        const nextRows: Record<number, OptionGroupRowState> = {}
        for (const option of options) {
          nextRows[option.id] = emptyRow()
        }
        setRows(nextRows)
      })
  }

  useEffect(() => {
    loadDraft()
    setRegistrationStatus(null)
    setCommand(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [productId, platformId])

  const pollCommand = async (commandId: number) => {
    try {
      setCommand(await api.get<ProductSyncExternalCommand>(`/api/products/sync-commands/${commandId}`))
    } catch {
      // 폴링 실패는 조용히 무시한다.
    }
  }

  const handleSaveGroupDraft = async (e: FormEvent) => {
    e.preventDefault()
    let parsedChannelFields: Record<string, unknown>
    try {
      parsedChannelFields = channelFields.trim() ? JSON.parse(channelFields) : {}
    } catch {
      setError('채널별 세부 계약 정보(JSON) 형식이 올바르지 않습니다.')
      return
    }
    setError(null)
    setIsSavingDraft(true)
    try {
      const saved = await api.post<ProductOptionGroupDraft>(`/api/products/${productId}/option-publish-draft`, {
        platform_id: platformId,
        name: name || null,
        description_html: descriptionHtml || null,
        category_code: categoryCode || null,
        image_urls: imageUrls.trim()
          ? imageUrls
              .split(',')
              .map((s) => s.trim())
              .filter(Boolean)
          : null,
        base_sale_price: baseSalePrice ? Number(baseSalePrice) : null,
        channel_fields: parsedChannelFields,
      })
      setDraft(saved)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '초안 저장 중 오류가 발생했습니다.')
    } finally {
      setIsSavingDraft(false)
    }
  }

  const updateRow = (optionId: number, patch: Partial<OptionGroupRowState>) => {
    setRows((prev) => ({ ...prev, [optionId]: { ...(prev[optionId] ?? emptyRow()), ...patch } }))
  }

  const handleToggleRow = async (option: ProductOptionDetail) => {
    const row = rows[option.id] ?? emptyRow()
    if (row.included && row.itemId) {
      if (!window.confirm(`SKU '${option.sku_code}'을(를) 이 옵션조합 초안에서 제외하시겠습니까?`)) {
        return
      }
      if (!draft) return
      try {
        await api.del(`/api/products/option-publish-drafts/${draft.id}/items/${row.itemId}`)
        updateRow(option.id, { ...emptyRow() })
      } catch (err) {
        setError(err instanceof ApiError ? err.message : '품목 제외 중 오류가 발생했습니다.')
      }
      return
    }
    updateRow(option.id, { included: !row.included })
  }

  const handleSaveRow = async (option: ProductOptionDetail) => {
    const row = rows[option.id]
    if (!row) return
    if (!draft) {
      setError('먼저 위의 상품 공통정보를 저장하세요(초안 저장).')
      return
    }
    if (!row.v1.trim() || (axis2 && !row.v2.trim()) || (axis3 && !row.v3.trim())) {
      setError(`SKU '${option.sku_code}'의 옵션값을 모두 입력하세요.`)
      return
    }
    if (!row.price || !row.stock) {
      setError(`SKU '${option.sku_code}'의 판매가/재고수량을 입력하세요.`)
      return
    }
    setError(null)
    setSavingRowId(option.id)
    try {
      const optionValues: string[][] = [[axis1, row.v1]]
      if (axis2) optionValues.push([axis2, row.v2])
      if (axis3) optionValues.push([axis3, row.v3])
      const item = await api.post<ProductOptionGroupItem>(
        `/api/products/option-publish-drafts/${draft.id}/items`,
        {
          product_option_id: option.id,
          option_values: optionValues,
          seller_product_code: row.code || null,
          sale_price: Number(row.price),
          stock_quantity: Number(row.stock),
        },
      )
      updateRow(option.id, { itemId: item.id, code: item.seller_product_code ?? '' })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '품목 저장 중 오류가 발생했습니다.')
    } finally {
      setSavingRowId(null)
    }
  }

  const includedCount = Object.values(rows).filter((r) => r.included && r.itemId).length

  const handleSubmit = async () => {
    if (!draft) return
    if (includedCount === 0) {
      setError('등록할 SKU가 1개 이상 필요합니다 - 먼저 품목을 저장하세요.')
      return
    }
    if (
      !window.confirm(
        `'${draft.name ?? ''}' 상품(SKU ${includedCount}개)을 이 초안 내용 그대로 채널에 옵션조합으로 등록 ` +
          '요청하시겠습니까?\n실제 채널 호출은 잠시 후 비동기로 처리되며, 등록 요청은 취소할 수 없습니다.',
      )
    ) {
      return
    }
    setError(null)
    setIsSubmitting(true)
    try {
      const result = await api.post<ProductSyncCommand>(`/api/products/option-publish-drafts/${draft.id}/submit`, {})
      await pollCommand(result.command_id)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '등록 요청 중 오류가 발생했습니다.')
    } finally {
      setIsSubmitting(false)
    }
  }

  const handleCheckStatus = async () => {
    if (!draft) return
    setError(null)
    try {
      setRegistrationStatus(
        await api.get<OptionRegistrationStatus>(`/api/products/option-publish-drafts/${draft.id}/registration-status`),
      )
      loadDraft() // 자동 매칭으로 매핑이 새로 확정됐을 수 있어 최신 상태를 다시 읽는다.
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '심사상태 조회 중 오류가 발생했습니다.')
    }
  }

  const handleConfirmItemMapping = async (productOptionId: number) => {
    if (!draft) return
    const channelOptionId = confirmInputs[productOptionId]
    if (!channelOptionId) return
    if (!window.confirm(`옵션 식별자 '${channelOptionId}'로 이 SKU의 매핑을 확정하시겠습니까? 확정 후에는 되돌릴 수 없습니다.`)) {
      return
    }
    setError(null)
    try {
      await api.post(`/api/products/option-publish-drafts/${draft.id}/confirm-item-mapping`, {
        product_option_id: productOptionId,
        channel_option_id: channelOptionId,
      })
      setConfirmInputs((prev) => ({ ...prev, [productOptionId]: '' }))
      await handleCheckStatus()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '매핑 확정 중 오류가 발생했습니다.')
    }
  }

  // ProductPublishControls(단일 SKU 등록)의 ETC 확인 블록과 동일한 원칙 - 이
  // 확인은 시스템이 대신 검증한 것이 아니라 운영자 확인 기록일 뿐이다(공식으로
  // 검증할 API가 없다 - integrations.malls.naver_smartstore_connector 모듈
  // docstring 참고). 옵션조합 등록도 같은 원상품 레벨 필드(productInfoProvidedNotice)를
  // 쓰므로 동일하게 필요하다.
  let currentNoticeType: string | undefined
  try {
    const parsed = channelFields.trim() ? JSON.parse(channelFields) : {}
    const notice = parsed?.productInfoProvidedNotice
    if (notice && typeof notice === 'object') {
      currentNoticeType = (notice as Record<string, unknown>).productInfoProvidedNoticeType as string | undefined
    }
  } catch {
    currentNoticeType = undefined
  }

  const handleConfirmEtcNotice = async () => {
    if (!draft) return
    if (
      !window.confirm(
        `카테고리 코드 '${draft.category_code ?? ''}'에 네이버 ETC(기타 재화) 상품정보제공고시 양식을 ` +
          '쓰는 것이 맞는지 판매자센터에서 직접 확인했습니까?\n' +
          '이 확인은 시스템이 대신 검증한 것이 아니라 운영자 본인의 확인 기록으로 남습니다.',
      )
    ) {
      return
    }
    setError(null)
    setIsConfirmingEtcNotice(true)
    try {
      const updated = await api.post<ProductOptionGroupDraft>(
        `/api/products/option-publish-drafts/${draft.id}/confirm-etc-notice`,
        {},
      )
      setDraft(updated)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'ETC 카테고리 적합성 확인 기록 중 오류가 발생했습니다.')
    } finally {
      setIsConfirmingEtcNotice(false)
    }
  }

  return (
    <div>
      <form className="inline-form" onSubmit={handleSaveGroupDraft} style={{ flexWrap: 'wrap', marginBottom: 4 }}>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="상품명" />
        <input
          type="number"
          value={baseSalePrice}
          onChange={(e) => setBaseSalePrice(e.target.value)}
          placeholder="기준 판매가(네이버 옵션가의 기준값)"
          min={0}
        />
        <input value={categoryCode} onChange={(e) => setCategoryCode(e.target.value)} placeholder="카테고리 코드" />
        <input
          value={imageUrls}
          onChange={(e) => setImageUrls(e.target.value)}
          placeholder="이미지 URL(쉼표 구분, 첫 번째=대표, http/https만)"
          className="publish-field-md"
        />
        <textarea
          value={descriptionHtml}
          onChange={(e) => setDescriptionHtml(e.target.value)}
          placeholder="상세설명(HTML)"
          rows={2}
          className="publish-field-md"
        />
        <textarea
          value={channelFields}
          onChange={(e) => setChannelFields(e.target.value)}
          placeholder="채널별 세부 계약 정보(JSON) - 배송/반품/원산지/인증/상품정보제공고시 등"
          rows={3}
          className="publish-field-lg"
        />
        <button type="submit" disabled={isSavingDraft}>
          {isSavingDraft ? '저장 중...' : '상품 공통정보 저장'}
        </button>
      </form>

      {currentNoticeType === 'ETC' && (
        <div className="hint-text" style={{ marginBottom: 8, padding: 8, border: '1px solid var(--border)', borderRadius: 6 }}>
          <p style={{ margin: '0 0 4px' }}>
            ⚠️ 이 채널은 상품정보제공고시로 <strong>ETC(기타 재화)</strong> 양식을 사용합니다. ETC 입력 형식
            자체는 이 시스템이 지원하지만, <strong>이 카테고리에 ETC 고시가 실제로 맞는지는 공식 API로 검증할
            방법이 없어</strong> 운영자가 네이버 판매자센터에서 직접 확인해야 합니다. 아래 확인은 그 사실을
            기록할 뿐이며 "공식 적합성 검증 완료"를 의미하지 않습니다 - 카테고리 코드나 고시유형을 바꾸면
            이 확인은 자동으로 무효화됩니다.
          </p>
          {draft?.etc_notice_confirmation_valid ? (
            <p style={{ margin: '0 0 4px', color: '#15803d' }}>
              ✅ 확인됨 - 사용자 #{draft.etc_notice_confirmed_by}
              {draft.etc_notice_confirmed_at && `, ${new Date(draft.etc_notice_confirmed_at).toLocaleString()}`}
              (카테고리 {draft.etc_notice_confirmed_category_code})
            </p>
          ) : draft?.etc_notice_confirmed_at ? (
            <p style={{ margin: '0 0 4px', color: '#b45309' }}>
              ⚠️ 이전 확인(카테고리 {draft.etc_notice_confirmed_category_code})이 카테고리/고시유형 변경으로
              무효화되었습니다 - 다시 확인이 필요합니다.
            </p>
          ) : (
            <p style={{ margin: '0 0 4px', color: '#b45309' }}>❌ 아직 확인되지 않았습니다 - 확인 전에는 등록이 차단됩니다.</p>
          )}
          <button type="button" onClick={handleConfirmEtcNotice} disabled={!draft || isConfirmingEtcNotice}>
            {isConfirmingEtcNotice ? '기록 중...' : '이 카테고리에 ETC 고시가 맞음을 확인'}
          </button>
          {!draft && <span> (먼저 상품 공통정보를 저장하세요)</span>}
        </div>
      )}

      <p className="hint-text">
        옵션축 이름(최대 3개, 네이버 조합형 옵션 제한) - 모든 SKU에 공통 적용됩니다.
      </p>
      <div className="inline-form" style={{ flexWrap: 'wrap', marginBottom: 8 }}>
        <input value={axis1} onChange={(e) => setAxis1(e.target.value)} placeholder="축1 이름(예: 색상)" />
        <input value={axis2} onChange={(e) => setAxis2(e.target.value)} placeholder="축2 이름(예: 사이즈, 선택)" />
        <input value={axis3} onChange={(e) => setAxis3(e.target.value)} placeholder="축3 이름(선택)" />
      </div>

      <p className="hint-text">SKU마다 포함 여부와 옵션값·판매가·재고수량을 입력한 뒤 각 행의 저장을 누르세요.</p>
      {options.map((option) => {
        const row = rows[option.id] ?? emptyRow()
        return (
          <div
            key={option.id}
            className="inline-form"
            style={{ flexWrap: 'wrap', marginBottom: 4, padding: 6, border: '1px solid var(--border)', borderRadius: 6 }}
          >
            <label>
              <input type="checkbox" checked={row.included} onChange={() => handleToggleRow(option)} /> {option.sku_code}
            </label>
            {row.included && (
              <>
                <input value={row.v1} onChange={(e) => updateRow(option.id, { v1: e.target.value })} placeholder={axis1 || '옵션값1'} />
                {axis2 && (
                  <input value={row.v2} onChange={(e) => updateRow(option.id, { v2: e.target.value })} placeholder={axis2} />
                )}
                {axis3 && (
                  <input value={row.v3} onChange={(e) => updateRow(option.id, { v3: e.target.value })} placeholder={axis3} />
                )}
                <input
                  type="number"
                  value={row.price}
                  onChange={(e) => updateRow(option.id, { price: e.target.value })}
                  placeholder="판매가"
                  min={0}
                />
                <input
                  type="number"
                  value={row.stock}
                  onChange={(e) => updateRow(option.id, { stock: e.target.value })}
                  placeholder="초기 재고수량"
                  min={0}
                />
                <input
                  value={row.code}
                  onChange={(e) => updateRow(option.id, { code: e.target.value })}
                  placeholder={`판매자 관리코드(미입력 시 SKU코드 ${option.sku_code})`}
                  className="publish-field-sm"
                />
                <button type="button" onClick={() => handleSaveRow(option)} disabled={savingRowId === option.id}>
                  {row.itemId ? '수정 저장' : '품목 저장'}
                </button>
                {row.itemId && <span className="hint-text">✅ 저장됨</span>}
              </>
            )}
          </div>
        )
      })}

      {error && <p className="form-error">{error}</p>}

      {draft && (
        <div style={{ marginTop: 8 }}>
          <button type="button" onClick={handleSubmit} disabled={isSubmitting}>
            {isSubmitting ? '요청 중...' : `채널에 옵션조합으로 등록 요청(SKU ${includedCount}개)`}
          </button>
          {draft.channel_product_id && (
            <>
              {' '}
              <button type="button" onClick={handleCheckStatus}>심사상태·매핑 조회</button>
            </>
          )}
        </div>
      )}

      {draft?.channel_product_id && (
        <p className="hint-text">
          접수됨 - 채널 상품ID: {draft.channel_product_id}
          {draft.channel_option_id && ` / 채널 리스팅ID: ${draft.channel_option_id}`}
        </p>
      )}

      {registrationStatus && (
        <div style={{ marginTop: 4 }}>
          <p className="hint-text">
            채널 상태: {registrationStatus.channel_status_name ?? '확인되지 않음'} / 전체 진행:{' '}
            {
              {
                PENDING_REVIEW: '심사 대기',
                PARTIALLY_MAPPED: '일부 매핑 미확정',
                FULLY_MAPPED: '매핑 완료',
              }[registrationStatus.overall_status] ?? registrationStatus.overall_status
            }
          </p>
          {registrationStatus.items.map((item) => (
            <p key={item.product_option_id} className="hint-text" style={{ margin: '2px 0' }}>
              SKU(옵션ID {item.product_option_id}, 코드 {item.seller_product_code ?? '-'}):{' '}
              {item.mapped ? (
                <span style={{ color: '#15803d' }}>✅ 매핑 완료(식별자 {item.channel_option_id})</span>
              ) : item.ambiguous ? (
                <span style={{ color: '#b45309' }}>
                  ⚠️ 후보가 여러 개라 자동 확정할 수 없습니다 - 채널에서 직접 확인 후 수동 확정하세요.{' '}
                  <input
                    value={confirmInputs[item.product_option_id] ?? ''}
                    onChange={(e) =>
                      setConfirmInputs((prev) => ({ ...prev, [item.product_option_id]: e.target.value }))
                    }
                    placeholder="확정할 옵션 식별자"
                  />
                  <button type="button" onClick={() => handleConfirmItemMapping(item.product_option_id)}>
                    매핑 확정
                  </button>
                </span>
              ) : (
                <span style={{ color: '#b45309' }}>❌ 아직 확인되지 않음(심사 대기 중일 수 있습니다)</span>
              )}
            </p>
          ))}
        </div>
      )}

      {command && (
        <p>
          명령 #{command.id}: <span className="status-badge">{SYNC_STATUS_LABELS[command.status] ?? command.status}</span>
          {command.error_code && ` (사유: ${command.error_code})`}
          {' '}
          <button type="button" onClick={() => pollCommand(command.id)}>상태 새로고침</button>
        </p>
      )}
    </div>
  )
}

export function ProductDetailPage() {
  const { productId } = useParams<{ productId: string }>()
  useRecentView('PRODUCT', productId)
  const { data: product, error, isLoading, reload } = useApiData<ProductDetail>(
    () => api.get(`/api/products/${productId}/detail`),
    [productId],
  )

  const [optionForm, setOptionForm] = useState<ProductOptionCreate>({ sku_code: '' })
  const [optionError, setOptionError] = useState<string | null>(null)
  const [selectedOptionId, setSelectedOptionId] = useState<number | null>(null)
  // 관리 패널이 옵션 표 밖으로 분리되면서(narrow-screen 대응) 패널을 연 "관리"
  // 버튼과 패널이 더 이상 DOM상 붙어있지 않다 - 패널을 닫을 때(행의 토글이든
  // 패널 자체의 닫기 버튼이든) 키보드 포커스를 원래 버튼으로 되돌리기 위해
  // 마지막으로 패널을 연 버튼을 기억해 둔다.
  const manageButtonRef = useRef<HTMLElement | null>(null)

  const handleManage = (id: number) => {
    if (selectedOptionId === id) {
      setSelectedOptionId(null)
      return
    }
    manageButtonRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    setSelectedOptionId(id)
  }

  const handleCloseManagePanel = () => {
    setSelectedOptionId(null)
    manageButtonRef.current?.focus()
  }

  const selectedOption = product?.options.find((o) => o.id === selectedOptionId) ?? null

  const handleCreateOption = async (e: FormEvent) => {
    e.preventDefault()
    if (!optionForm.sku_code) {
      setOptionError('SKU 코드를 입력하세요.')
      return
    }
    setOptionError(null)
    try {
      await api.post(`/api/products/${productId}/options`, optionForm)
      setOptionForm({ sku_code: '' })
      reload()
    } catch (err) {
      setOptionError(err instanceof ApiError ? err.message : '옵션 등록 중 오류가 발생했습니다.')
    }
  }

  const handleToggleActive = async (option: ProductOptionDetail) => {
    try {
      await api.patch(`/api/products/options/${option.id}/active`, { is_active: !option.is_active })
      reload()
    } catch (err) {
      setOptionError(err instanceof ApiError ? err.message : '옵션 상태 변경 중 오류가 발생했습니다.')
    }
  }

  const handleDeleteOption = async (option: ProductOptionDetail) => {
    if (!window.confirm(`옵션 '${option.sku_code}'을(를) 삭제하시겠습니까?`)) {
      return
    }
    try {
      await api.del(`/api/products/options/${option.id}`)
      reload()
    } catch (err) {
      setOptionError(
        err instanceof ApiError ? err.message : '옵션 삭제 중 오류가 발생했습니다.',
      )
    }
  }

  const handleMoveOption = async (index: number, direction: -1 | 1) => {
    if (!product) return
    const ids = product.options.map((o) => o.id)
    const target = index + direction
    if (target < 0 || target >= ids.length) return
    ;[ids[index], ids[target]] = [ids[target], ids[index]]
    try {
      await api.patch(`/api/products/${productId}/options/reorder`, { option_ids: ids })
      reload()
    } catch (err) {
      setOptionError(err instanceof ApiError ? err.message : '옵션 순서 변경 중 오류가 발생했습니다.')
    }
  }

  return (
    <div className="product-detail-page">
      <p><Link to="/products">← 상품 목록으로</Link></p>
      <h2>
        상품 상세 #{productId} {productId && <FavoriteStar targetType="PRODUCT" targetId={Number(productId)} />}
      </h2>
      {isLoading && <p>불러오는 중...</p>}
      {error && <p className="form-error">{error}</p>}
      {product && <ProductInfoEdit key={product.id} product={product} onSaved={reload} />}

      <h3>옵션(SKU)</h3>
      <form className="inline-form" onSubmit={handleCreateOption}>
        <input
          value={optionForm.sku_code}
          onChange={(e) => setOptionForm({ ...optionForm, sku_code: e.target.value })}
          placeholder="SKU 코드"
        />
        <input
          value={optionForm.option_name ?? ''}
          onChange={(e) => setOptionForm({ ...optionForm, option_name: e.target.value })}
          placeholder="옵션명"
        />
        <input
          value={optionForm.color ?? ''}
          onChange={(e) => setOptionForm({ ...optionForm, color: e.target.value })}
          placeholder="색상"
        />
        <input
          value={optionForm.size ?? ''}
          onChange={(e) => setOptionForm({ ...optionForm, size: e.target.value })}
          placeholder="사이즈"
        />
        <input
          type="number"
          value={optionForm.unit_cost_price ?? ''}
          onChange={(e) =>
            setOptionForm({ ...optionForm, unit_cost_price: e.target.value ? Number(e.target.value) : undefined })
          }
          placeholder="단가(매입원가)"
          min={0}
        />
        <button type="submit">옵션 등록</button>
      </form>
      {optionError && <p className="form-error">{optionError}</p>}

      <p className="hint-text">옵션명·색상·사이즈·바코드·단가를 칸에서 바로 고친 뒤 <strong>저장</strong>을 누르세요(Enter로도 저장).</p>
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>순서</th><th>SKU</th><th>옵션명</th><th>색상</th><th>사이즈</th><th>바코드</th><th>단가</th><th>상태</th><th>저장</th><th>판매설정</th><th>삭제/관리</th>
            </tr>
          </thead>
          <tbody>
            {product?.options.map((o, index) => (
              <OptionRow
                key={o.id}
                option={o}
                index={index}
                total={product.options.length}
                onReload={reload}
                onMove={handleMoveOption}
                onToggleActive={handleToggleActive}
                onDelete={handleDeleteOption}
                onManage={handleManage}
                isManaging={selectedOptionId === o.id}
              />
            ))}
            {product?.options.length === 0 && <tr><td colSpan={OPTIONS_TABLE_COLUMNS}>등록된 옵션이 없습니다.</td></tr>}
          </tbody>
        </table>
      </div>

      {selectedOption && (
        <OptionSubDetail key={selectedOption.id} option={selectedOption} warehousePlatformId={1} onClose={handleCloseManagePanel} />
      )}

      {product && product.options.length > 0 && (
        <>
          <h3>옵션조합 상품 신규 등록(여러 SKU를 채널 옵션 목록 하나로 등록)</h3>
          <p className="hint-text">
            아래 "신규 채널 등록(초안)"(SKU별 단일 상품 등록)과 달리, 이 상품에 속한 여러 SKU를 채널의
            옵션 조합 상품 하나로 묶어 등록한다 - 이미 등록된 SKU가 있으면 등록 요청 시 차단된다.
          </p>
          <ProductOptionGroupPublishControls
            key={product.id}
            productId={product.id}
            platformId={1}
            options={product.options}
          />
        </>
      )}

      {product && <ProductImages productId={productId ?? ''} images={product.images} onChanged={reload} />}
    </div>
  )
}
