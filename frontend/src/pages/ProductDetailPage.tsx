import { Fragment, useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import { useApiData } from '../api/useApiData'
import { useRecentView } from '../api/useRecentView'
import { FavoriteStar } from '../components/FavoriteStar'
import type {
  ProductCostHistory,
  ProductDetail,
  ProductImage,
  ProductOptionCreate,
  ProductOptionDetail,
  ProductPlatformMap,
} from '../api/types'

const OPTIONS_TABLE_COLUMNS = 11

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
        <option value="ACTIVE">ACTIVE</option>
        <option value="DISCONTINUED">DISCONTINUED</option>
      </select>
      <button type="submit" disabled={isSaving}>{isSaving ? '저장 중...' : '상품정보 저장'}</button>
      <button type="button" onClick={handleDuplicate}>상품 복제</button>
      <button type="button" onClick={handleDelete}>상품 삭제</button>
      {error && <span className="form-error">{error}</span>}
    </form>
  )
}

function OptionEditRow({ option, onSaved }: { option: ProductOptionDetail; onSaved: () => void }) {
  const [form, setForm] = useState({
    option_name: option.option_name ?? '',
    color: option.color ?? '',
    size: option.size ?? '',
    barcode: option.barcode ?? '',
    unit_cost_price: option.unit_cost_price ?? undefined,
  })
  const [error, setError] = useState<string | null>(null)
  const [isSaving, setIsSaving] = useState(false)

  const handleSave = async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    setIsSaving(true)
    try {
      await api.patch(`/api/products/options/${option.id}`, {
        option_name: form.option_name || null,
        color: form.color || null,
        size: form.size || null,
        barcode: form.barcode || null,
        unit_cost_price: form.unit_cost_price ?? null,
      })
      onSaved()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '옵션 수정 중 오류가 발생했습니다.')
    } finally {
      setIsSaving(false)
    }
  }

  return (
    <tr className="detail-subrow">
      <td colSpan={OPTIONS_TABLE_COLUMNS}>
        <form className="inline-form" onSubmit={handleSave}>
          <input
            value={form.option_name}
            onChange={(e) => setForm({ ...form, option_name: e.target.value })}
            placeholder="옵션명"
          />
          <input value={form.color} onChange={(e) => setForm({ ...form, color: e.target.value })} placeholder="색상" />
          <input value={form.size} onChange={(e) => setForm({ ...form, size: e.target.value })} placeholder="사이즈" />
          <input
            value={form.barcode}
            onChange={(e) => setForm({ ...form, barcode: e.target.value })}
            placeholder="바코드"
          />
          <input
            type="number"
            value={form.unit_cost_price ?? ''}
            onChange={(e) =>
              setForm({ ...form, unit_cost_price: e.target.value ? Number(e.target.value) : undefined })
            }
            placeholder="단가(매입원가)"
            min={0}
          />
          <button type="submit" disabled={isSaving}>{isSaving ? '저장 중...' : '옵션정보 저장'}</button>
          {error && <span className="form-error">{error}</span>}
        </form>
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
      <td colSpan={7}>
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

function OptionSubDetail({ option, warehousePlatformId }: { option: ProductOptionDetail; warehousePlatformId: number }) {
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
    <tr className="detail-subrow">
      <td colSpan={OPTIONS_TABLE_COLUMNS}>
        <h3>SKU {option.sku_code} - 통계</h3>
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
        <table className="data-table nested">
          <thead>
            <tr>
              <th>ID</th><th>플랫폼ID</th><th>옵션번호</th><th>상품번호</th><th>노출상품명</th><th>판매자상품코드</th><th></th>
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
            {maps?.length === 0 && <tr><td colSpan={7}>등록된 매핑이 없습니다.</td></tr>}
          </tbody>
        </table>

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
      </td>
    </tr>
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
  const [editingOptionId, setEditingOptionId] = useState<number | null>(null)

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
    <div>
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

      <table className="data-table">
        <thead>
          <tr>
            <th>순서</th><th>ID</th><th>SKU</th><th>옵션명</th><th>색상</th><th>사이즈</th><th>단가</th><th>상태</th><th></th><th></th><th></th>
          </tr>
        </thead>
        <tbody>
          {product?.options.map((o, index) => (
            <Fragment key={o.id}>
              <tr>
                <td>
                  <button type="button" disabled={index === 0} onClick={() => handleMoveOption(index, -1)}>▲</button>
                  <button
                    type="button"
                    disabled={index === product.options.length - 1}
                    onClick={() => handleMoveOption(index, 1)}
                  >
                    ▼
                  </button>
                </td>
                <td>{o.id}</td>
                <td>{o.sku_code}</td>
                <td>{o.option_name ?? '-'}</td>
                <td>{o.color ?? '-'}</td>
                <td>{o.size ?? '-'}</td>
                <td>{o.unit_cost_price?.toLocaleString() ?? '-'}</td>
                <td><span className="status-badge">{o.is_active ? 'ACTIVE' : 'INACTIVE'}</span></td>
                <td><button type="button" onClick={() => handleToggleActive(o)}>{o.is_active ? '비활성화' : '활성화'}</button></td>
                <td>
                  <button type="button" onClick={() => setEditingOptionId(editingOptionId === o.id ? null : o.id)}>
                    {editingOptionId === o.id ? '닫기' : '수정'}
                  </button>
                  <button type="button" onClick={() => handleDeleteOption(o)}>삭제</button>
                </td>
                <td>
                  <button type="button" onClick={() => setSelectedOptionId(selectedOptionId === o.id ? null : o.id)}>
                    {selectedOptionId === o.id ? '닫기' : '관리'}
                  </button>
                </td>
              </tr>
              {editingOptionId === o.id && (
                <OptionEditRow
                  option={o}
                  onSaved={() => {
                    setEditingOptionId(null)
                    reload()
                  }}
                />
              )}
              {selectedOptionId === o.id && <OptionSubDetail option={o} warehousePlatformId={1} />}
            </Fragment>
          ))}
          {product?.options.length === 0 && <tr><td colSpan={OPTIONS_TABLE_COLUMNS}>등록된 옵션이 없습니다.</td></tr>}
        </tbody>
      </table>

      {product && <ProductImages productId={productId ?? ''} images={product.images} onChanged={reload} />}
    </div>
  )
}
