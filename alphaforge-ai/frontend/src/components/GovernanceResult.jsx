import { Badge, SEVERITY_TONE } from './ui.jsx'
import { EvidenceList } from './Evidence.jsx'
import { pct } from '../format.js'

const STATUS = {
  conflict: ['red', 'Conflicts with profile'],
  review: ['amber', 'In tension with profile'],
  aligned: ['green', 'Consistent with constraints'],
}

function Stats({ title, s }) {
  if (!s) return null
  return (
    <div className="rounded-lg bg-slate-50 p-3 text-sm">
      <p className="mb-1 font-medium text-slate-700">{title}</p>
      <p>Volatility: <b>{pct(s.volatility, 1)}</b></p>
      <p>Defensive share: <b>{pct(s.defensive_share)}</b></p>
      <p>Crypto share: <b>{pct(s.crypto_share)}</b></p>
      <p>Largest position: <b>{s.largest_position.ticker} {pct(s.largest_position.weight)}</b></p>
    </div>
  )
}

export default function GovernanceResult({ result }) {
  const [tone, text] = STATUS[result.status]
  const fresh = result.flags.filter((f) => f.new)
  const existing = result.flags.filter((f) => !f.new)
  const alt = result.alternatives
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={tone}>{text}</Badge>
        {result.asset_fit && <span className="text-sm text-slate-600">Fit score {result.asset_fit.score}/100</span>}
      </div>
      <ul className="space-y-2">
        {fresh.map((f, i) => (
          <li key={i} className="flex gap-2 text-sm">
            <Badge tone={SEVERITY_TONE[f.severity]}>{f.severity}</Badge>
            <div>
              <span className="text-slate-700">{f.message}</span>
              {f.evidence?.length > 0 && <div className="mt-1"><EvidenceList items={f.evidence} /></div>}
            </div>
          </li>
        ))}
      </ul>
      {existing.length > 0 && (
        <details className="text-sm">
          <summary className="cursor-pointer text-slate-500">{existing.length} issue(s) already present in the current portfolio</summary>
          <ul className="mt-1 list-disc pl-5 text-slate-600">{existing.map((f, i) => <li key={i}>{f.message}</li>)}</ul>
        </details>
      )}
      <div className="grid gap-3 sm:grid-cols-2">
        <Stats title="Before" s={result.before} />
        <Stats title="After (simulated)" s={result.after} />
      </div>
      {alt && result.status !== 'aligned' && (
        <div className="rounded-lg border border-indigo-100 bg-indigo-50 p-3 text-sm text-indigo-900">
          <p className="font-medium">Alternatives</p>
          <p className="mt-1">
            {alt.max_weight_without_new_conflicts > 0
              ? `Largest ${result.proposal.ticker} position without a new hard conflict: ${pct(alt.max_weight_without_new_conflicts, 1)}.`
              : `Any ${result.proposal.ticker} position adds a new conflict for this profile.`}
          </p>
          {alt.warnings_at_max_weight?.length > 0 && (
            <ul className="mt-1 list-disc pl-5">{alt.warnings_at_max_weight.map((w, i) => <li key={i}>{w}</li>)}</ul>
          )}
          {alt.related_funds?.length > 0 && (
            <p className="mt-1">
              Diversified funds with a shared theme: {alt.related_funds.map((f) => `${f.ticker} (fit ${f.fit_score})`).join(', ')}.
            </p>
          )}
          <p className="mt-1 text-xs text-indigo-700">{alt.method}</p>
        </div>
      )}
      <p className="text-xs text-slate-500">{result.notice}</p>
    </div>
  )
}
