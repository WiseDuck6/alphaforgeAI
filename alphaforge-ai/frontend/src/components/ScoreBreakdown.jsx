import { Bar, scoreTone } from './ui.jsx'
import { EvidenceList } from './Evidence.jsx'

export default function ScoreBreakdown({ components }) {
  const shown = [...components].sort((a, b) => (b.applicable ? b.weight : -1) - (a.applicable ? a.weight : -1))
  return (
    <div className="space-y-4">
      {shown.map((c) => (
        <div key={c.key} className={c.applicable === false ? 'opacity-60' : ''}>
          <div className="mb-1 flex items-baseline justify-between text-sm">
            <span className="font-medium text-slate-800">
              {c.label}{' '}
              <span className="font-normal text-slate-400" title="Weight from your profile, after leaving out components that do not apply">
                · weight {c.applicable === false ? 'n/a' : `${Math.round(c.weight * 100)}%`}
                {c.profile_weight !== undefined && c.applicable !== false && Math.abs(c.profile_weight - c.weight) > 0.005
                  ? ` (profile ${Math.round(c.profile_weight * 100)}%)` : ''}
              </span>
            </span>
            <span className="text-slate-600">
              {c.score === null || c.applicable === false ? 'not scored' : <>{c.score.toFixed(0)}/100 → <b>{c.contribution.toFixed(1)}</b> pts</>}
            </span>
          </div>
          {c.applicable !== false && c.score !== null && <Bar value={c.score} tone={scoreTone(c.score)} />}
          <p className="mt-1.5 text-sm text-slate-600">{c.explanation}</p>
          {c.evidence?.length > 0 && (
            <details className="mt-1">
              <summary className="cursor-pointer text-xs text-indigo-600">Evidence ({c.evidence.length})</summary>
              <div className="mt-1 pl-2"><EvidenceList items={c.evidence} /></div>
            </details>
          )}
        </div>
      ))}
    </div>
  )
}
