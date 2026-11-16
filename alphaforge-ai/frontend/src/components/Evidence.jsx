import { byUnit } from '../format.js'
import { Badge } from './ui.jsx'

// Legacy evidence (no unit): ratios shown as percentages, correlations, counts and sentiment as numbers.
const RAW_CLAIM = /correlation|effective|sentiment|score|days|sharpe|sortino|beta|ratio|p\/e|rsi/i

function formatValue(v, claim, unit) {
  if (unit) return byUnit(v, unit)
  if (v === null || v === undefined) return 'n/a'
  if (Array.isArray(v)) return v.length ? v.join(', ') : 'none'
  if (typeof v === 'number') {
    if (RAW_CLAIM.test(claim || '') || Number.isInteger(v) || Math.abs(v) > 1.5) return String(v)
    return `${(v * 100).toFixed(1)}%`
  }
  return String(v)
}

const KIND_TONE = { news: 'blue', filing: 'slate', macro: 'amber', official: 'amber' }

function Source({ id }) {
  if (!id) return null
  if (id.startsWith('http')) {
    let host = id
    try { host = new URL(id).hostname.replace(/^www\./, '') } catch { /* keep raw */ }
    return <a className="underline" href={id} target="_blank" rel="noreferrer">{host}</a>
  }
  return <span>{id}</span>
}

export function EvidenceList({ items, highlight }) {
  if (!items?.length) return <p className="text-sm text-slate-500">No evidence items.</p>
  return (
    <ul className="space-y-1.5">
      {items.map((e, i) => (
        <li key={e.ref ?? i} id={e.ref ? `ev-${e.ref}` : undefined}
          className={`rounded text-sm ${highlight && highlight === e.ref ? 'bg-amber-50 ring-1 ring-amber-300' : ''}`}>
          {e.ref && <span className="mr-1.5 font-mono text-xs text-indigo-600">[{e.ref}]</span>}
          {e.kind && e.kind !== 'data' && <span className="mr-1.5"><Badge tone={KIND_TONE[e.kind] || 'slate'}>{e.kind}</Badge></span>}
          <span className="text-slate-700">{e.claim}: </span>
          <span className="font-medium text-slate-900">{formatValue(e.value, e.claim, e.unit)}</span>
          <span className="ml-2 font-mono text-xs text-slate-400">
            <Source id={e.source_id} />{e.period ? ` · ${e.period}` : ''}
          </span>
        </li>
      ))}
    </ul>
  )
}

export function BulletList({ items, empty = 'None.' }) {
  if (!items?.length) return <p className="text-sm text-slate-500">{empty}</p>
  return (
    <ul className="list-disc space-y-1 pl-5 text-sm text-slate-700">
      {items.map((x, i) => <li key={i}>{x}</li>)}
    </ul>
  )
}

// Text with [n] citation markers turned into buttons that point at the evidence list.
export function CitedText({ text, onCite, className = '' }) {
  const parts = String(text || '').split(/(\[\d+\])/g)
  return (
    <span className={className}>
      {parts.map((p, i) => {
        const m = p.match(/^\[(\d+)\]$/)
        if (!m) return <span key={i}>{p}</span>
        return (
          <button key={i} type="button" onClick={() => onCite?.(Number(m[1]))}
            className="mx-px rounded bg-indigo-50 px-1 font-mono text-[10px] text-indigo-700 align-super hover:bg-indigo-100">
            {m[1]}
          </button>
        )
      })}
    </span>
  )
}

export function NewsList({ items, onCite, empty = 'No items in the sources searched.' }) {
  if (!items?.length) return <p className="text-sm text-slate-500">{empty}</p>
  return (
    <ul className="space-y-2">
      {items.map((s, i) => (
        <li key={s.signal_id || i} className="text-sm">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="font-mono text-xs text-slate-500">{(s.published_at || '').slice(0, 10)}</span>
            <Badge tone={KIND_TONE[s.kind] || 'slate'}>{s.kind === 'news' ? s.publisher : s.kind}</Badge>
            {s.event_type && s.event_type !== 'general' && <Badge>{s.event_type.replaceAll('_', ' ')}</Badge>}
            {s.materiality === 'high' && <Badge tone="red">material</Badge>}
            {s.kind === 'news' && typeof s.sentiment === 'number' && (
              <Badge tone={s.sentiment > 0.15 ? 'green' : s.sentiment < -0.15 ? 'red' : 'slate'}>
                sentiment {s.sentiment > 0 ? '+' : ''}{s.sentiment.toFixed(2)}
              </Badge>
            )}
            {s.linked_move && <Badge tone="amber">near a {(s.linked_move.return * 100).toFixed(1)}% move on {s.linked_move.date}</Badge>}
            {s.ref && onCite && (
              <button type="button" onClick={() => onCite(s.ref)} className="font-mono text-[10px] text-indigo-700">[{s.ref}]</button>
            )}
          </div>
          <p className="mt-0.5 text-slate-800">
            {s.source_url ? <a href={s.source_url} target="_blank" rel="noreferrer" className="hover:underline">{s.title}</a> : s.title}
          </p>
        </li>
      ))}
    </ul>
  )
}
