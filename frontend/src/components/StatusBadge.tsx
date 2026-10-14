import { label, toneOf, TONE_SYMBOL, type LabelDomain } from '../labels'

// 상태를 한국어 문구 + 기호 + 테두리 모양으로 보여 주는 공통 배지.
// 내부 코드(enum)는 화면에 나오지 않고, 색이 없어도(흑백) 상태 종류를 구분할 수 있다.
export function StatusBadge({ domain, value }: { domain: LabelDomain; value: string | null | undefined }) {
  const tone = toneOf(domain, value)
  return (
    <span className={`badge badge-${tone}`}>
      <span aria-hidden="true">{TONE_SYMBOL[tone]}</span>
      {label(domain, value)}
    </span>
  )
}
