interface PaginationProps {
  page: number
  pageSize: number
  total: number
  onPageChange: (page: number) => void
}

// 배송/교환/반품/취소 관리 화면에서 공통으로 쓰는 페이지네이션 컨트롤.
export function Pagination({ page, pageSize, total, onPageChange }: PaginationProps) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize))

  return (
    <div className="pagination">
      <button type="button" disabled={page <= 1} onClick={() => onPageChange(page - 1)}>
        ← 이전
      </button>
      <span>
        {page} / {totalPages} 페이지 (총 {total}건)
      </span>
      <button type="button" disabled={page >= totalPages} onClick={() => onPageChange(page + 1)}>
        다음 →
      </button>
    </div>
  )
}
