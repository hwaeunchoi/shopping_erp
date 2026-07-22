import { useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import { useApiData } from '../api/useApiData'
import { FavoriteStar } from '../components/FavoriteStar'
import type { Platform, Product, ProductCreate, ProductNaverSyncResult } from '../api/types'

export function ProductsPage() {
  const [category, setCategory] = useState('')
  const [showDeleted, setShowDeleted] = useState(false)
  const { data, error, isLoading, reload } = useApiData<Product[]>(
    () =>
      api.get(
        `/api/products?${category ? `category=${category}&` : ''}include_deleted=${showDeleted}`,
      ),
    [category, showDeleted],
  )

  const [form, setForm] = useState<ProductCreate>({
    name: '',
    category: '',
    brand: '',
    manufacturer: '',
    base_price: undefined,
  })
  const [createError, setCreateError] = useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)

  const [actionError, setActionError] = useState<string | null>(null)

  const { data: platforms } = useApiData<Platform[]>(() => api.get('/api/platforms'), [])
  const [syncPlatformId, setSyncPlatformId] = useState<number | ''>('')
  const [syncError, setSyncError] = useState<string | null>(null)
  const [syncResult, setSyncResult] = useState<ProductNaverSyncResult | null>(null)
  const [isSyncing, setIsSyncing] = useState(false)

  const handleSyncFromNaver = async (e: FormEvent) => {
    e.preventDefault()
    if (!syncPlatformId) {
      setSyncError('플랫폼을 선택하세요.')
      return
    }
    setIsSyncing(true)
    setSyncError(null)
    setSyncResult(null)
    try {
      const result = await api.post<ProductNaverSyncResult>('/api/products/sync-from-naver', {
        platform_id: syncPlatformId,
      })
      setSyncResult(result)
      reload()
    } catch (err) {
      setSyncError(err instanceof ApiError ? err.message : '상품 동기화 중 오류가 발생했습니다.')
    } finally {
      setIsSyncing(false)
    }
  }

  const handleCreate = async (e: FormEvent) => {
    e.preventDefault()
    if (!form.name) {
      setCreateError('상품명을 입력하세요.')
      return
    }
    setCreateError(null)
    setIsSubmitting(true)
    try {
      await api.post<Product>('/api/products', {
        name: form.name,
        category: form.category || null,
        brand: form.brand || null,
        manufacturer: form.manufacturer || null,
        base_price: form.base_price || null,
      })
      setForm({ name: '', category: '', brand: '', manufacturer: '', base_price: undefined })
      reload()
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.message : '상품 등록 중 오류가 발생했습니다.')
    } finally {
      setIsSubmitting(false)
    }
  }

  const handleDelete = async (product: Product) => {
    if (!window.confirm(`'${product.name}' 상품을 삭제하시겠습니까?`)) {
      return
    }
    setActionError(null)
    try {
      await api.del(`/api/products/${product.id}`)
      reload()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : '상품 삭제 중 오류가 발생했습니다.')
    }
  }

  const handleRestore = async (product: Product) => {
    setActionError(null)
    try {
      await api.post(`/api/products/${product.id}/restore`, {})
      reload()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : '상품 복원 중 오류가 발생했습니다.')
    }
  }

  const handleDuplicate = async (product: Product) => {
    setActionError(null)
    try {
      await api.post(`/api/products/${product.id}/duplicate`, {})
      reload()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : '상품 복제 중 오류가 발생했습니다.')
    }
  }

  return (
    <div>
      <h2>상품관리</h2>

      <h3>네이버 상품 동기화</h3>
      <form className="inline-form" onSubmit={handleSyncFromNaver}>
        <select
          value={syncPlatformId}
          onChange={(e) => setSyncPlatformId(e.target.value ? Number(e.target.value) : '')}
        >
          <option value="">플랫폼 선택</option>
          {platforms?.map((p) => (
            <option key={p.id} value={p.id}>{p.name}</option>
          ))}
        </select>
        <button type="submit" disabled={isSyncing}>{isSyncing ? '동기화 중...' : '네이버 상품 동기화'}</button>
      </form>
      {syncError && <p className="form-error">{syncError}</p>}
      {syncResult && (
        <p className="form-info">
          동기화 완료 — 총 {syncResult.total_items}건 (신규상품 {syncResult.created_products}, 신규옵션{' '}
          {syncResult.created_options}, 갱신옵션 {syncResult.updated_options})
        </p>
      )}

      <h3>상품 등록</h3>
      <form className="inline-form" onSubmit={handleCreate}>
        <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="상품명" />
        <input
          value={form.category ?? ''}
          onChange={(e) => setForm({ ...form, category: e.target.value })}
          placeholder="카테고리"
        />
        <input
          value={form.brand ?? ''}
          onChange={(e) => setForm({ ...form, brand: e.target.value })}
          placeholder="브랜드"
        />
        <input
          value={form.manufacturer ?? ''}
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
        <button type="submit" disabled={isSubmitting}>{isSubmitting ? '등록 중...' : '상품 등록'}</button>
      </form>
      {createError && <p className="form-error">{createError}</p>}

      <div className="filter-bar">
        <input value={category} onChange={(e) => setCategory(e.target.value)} placeholder="카테고리 필터" />
        <label>
          <input type="checkbox" checked={showDeleted} onChange={(e) => setShowDeleted(e.target.checked)} />
          삭제된 상품 포함
        </label>
      </div>

      {isLoading && <p>불러오는 중...</p>}
      {error && <p className="form-error">{error}</p>}
      {actionError && <p className="form-error">{actionError}</p>}
      {data && (
        <table className="data-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>상품명</th>
              <th>카테고리</th>
              <th>브랜드</th>
              <th>제조사</th>
              <th>판매가</th>
              <th>상태</th>
              <th></th>
              <th></th>
              <th></th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {data.map((p) => (
              <tr key={p.id}>
                <td>{p.id}</td>
                <td>{p.name}</td>
                <td>{p.category ?? '-'}</td>
                <td>{p.brand ?? '-'}</td>
                <td>{p.manufacturer ?? '-'}</td>
                <td>{p.base_price?.toLocaleString() ?? '-'}</td>
                <td>
                  <span className="status-badge">{p.is_deleted ? '삭제됨' : p.status}</span>
                </td>
                <td>{p.is_deleted ? '-' : <Link to={`/products/${p.id}`}>상세</Link>}</td>
                <td>{p.is_deleted ? '-' : <FavoriteStar targetType="PRODUCT" targetId={p.id} />}</td>
                <td>
                  {p.is_deleted ? (
                    <button type="button" onClick={() => handleRestore(p)}>복원</button>
                  ) : (
                    <button type="button" onClick={() => handleDuplicate(p)}>복제</button>
                  )}
                </td>
                <td>{p.is_deleted ? '-' : <button type="button" onClick={() => handleDelete(p)}>삭제</button>}</td>
              </tr>
            ))}
            {data.length === 0 && <tr><td colSpan={11}>등록된 상품이 없습니다.</td></tr>}
          </tbody>
        </table>
      )}
    </div>
  )
}
