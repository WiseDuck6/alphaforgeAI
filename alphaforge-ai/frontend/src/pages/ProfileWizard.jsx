import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { useApi, useUser } from '../UserContext.jsx'
import { Badge, Card, ErrorBox, Spinner } from '../components/ui.jsx'
import { RISK_LABELS, label, money } from '../format.js'

const STEPS = ['Start', 'Risk & horizon', 'Preferences', 'Holdings', 'Review']

const BLANK = {
  display_name: 'Me', age: 35, risk_level: 'moderate', horizon_years: 10, objective: '',
  interests: [], avoid_sectors: [], esg_exclusions: [], crypto: 'limited', holdings: [],
  settings: { alert_threshold: 4, email_alerts: false, email: null },
}

// Simple, transparent questionnaire. A heuristic for the simulation, not a regulatory KYC process.
const QUESTIONS = [
  ['If your portfolio fell 25% in one year, you would...', ['Sell everything', 'Sell some', 'Hold', 'Buy more']],
  ['Your main goal is...', ['Preserve capital', 'Income with some growth', 'Balanced growth', 'Maximum long-term growth']],
  ['Your investing experience is...', ['None', 'Intermediate', 'Advanced']],
]

function suggestedRisk(answers, horizon) {
  const h = horizon <= 5 ? 0 : horizon <= 12 ? 1.5 : 3
  const total = answers.reduce((s, a) => s + (a ?? 0), 0) + h
  if (total <= 3) return 'low'
  if (total <= 5.5) return 'low_moderate'
  if (total <= 8) return 'moderate'
  return 'high'
}

function Chip({ on, onClick, children }) {
  return (
    <button type="button" onClick={onClick}
      className={`rounded-full border px-3 py-1 text-xs ${on ? 'border-indigo-600 bg-indigo-600 text-white' : 'border-slate-300 text-slate-700 hover:border-indigo-400'}`}>
      {children}
    </button>
  )
}

const toggle = (list, x) => (list.includes(x) ? list.filter((y) => y !== x) : [...list, x])

