export function Card({ title, subtitle, actions, children, className = '' }) {
  return (
    <section className={`rounded-xl border border-slate-200 bg-white p-5 shadow-sm ${className}`}>
      {(title || actions) && (
        <header className="mb-4 flex items-start justify-between gap-4">
          <div>
            {title && <h2 className="text-base font-semibold text-slate-900">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-sm text-slate-500">{subtitle}</p>}
          </div>
          {actions}
        </header>
      )}
      {children}
    </section>
  )
}

const TONES = {
  slate: 'bg-slate-100 text-slate-700',
  green: 'bg-emerald-100 text-emerald-800',
  amber: 'bg-amber-100 text-amber-800',
  red: 'bg-rose-100 text-rose-800',
  blue: 'bg-indigo-100 text-indigo-800',
}

export function Badge({ tone = 'slate', children, title }) {
  return (
    <span title={title} className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${TONES[tone]}`}>
      {children}
    </span>
  )
}

export function scoreTone(score) {
  if (score >= 70) return 'green'
  if (score >= 45) return 'amber'
  return 'red'
}

export function ScoreBadge({ score, size = 'md' }) {
  const colors = { green: 'bg-emerald-600', amber: 'bg-amber-500', red: 'bg-rose-600' }
  const dims = size === 'lg' ? 'h-16 w-16 text-2xl' : 'h-9 w-9 text-sm'
  return (
    <div title="Profile-fit score (0-100)" className={`${dims} ${colors[scoreTone(score)]} flex shrink-0 items-center justify-center rounded-full font-bold text-white`}>
      {score}
    </div>
  )
}

const STATUS = {
  ok: ['green', 'Fits profile'],
  caution: ['amber', 'Outside risk profile'],
  cap_reached: ['blue', 'At cap'],
  over_cap: ['red', 'Over cap'],
  excluded: ['red', 'Excluded'],
}

export function StatusBadge({ status }) {
  const [tone, text] = STATUS[status] || ['slate', status]
  return <Badge tone={tone}>{text}</Badge>
}

const VERDICT_TONE = { strong_fit: 'green', good_fit: 'green', partial_fit: 'amber', poor_fit: 'red', outside_profile: 'red', excluded: 'slate' }

export function VerdictBadge({ verdict }) {
  if (!verdict) return null
  return <Badge tone={VERDICT_TONE[verdict.key] || 'slate'}>{verdict.label}</Badge>
}

// How often an asset stays in the top five when the profile weights are perturbed.
export function StabilityBadge({ s }) {
  if (!s) return null
  const p = s.p_top5
  const tone = p >= 0.8 ? 'green' : p >= 0.3 ? 'amber' : 'slate'
  return (
    <span title={`Median rank ${s.median_rank}; 80% of weight variations rank it between ${s.rank_p10} and ${s.rank_p90}.`}>
      <Badge tone={tone}>top 5 in {Math.round(p * 100)}%</Badge>
    </span>
  )
}

export const SEVERITY_TONE = { high: 'red', conflict: 'red', medium: 'amber', warning: 'amber', low: 'blue', info: 'slate' }

export function Bar({ value, max = 100, tone = 'blue', target }) {
  const colors = { blue: 'bg-indigo-500', green: 'bg-emerald-500', amber: 'bg-amber-500', red: 'bg-rose-500' }
  const w = Math.max(0, Math.min(100, ((value ?? 0) / max) * 100))
  return (
    <div className="relative h-2 w-full rounded-full bg-slate-100">
      <div className={`h-2 rounded-full ${colors[tone]}`} style={{ width: `${w}%` }} />
      {target !== undefined && target !== null && (
        <div title={`Target ${target}`} className="absolute -top-1 h-4 w-0.5 bg-slate-700" style={{ left: `${Math.min(100, (target / max) * 100)}%` }} />
      )}
    </div>
  )
}

export function Spinner({ text = 'Loading...' }) {
  return <p className="animate-pulse text-sm text-slate-500">{text}</p>
}

export function ErrorBox({ error }) {
  if (!error) return null
  return <div className="rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">{String(error.message || error)}</div>
}

export function Collapsible({ title, children, defaultOpen = false }) {
  return (
    <details open={defaultOpen} className="group rounded-lg border border-slate-200 bg-white">
      <summary className="cursor-pointer select-none px-4 py-2 text-sm font-medium text-slate-700">{title}</summary>
      <div className="border-t border-slate-100 px-4 py-3 text-sm text-slate-600">{children}</div>
    </details>
  )
}
