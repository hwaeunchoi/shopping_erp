// 행 작업 메뉴(더보기)의 순수 계산 로직. React 에 의존하지 않아 Node 테스트에서 그대로 검증한다.

export interface MenuAction {
  key: string
  label: string
  onSelect: () => void
  /** 삭제·취소·종결·재처리처럼 되돌리기 어려운 작업. 메뉴 맨 아래에 따로 놓고 확인창을 거친다. */
  danger?: boolean
  /** true 면 선택할 수 없고 disabledReason 을 화면에 보여 준다. */
  disabled?: boolean
  disabledReason?: string
  /** true 면 아예 표시하지 않는다(권한 없는 작업). */
  hidden?: boolean
  /** danger 작업을 실행하기 전에 보여 줄 확인 문구. 없으면 기본 문구를 쓴다. */
  confirmMessage?: string
  /** true 면 선택한 처리 함수가 이미 자체 확인창을 띄우므로 메뉴는 확인창을 또 띄우지 않는다. */
  skipConfirm?: boolean
}

/** 숨김 작업을 빼고, 일반 작업 다음에 위험 작업이 오도록 정렬한다(상대 순서는 유지). */
export function visibleActions(actions: MenuAction[]): MenuAction[] {
  const shown = actions.filter((a) => !a.hidden)
  return [...shown.filter((a) => !a.danger), ...shown.filter((a) => a.danger)]
}

export function defaultConfirmMessage(action: MenuAction): string {
  return `'${action.label}' 작업을 진행하시겠습니까? 되돌릴 수 없을 수 있습니다.`
}

/** 방향키·Home·End 로 선택 가능한 다음 항목의 인덱스를 구한다(비활성 항목은 건너뜀). */
export function nextEnabledIndex(current: number, key: string, disabled: boolean[]): number {
  const count = disabled.length
  if (count === 0) return -1
  const enabled = disabled.map((d, i) => (d ? -1 : i)).filter((i) => i >= 0)
  if (enabled.length === 0) return -1
  if (key === 'Home') return enabled[0]
  if (key === 'End') return enabled[enabled.length - 1]
  const step = key === 'ArrowUp' ? -1 : 1
  for (let n = 1; n <= count; n++) {
    const idx = (((current + step * n) % count) + count) % count
    if (!disabled[idx]) return idx
  }
  return enabled[0]
}

export interface Box {
  left: number
  top: number
  right: number
  bottom: number
}

export interface MenuPlacement {
  left: number
  top: number
  maxHeight: number
}

/**
 * 메뉴가 화면 밖으로 나가지 않도록 위치를 정한다. 기본은 버튼 아래, 오른쪽 끝 정렬이며
 * 아래 공간이 부족하고 위쪽이 더 넓으면 위로 연다. 좌우는 viewport 안으로 밀어 넣는다.
 */
export function placeMenu(button: Box, menu: { width: number; height: number }, viewport: { width: number; height: number }, margin = 8): MenuPlacement {
  const width = Math.min(menu.width, viewport.width - margin * 2)
  let left = button.right - width
  left = Math.max(margin, Math.min(left, viewport.width - width - margin))
  const below = viewport.height - button.bottom - margin
  const above = button.top - margin
  const openUp = menu.height > below && above > below
  const available = openUp ? above : below
  const height = Math.min(menu.height, Math.max(available, 80))
  const top = openUp ? Math.max(margin, button.top - height - 4) : Math.min(button.bottom + 4, viewport.height - height - margin)
  return { left, top, maxHeight: Math.max(available, 80) }
}