export default function ProfileWizard() {
  const { meta, refresh, userId } = useUser()
  const navigate = useNavigate()
  const presets = useApi(() => api.presets(), [])
  const [step, setStep] = useState(0)
  const [p, setP] = useState(BLANK)
  const [answers, setAnswers] = useState([null, null, null])
  const [error, setError] = useState(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    setStep(0)
    api.profile().then((r) => setP(r.profile)).catch(() => setP(BLANK))
  }, [userId])

  const set = (k, v) => setP((x) => ({ ...x, [k]: v }))
  const sectors = useMemo(() => [...new Set((meta?.assets || []).filter((a) => a.asset_group === 'equity').map((a) => a.sector))].sort(), [meta])
  const total = p.holdings.reduce((s, h) => s + Number(h.amount || 0), 0)
  const suggestion = answers.every((a) => a !== null) ? suggestedRisk(answers, p.horizon_years) : null

  const applyPreset = async (id) => {
    setError(null)
    try {
      await api.loadPreset(id)
      refresh()
      navigate('/')
    } catch (e) { setError(e) }
  }

  const save = async () => {
    setSaving(true)
    setError(null)
    try {
      await api.saveProfile({ ...p, horizon_years: Number(p.horizon_years), age: Number(p.age),
        holdings: p.holdings.map((h) => ({ ticker: h.ticker, amount: Number(h.amount) })) })
      refresh()
      navigate('/')
    } catch (e) { setError(e) } finally { setSaving(false) }
  }

  if (!meta) return <Spinner />

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <ol className="flex flex-wrap gap-2 text-sm">
        {STEPS.map((s, i) => (
          <li key={s}><button onClick={() => setStep(i)}
            className={`rounded-full px-3 py-1 ${i === step ? 'bg-indigo-600 text-white' : 'bg-white text-slate-600 ring-1 ring-slate-200'}`}>{i + 1}. {s}</button></li>
        ))}
      </ol>
      <ErrorBox error={error} />

      {step === 0 && (
        <Card title="Start from a persona or from scratch" subtitle="The three personas come from the challenge brief. All profiles and portfolios are fictional.">
          <div className="grid gap-3 sm:grid-cols-3">
            {(presets.data || []).map((x) => (
              <div key={x.id} className="flex flex-col rounded-lg border border-slate-200 p-4">
                <p className="font-semibold text-slate-900">{x.display_name}, {x.age}</p>
                <div className="mt-1 flex flex-wrap gap-1"><Badge tone="blue">{RISK_LABELS[x.risk_level]}</Badge><Badge>{x.horizon_years} years</Badge></div>
                <p className="mt-2 text-xs text-slate-600">{x.objective}</p>
                <p className="mt-2 text-xs italic text-slate-500">Tests: {x.brief_test}</p>
                <button onClick={() => applyPreset(x.id)} className="mt-auto pt-3 text-left text-sm font-medium text-indigo-600">Load {x.display_name} as my profile →</button>
              </div>
            ))}
          </div>
          <button onClick={() => { setP(BLANK); setStep(1) }} className="mt-4 rounded-lg border border-slate-300 px-4 py-2 text-sm">Start from scratch</button>
        </Card>
      )}

      {step === 1 && (
        <Card title="Risk and horizon">
          <div className="grid gap-4 sm:grid-cols-3">
            <label className="text-sm">Name<input value={p.display_name} onChange={(e) => set('display_name', e.target.value)} className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1.5" /></label>
            <label className="text-sm">Age<input type="number" value={p.age ?? ''} onChange={(e) => set('age', e.target.value)} className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1.5" /></label>
            <label className="text-sm">Horizon (years)<input type="number" min="1" max="50" value={p.horizon_years} onChange={(e) => set('horizon_years', e.target.value)} className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1.5" /></label>
          </div>
          <div className="mt-5 space-y-4">
            {QUESTIONS.map(([q, opts], qi) => (
              <div key={q}>
                <p className="text-sm font-medium text-slate-700">{q}</p>
                <div className="mt-1 flex flex-wrap gap-2">
                  {opts.map((o, oi) => <Chip key={o} on={answers[qi] === oi} onClick={() => setAnswers((a) => a.map((x, i) => (i === qi ? oi : x)))}>{o}</Chip>)}
                </div>
              </div>
            ))}
          </div>
          {suggestion && <p className="mt-4 text-sm text-slate-600">Suggested risk level from your answers and horizon: <b>{RISK_LABELS[suggestion]}</b>.{' '}
            {suggestion !== p.risk_level && <button className="text-indigo-600 underline" onClick={() => set('risk_level', suggestion)}>Use it</button>}</p>}
          <label className="mt-4 block text-sm">Risk level (you decide)
            <select value={p.risk_level} onChange={(e) => set('risk_level', e.target.value)} className="mt-1 block rounded-lg border border-slate-300 px-2 py-1.5">
              {Object.entries(RISK_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          </label>
          <label className="mt-4 block text-sm">Objective (free text)
            <textarea value={p.objective} onChange={(e) => set('objective', e.target.value)} rows={2} maxLength={300} className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1.5" />
          </label>
        </Card>
      )}

      {step === 2 && (
        <Card title="Preferences">
          <p className="text-sm font-medium text-slate-700">Interests</p>
          <div className="mt-1 flex flex-wrap gap-2">{meta.interest_tags.map((t) => <Chip key={t} on={p.interests.includes(t)} onClick={() => set('interests', toggle(p.interests, t))}>{label(t)}</Chip>)}</div>
          <p className="mt-4 text-sm font-medium text-slate-700">ESG exclusions</p>
          <div className="mt-1 flex flex-wrap gap-2">{meta.esg_exclusions.map((t) => <Chip key={t} on={p.esg_exclusions.includes(t)} onClick={() => set('esg_exclusions', toggle(p.esg_exclusions, t))}>Exclude {label(t)}</Chip>)}</div>
          <p className="mt-1 text-xs text-slate-500">Screens use sector-based flags documented on the Data page, not third-party ESG ratings.</p>
          <p className="mt-4 text-sm font-medium text-slate-700">Crypto</p>
          <div className="mt-1 flex gap-2">{meta.crypto_prefs.map((c) => <Chip key={c} on={p.crypto === c} onClick={() => set('crypto', c)}>{c}</Chip>)}</div>
          <p className="mt-4 text-sm font-medium text-slate-700">Sectors to avoid</p>
          <div className="mt-1 flex flex-wrap gap-2">{sectors.map((s) => <Chip key={s} on={p.avoid_sectors.includes(s)} onClick={() => set('avoid_sectors', toggle(p.avoid_sectors, s))}>{s}</Chip>)}</div>
        </Card>
      )}

      {step === 3 && (
        <Card title="Fictional holdings" subtitle="Amounts in CAD. Weights are computed from the amounts.">
          <div className="space-y-2">
            {p.holdings.map((h, i) => (
              <div key={i} className="flex gap-2">
                <select value={h.ticker} onChange={(e) => set('holdings', p.holdings.map((x, j) => (j === i ? { ...x, ticker: e.target.value } : x)))}
                  className="flex-1 rounded-lg border border-slate-300 px-2 py-1.5 text-sm">
                  {meta.assets.map((a) => <option key={a.ticker} value={a.ticker}>{a.ticker} · {a.name}</option>)}
                </select>
                <input type="number" min="0" value={h.amount} onChange={(e) => set('holdings', p.holdings.map((x, j) => (j === i ? { ...x, amount: e.target.value } : x)))}
                  className="w-32 rounded-lg border border-slate-300 px-2 py-1.5 text-sm" />
                <span className="w-12 self-center text-right text-xs text-slate-500">{total ? `${Math.round((Number(h.amount) / total) * 100)}%` : ''}</span>
                <button onClick={() => set('holdings', p.holdings.filter((_, j) => j !== i))} className="px-2 text-slate-400 hover:text-rose-600" aria-label="Remove">✕</button>
              </div>
            ))}
          </div>
          <button onClick={() => set('holdings', [...p.holdings, { ticker: meta.assets[0].ticker, amount: 1000 }])} className="mt-3 rounded-lg border border-slate-300 px-3 py-1.5 text-sm">+ Add holding</button>
          <p className="mt-3 text-sm text-slate-600">Total: <b>{money(total)}</b></p>
        </Card>
      )}

      {step === 4 && (
        <Card title="Review">
          <dl className="grid gap-2 text-sm sm:grid-cols-2">
            <div><dt className="text-slate-500">Name / age</dt><dd>{p.display_name}, {p.age}</dd></div>
            <div><dt className="text-slate-500">Risk / horizon</dt><dd>{RISK_LABELS[p.risk_level]}, {p.horizon_years} years</dd></div>
            <div><dt className="text-slate-500">Interests</dt><dd>{p.interests.map(label).join(', ') || 'none'}</dd></div>
            <div><dt className="text-slate-500">ESG exclusions</dt><dd>{p.esg_exclusions.map(label).join(', ') || 'none'}</dd></div>
            <div><dt className="text-slate-500">Crypto</dt><dd>{p.crypto}</dd></div>
            <div><dt className="text-slate-500">Holdings</dt><dd>{p.holdings.length} positions, {money(total)}</dd></div>
          </dl>
          <button onClick={save} disabled={saving} className="mt-4 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50">
            {saving ? 'Saving...' : 'Save profile and open dashboard'}
          </button>
        </Card>
      )}

      {step > 0 && step < 4 && (
        <div className="flex justify-between">
          <button onClick={() => setStep(step - 1)} className="text-sm text-slate-600">← Back</button>
          <button onClick={() => setStep(step + 1)} className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white">Next →</button>
        </div>
      )}
    </div>
  )
}
