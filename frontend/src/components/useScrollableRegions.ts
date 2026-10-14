import { useEffect, type RefObject } from 'react'

// 좁은 화면에서 표가 가로로 스크롤될 때, 키보드 사용자도 방향키로 스크롤할 수 있도록
// 스크롤되는 표·래퍼에 tabindex=0 과 이름을 붙인다(스크롤 영역은 초점을 받을 수 있어야 한다).
// 스크롤이 필요 없으면(넓은 화면) 붙이지 않는다.
const SELECTOR = 'table, .table-scroll, .order-table-scroll'

export function useScrollableRegions(rootRef: RefObject<HTMLElement | null>, deps: unknown[]): void {
  useEffect(() => {
    const root = rootRef.current
    if (!root) return
    function apply() {
      root!.querySelectorAll<HTMLElement>(SELECTOR).forEach((el) => {
        const scrolls = el.scrollWidth > el.clientWidth + 1 && /(auto|scroll)/.test(getComputedStyle(el).overflowX)
        if (scrolls) {
          if (el.getAttribute('tabindex') === null) el.setAttribute('tabindex', '0')
          if (!el.getAttribute('aria-label') && !el.querySelector('caption')) el.setAttribute('aria-label', '표(가로로 스크롤할 수 있습니다)')
        } else if (el.getAttribute('aria-label') === '표(가로로 스크롤할 수 있습니다)') {
          el.removeAttribute('tabindex')
          el.removeAttribute('aria-label')
        }
      })
    }
    apply()
    const mutation = new MutationObserver(apply)
    mutation.observe(root, { childList: true, subtree: true })
    window.addEventListener('resize', apply)
    return () => {
      mutation.disconnect()
      window.removeEventListener('resize', apply)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
}
