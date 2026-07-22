import { Fragment, useState, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'
import { useApiData } from '../api/useApiData'
import type { Supplier, SupplierContact } from '../api/types'

function SupplierRowDetail({ supplier, onSaved }: { supplier: Supplier; onSaved: () => void }) {
  const { data: contacts, reload: reloadContacts } = useApiData<SupplierContact[]>(
    () => api.get(`/api/suppliers/${supplier.id}/contacts`),
    [supplier.id],
  )

  const [name, setName] = useState(supplier.name)
  const [businessNo, setBusinessNo] = useState(supplier.business_no ?? '')
  const [bankName, setBankName] = useState(supplier.bank_name ?? '')
  const [bankAccountNo, setBankAccountNo] = useState(supplier.bank_account_no ?? '')
  const [bankAccountHolder, setBankAccountHolder] = useState(supplier.bank_account_holder ?? '')
  const [paymentTerms, setPaymentTerms] = useState(supplier.payment_terms ?? '')
  const [isActive, setIsActive] = useState(supplier.is_active)
  const [infoError, setInfoError] = useState<string | null>(null)

  const [contactName, setContactName] = useState('')
  const [contactPhone, setContactPhone] = useState('')
  const [contactEmail, setContactEmail] = useState('')
  const [contactError, setContactError] = useState<string | null>(null)

  const handleSaveInfo = async (e: FormEvent) => {
    e.preventDefault()
    setInfoError(null)
    try {
      await api.patch(`/api/suppliers/${supplier.id}`, {
        name,
        business_no: businessNo || null,
        bank_name: bankName || null,
        bank_account_no: bankAccountNo || null,
        bank_account_holder: bankAccountHolder || null,
        payment_terms: paymentTerms || null,
        is_active: isActive,
      })
      onSaved()
    } catch (err) {
      setInfoError(err instanceof ApiError ? err.message : '공급처 정보 저장 중 오류가 발생했습니다.')
    }
  }

  const handleAddContact = async (e: FormEvent) => {
    e.preventDefault()
    if (!contactName) {
      setContactError('담당자 이름을 입력하세요.')
      return
    }
    setContactError(null)
    try {
      await api.post(`/api/suppliers/${supplier.id}/contacts`, {
        name: contactName,
        phone: contactPhone || null,
        email: contactEmail || null,
      })
      setContactName('')
      setContactPhone('')
      setContactEmail('')
      reloadContacts()
    } catch (err) {
      setContactError(err instanceof ApiError ? err.message : '담당자 등록 중 오류가 발생했습니다.')
    }
  }

  const handleDeleteContact = async (contactId: number) => {
    await api.del(`/api/suppliers/${supplier.id}/contacts/${contactId}`)
    reloadContacts()
  }

  return (
    <tr className="detail-subrow">
      <td colSpan={6}>
        <h3>공급처 정보 수정</h3>
        <form className="inline-form" onSubmit={handleSaveInfo}>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="상호명" />
          <input value={businessNo} onChange={(e) => setBusinessNo(e.target.value)} placeholder="사업자번호" />
          <input value={bankName} onChange={(e) => setBankName(e.target.value)} placeholder="은행명" />
          <input value={bankAccountNo} onChange={(e) => setBankAccountNo(e.target.value)} placeholder="계좌번호" />
          <input
            value={bankAccountHolder}
            onChange={(e) => setBankAccountHolder(e.target.value)}
            placeholder="예금주"
          />
          <input value={paymentTerms} onChange={(e) => setPaymentTerms(e.target.value)} placeholder="결제조건" />
          <label>
            <input type="checkbox" checked={isActive} onChange={(e) => setIsActive(e.target.checked)} /> 사용중
          </label>
          <button type="submit">저장</button>
          {infoError && <span className="form-error">{infoError}</span>}
        </form>

        <h3>담당자</h3>
        <table className="data-table nested">
          <thead>
            <tr>
              <th>이름</th><th>연락처</th><th>이메일</th><th></th>
            </tr>
          </thead>
          <tbody>
            {contacts?.map((c) => (
              <tr key={c.id}>
                <td>{c.name}</td>
                <td>{c.phone ?? '-'}</td>
                <td>{c.email ?? '-'}</td>
                <td>
                  <button type="button" onClick={() => handleDeleteContact(c.id)}>삭제</button>
                </td>
              </tr>
            ))}
            {contacts && contacts.length === 0 && (
              <tr><td colSpan={4}>등록된 담당자가 없습니다.</td></tr>
            )}
          </tbody>
        </table>
        <form className="inline-form" onSubmit={handleAddContact}>
          <input value={contactName} onChange={(e) => setContactName(e.target.value)} placeholder="담당자 이름" />
          <input value={contactPhone} onChange={(e) => setContactPhone(e.target.value)} placeholder="연락처" />
          <input value={contactEmail} onChange={(e) => setContactEmail(e.target.value)} placeholder="이메일" />
          <button type="submit">담당자 추가</button>
        </form>
        {contactError && <p className="form-error">{contactError}</p>}
      </td>
    </tr>
  )
}

export function SuppliersPage() {
  const [activeOnly, setActiveOnly] = useState(false)
  const [expandedId, setExpandedId] = useState<number | null>(null)
  const { data, error, isLoading, reload } = useApiData<Supplier[]>(
    () => api.get(`/api/suppliers${activeOnly ? '?active_only=true' : ''}`),
    [activeOnly],
  )

  const [newName, setNewName] = useState('')
  const [createError, setCreateError] = useState<string | null>(null)

  const handleCreate = async (e: FormEvent) => {
    e.preventDefault()
    if (!newName) {
      setCreateError('공급처명을 입력하세요.')
      return
    }
    setCreateError(null)
    try {
      await api.post('/api/suppliers', { name: newName })
      setNewName('')
      reload()
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.message : '공급처 등록 중 오류가 발생했습니다.')
    }
  }

  return (
    <div>
      <h2>공급처관리</h2>
      <div className="filter-bar">
        <button type="button" className={!activeOnly ? 'active' : ''} onClick={() => setActiveOnly(false)}>전체</button>
        <button type="button" className={activeOnly ? 'active' : ''} onClick={() => setActiveOnly(true)}>사용중만</button>
      </div>

      <form className="inline-form" onSubmit={handleCreate}>
        <input
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          placeholder="신규 공급처명"
          style={{ width: '16rem' }}
        />
        <button type="submit">공급처 등록</button>
        {createError && <span className="form-error">{createError}</span>}
      </form>

      {isLoading && <p>불러오는 중...</p>}
      {error && <p className="form-error">{error}</p>}
      {data && (
        <table className="data-table">
          <thead>
            <tr>
              <th>상호명</th>
              <th>사업자번호</th>
              <th>결제조건</th>
              <th>계좌</th>
              <th>사용여부</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {data.map((s) => (
              <Fragment key={s.id}>
                <tr className={!s.is_active ? 'row-muted' : ''}>
                  <td>{s.name}</td>
                  <td>{s.business_no ?? '-'}</td>
                  <td>{s.payment_terms ?? '-'}</td>
                  <td>{s.bank_name ? `${s.bank_name} ${s.bank_account_no ?? ''}` : '-'}</td>
                  <td>{s.is_active ? 'Y' : 'N'}</td>
                  <td>
                    <button type="button" onClick={() => setExpandedId(expandedId === s.id ? null : s.id)}>
                      {expandedId === s.id ? '닫기' : '관리'}
                    </button>
                  </td>
                </tr>
                {expandedId === s.id && <SupplierRowDetail supplier={s} onSaved={reload} />}
              </Fragment>
            ))}
            {data.length === 0 && <tr><td colSpan={6}>등록된 공급처가 없습니다.</td></tr>}
          </tbody>
        </table>
      )}
    </div>
  )
}
