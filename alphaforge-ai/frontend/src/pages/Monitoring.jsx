import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { useApi, useUser } from '../UserContext.jsx'
import { Badge, Card, ErrorBox, Spinner } from '../components/ui.jsx'

const RATE_LABELS = { off: 'Off', '15m': 'Every 15 min', '1h': 'Hourly', '6h': 'Every 6 hours', '1d': 'Daily' }

function AlertSettings() {
  const { userId, refresh } = useUser()
  const [profile, setProfile] = useState(null)
  const [msg, setMsg] = useState(null)
  const [error, setError] = useState(null)
  useEffect(() => { api.profile().then((r) => setProfile(r.profile)).catch(setError) }, [userId])
  if (error) return <ErrorBox error={error} />
  if (!profile) return <Spinner />
  const s = profile.settings
  const setS = (k, v) => setProfile({ ...profile, settings: { ...s, [k]: v } })
  const save = () => api.saveProfile(profile).then(() => { setMsg('Saved.'); refresh() }).catch(setError)
  return (
    <Card title="Your alerts" subtitle="In-app alerts for signals that touch your holdings. Email delivery uses Amazon SNS once deployed.">
      <label className="block text-sm">Minimum severity: <b>{s.alert_threshold}</b>
        <input type="range" min="1" max="5" value={s.alert_threshold} onChange={(e) => setS('alert_threshold', Number(e.target.value))} className="ml-3 align-middle" />
      </label>
      <label className="mt-3 flex items-center gap-2 text-sm">
        <input type="checkbox" checked={s.email_alerts} onChange={(e) => setS('email_alerts', e.target.checked)} /> Email me high-severity alerts
      </label>
      {s.email_alerts && <input type="email" value={s.email || ''} onChange={(e) => setS('email', e.target.value)} placeholder="you@example.com"
        className="mt-2 w-72 rounded-lg border border-slate-300 px-2 py-1.5 text-sm" />}
      <div className="mt-3 flex items-center gap-3">
        <button onClick={save} className="rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white">Save</button>
        {msg && <span className="text-sm text-emerald-700">{msg}</span>}
      </div>
    </Card>
  )
}

export default function Monitoring() {
  const { data, error, loading } = useApi(() => api.sources(), [])
  const [sources, setSources] = useState([])
  const [feedback, setFeedback] = useState({})
  const [err, setErr] = useState(null)
  useEffect(() => { if (data) setSources(data.sources) }, [data])

  const change = async (id, rate) => {
    setErr(null)
    try {
      const res = await api.setSourceRate(id, rate)
      setSources((list) => list.map((s) => (s.source_id === id ? { ...s, rate, last_changed: new Date().toISOString() } : s)))
      setFeedback((f) => ({ ...f, [id]: res.scheduler.applied ? `EventBridge schedule updated (${res.scheduler.expression}, ${res.scheduler.state})` : res.scheduler.reason }))
    } catch (e) { setErr(e) }
  }

  if (error) return <ErrorBox error={error} />
  if (loading && !data) return <Spinner />
  return (
    <div className="space-y-6">
      <Card title="Monitoring sources" subtitle="How often each public source is checked for events, statements and filings. Each source maps to one EventBridge Scheduler schedule.">
        <ErrorBox error={err} />
        <div className="divide-y divide-slate-100">
          {sources.map((s) => (
            <div key={s.source_id} className="flex flex-wrap items-start justify-between gap-4 py-3">
              <div className="max-w-xl">
                <p className="font-medium text-slate-900">{s.name} <Badge>{s.category}</Badge></p>
                <p className="text-sm text-slate-600">{s.description}</p>
                <p className="mt-1 text-xs text-slate-400">Access: {s.access} · <a className="underline" href={s.url} target="_blank" rel="noreferrer">docs</a> · status: {s.last_status}
                  {s.last_run ? ` · last run ${s.last_run.replace('T', ' ').slice(0, 16)} UTC` : ''}</p>
                {s.last_summary && (
                  <p className="mt-0.5 font-mono text-[11px] text-slate-400">
                    {Object.entries(s.last_summary).filter(([, v]) => typeof v !== 'object').slice(0, 6).map(([k, v]) => `${k}: ${v}`).join(' · ')}
                  </p>
                )}
                {feedback[s.source_id] && <p className="mt-1 text-xs text-indigo-700">{feedback[s.source_id]}</p>}
              </div>
              <select value={s.rate} onChange={(e) => change(s.source_id, e.target.value)} className="rounded-lg border border-slate-300 px-2 py-1.5 text-sm">
                {s.rate_options.map((r) => <option key={r} value={r}>{RATE_LABELS[r] || r}</option>)}
              </select>
            </div>
          ))}
        </div>
        <p className="mt-3 text-xs text-slate-500">
          Reliable sources only: regulators, central banks, the starter data's price source and allow-listed news outlets.
          Social media, forums, blogs and investment newsletters are not ingested. {data.signals_loaded} items loaded
          {data.data_through?.live ? `; market data through ${data.data_through.live}` : ''}.
          Locally, run <code>python -m alphaforge.ingest.run --bundle ../data_pipeline/out</code> from backend/.
        </p>
      </Card>
      <AlertSettings />
    </div>
  )
}
