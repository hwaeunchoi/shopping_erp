// 대시보드 "일별 추이" 등에 쓰는 가벼운 SVG 막대그래프.
// 별도 차트 라이브러리를 추가하지 않고 순수 SVG로 그려 번들 크기를 늘리지 않는다.
// 값이 음수일 수 있으므로(순이익 적자일 등) 0을 기준선으로 위/아래로 막대를 그린다.
export interface BarDatum {
  key: string
  label: string
  value: number
}

interface MiniBarChartProps {
  data: BarDatum[]
  height?: number
  onBarClick?: (datum: BarDatum) => void
  formatValue?: (value: number) => string
}

export function MiniBarChart({ data, height = 180, onBarClick, formatValue }: MiniBarChartProps) {
  if (data.length === 0) return <p className="form-info">표시할 데이터가 없습니다.</p>

  const maxAbs = Math.max(...data.map((d) => Math.abs(d.value)), 1)
  const barGap = 8
  const barWidth = 40
  const width = data.length * (barWidth + barGap) + barGap
  const zeroY = height / 2
  const usableHalf = height / 2 - 20 // 라벨 여백

  const fmt = formatValue ?? ((v: number) => v.toLocaleString())

  return (
    <svg
      viewBox={`0 0 ${width} ${height + 30}`}
      width="100%"
      style={{ maxWidth: width, height: 'auto' }}
      role="img"
      aria-label="일별 추이 막대그래프"
    >
      <line x1={0} y1={zeroY} x2={width} y2={zeroY} stroke="var(--border)" strokeWidth={1} />
      {data.map((d, i) => {
        const barHeight = (Math.abs(d.value) / maxAbs) * usableHalf
        const x = barGap + i * (barWidth + barGap)
        const y = d.value >= 0 ? zeroY - barHeight : zeroY
        const isNegative = d.value < 0
        return (
          <g
            key={d.key}
            onClick={onBarClick ? () => onBarClick(d) : undefined}
            style={{ cursor: onBarClick ? 'pointer' : 'default' }}
          >
            <title>
              {d.label}: {fmt(d.value)}
            </title>
            <rect
              x={x}
              y={y}
              width={barWidth}
              height={Math.max(barHeight, 1)}
              fill={isNegative ? 'var(--danger)' : 'var(--accent, #2563eb)'}
              rx={2}
            />
            <text x={x + barWidth / 2} y={height + 14} textAnchor="middle" fontSize={10} fill="var(--text-muted)">
              {d.label}
            </text>
          </g>
        )
      })}
    </svg>
  )
}
