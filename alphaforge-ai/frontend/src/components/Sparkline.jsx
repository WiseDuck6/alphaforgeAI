// Small trend line for tables. Green when the last value is at or above the first, red otherwise.
export default function Sparkline({ values, width = 112, height = 32, title }) {
  if (!values || values.length < 2) return <span className="text-xs text-slate-400">n/a</span>
  const pad = 2
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min
  const points = values.map((v, i) => {
    const x = (i / (values.length - 1)) * width
    const y = span === 0 ? height / 2 : pad + (1 - (v - min) / span) * (height - 2 * pad)
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
  const up = values[values.length - 1] >= values[0]
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={title} className="block">
      {title && <title>{title}</title>}
      <polyline points={points} fill="none" stroke={up ? '#059669' : '#e11d48'} strokeWidth="1.5"
        strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  )
}
