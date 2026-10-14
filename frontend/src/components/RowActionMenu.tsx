import { useEffect, useId, useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react'
import { defaultConfirmMessage, nextEnabledIndex, placeMenu, visibleActions, type MenuAction } from '../rowActionMenu'

// 표의 행마다 여러 버튼을 늘어놓는 대신, 가장 자주 쓰는 안전한 작업 1개만 직접 보여 주고
// 나머지는 '더보기' 메뉴에 모은다. 위험 작업은 메뉴 맨 아래에 두고 확인창을 거친다.
// 키보드: Enter/Space/↓ 로 열기, ↑↓ Home End 이동, Enter 실행, Esc 닫기(버튼으로 포커스 복귀), Tab 으로 닫기.
export function RowActionMenu({ label, actions }: { label: string; actions: MenuAction[] }) {
  const items = visibleActions(actions)
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(0)
  const [style, setStyle] = useState<{ left: number; top: number; maxHeight: number } | null>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([])
  const menuId = useId()
  const disabledFlags = items.map((a) => Boolean(a.disabled))

  function close(returnFocus: boolean) {
    setOpen(false)
    if (returnFocus) buttonRef.current?.focus()
  }

  // 열릴 때 화면 안에 들어오는 위치를 계산한다(모바일에서도 잘리지 않음).
  useLayoutEffect(() => {
    if (!open || !buttonRef.current || !menuRef.current) return
    const b = buttonRef.current.getBoundingClientRect()
    const m = menuRef.current.getBoundingClientRect()
    setStyle(placeMenu(b, { width: m.width, height: m.height }, { width: window.innerWidth, height: window.innerHeight }))
  }, [open, items.length])

  useEffect(() => {
    if (!open) return
    itemRefs.current[active]?.focus()
  }, [open, active, style])

  useEffect(() => {
    if (!open) return
    function onMouseDown(e: MouseEvent) {
      const t = e.target as Node
      if (!menuRef.current?.contains(t) && !buttonRef.current?.contains(t)) setOpen(false)
    }
    function onScrollOrResize(e: Event) {
      if (e.target instanceof Node && menuRef.current?.contains(e.target)) return
      setOpen(false)
    }
    document.addEventListener('mousedown', onMouseDown)
    window.addEventListener('resize', onScrollOrResize)
    window.addEventListener('scroll', onScrollOrResize, true)
    return () => {
      document.removeEventListener('mousedown', onMouseDown)
      window.removeEventListener('resize', onScrollOrResize)
      window.removeEventListener('scroll', onScrollOrResize, true)
    }
  }, [open])

  if (items.length === 0) return null

  function openMenu() {
    setStyle(null)
    setActive(Math.max(0, disabledFlags.findIndex((d) => !d)))
    setOpen(true)
  }

  function onButtonKeyDown(e: KeyboardEvent<HTMLButtonElement>) {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      openMenu()
    }
  }

  function onMenuKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    if (e.key === 'Escape') {
      e.preventDefault()
      close(true)
    } else if (e.key === 'Tab') {
      setOpen(false)
    } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) {
      e.preventDefault()
      const next = nextEnabledIndex(active, e.key, disabledFlags)
      if (next >= 0) setActive(next)
    }
  }

  function run(action: MenuAction) {
    if (action.disabled) return
    if (action.danger && !action.skipConfirm && !window.confirm(action.confirmMessage ?? defaultConfirmMessage(action))) {
      close(true)
      return
    }
    close(true)
    action.onSelect()
  }

  const firstDanger = items.findIndex((a) => a.danger)

  return (
    <span className="row-action-menu">
      <button
        ref={buttonRef}
        type="button"
        className="row-action-trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-label={`${label} 더보기`}
        onClick={() => (open ? close(false) : openMenu())}
        onKeyDown={onButtonKeyDown}
      >
        더보기
      </button>
      {open && (
        <div
          ref={menuRef}
          id={menuId}
          role="menu"
          aria-label={`${label} 작업`}
          className="row-action-list"
          style={style ? { left: style.left, top: style.top, maxHeight: style.maxHeight } : { visibility: 'hidden' }}
          onKeyDown={onMenuKeyDown}
        >
          {items.map((a, i) => (
            <div key={a.key}>
              {i === firstDanger && firstDanger > 0 && <div className="row-action-sep" role="separator" />}
              <button
                ref={(el) => {
                  itemRefs.current[i] = el
                }}
                type="button"
                role="menuitem"
                tabIndex={i === active ? 0 : -1}
                aria-disabled={a.disabled || undefined}
                className={'row-action-item' + (a.danger ? ' is-danger' : '') + (a.disabled ? ' is-disabled' : '')}
                onClick={() => run(a)}
              >
                {a.danger && <span aria-hidden="true">✕ </span>}
                {a.label}
                {a.disabled && a.disabledReason && <span className="row-action-reason">{a.disabledReason}</span>}
              </button>
            </div>
          ))}
        </div>
      )}
    </span>
  )
}
