import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useApi, useUser } from '../UserContext.jsx'
import PortfolioRadar from '../components/PortfolioRadar.jsx'
import { Badge, Bar, Card, Collapsible, ErrorBox, ScoreBadge, SEVERITY_TONE, Spinner, StabilityBadge, VerdictBadge } from '../components/ui.jsx'
import { BulletList, EvidenceList } from '../components/Evidence.jsx'
import { RISK_LABELS, label, money, num, pct, signedPct } from '../format.js'

function Controls({ windowKey, setWindow, risk, setRisk, meta }) {
  return (
    <div className="flex flex-wrap items-center gap-3 text-sm">
      <label className="flex items-center gap-2 text-slate-600">
        Data window
        <select value={windowKey} onChange={(e) => setWindow(e.target.value)} className="rounded-lg border border-slate-300 px-2 py-1">
          <option value="">Default</option>
          {Object.entries(meta?.windows || {}).map(([k, v]) => <option key={k} value={k}>{k} ({v})</option>)}
        </select>
      </label>
      <label className="flex items-center gap-2 text-slate-600" title="Recompute everything as if the risk level were different">
        What-if risk level
        <select value={risk} onChange={(e) => setRisk(e.target.value)} className="rounded-lg border border-slate-300 px-2 py-1">
          <option value="">As in profile</option>
          {Object.entries(RISK_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </label>
    </div>
  )
}

function AxisList({ axes }) {
  return (
    <div className="grid gap-x-8 gap-y-3 sm:grid-cols-2">
      {axes.map((a) => {
        const off = a.gap !== null && Math.abs(a.gap) > 15
        return (
          <div key={a.key} title={a.description}>
            <div className="flex justify-between text-xs">
              <span className="font-medium text-slate-700">{a.label}</span>
              <span className={off ? 'font-medium text-amber-700' : 'text-slate-500'}>
                {a.value === null ? 'n/a' : num(a.value)}{a.target !== null ? ` / target ${num(a.target)}` : ''}
              </span>
            </div>
            <div className="mt-1"><Bar value={a.value} target={a.target} tone={off ? 'amber' : 'blue'} /></div>
            <p className="mt-1 text-xs text-slate-500">{a.explanation}</p>
          </div>
        )
      })}
    </div>
  )
}

function Suggestion({ s }) {
  return (
    <div className="rounded-lg border border-slate-200 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={SEVERITY_TONE[s.severity]}>{s.severity}</Badge>
        <Badge>{s.kind}</Badge>
        <h3 className="text-sm font-semibold text-slate-900">{s.title}</h3>
      </div>
      <p className="mt-2 text-sm text-slate-600">{s.detail}</p>
      {s.candidates.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2">
          {s.candidates.map((c) => (
            <Link key={c.ticker} to={`/assets/${c.ticker}`} className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs hover:border-indigo-300">
              <span className="font-semibold text-slate-900">{c.ticker}</span>
              <span className="text-slate-500"> · fit {c.fit_score}</span>
              {c.impact && c.impact.from !== null && c.impact.to !== null && (
                <span className="block text-slate-600" title={c.impact.assumption}>
                  {label(c.impact.axis)} {num(c.impact.from)} → {num(c.impact.to)}
                </span>
              )}
            </Link>
          ))}
        </div>
      )}
      {s.evidence?.length > 0 && (
        <details className="mt-2"><summary className="cursor-pointer text-xs text-indigo-600">Evidence</summary>
          <div className="mt-1"><EvidenceList items={s.evidence} /></div></details>
      )}
    </div>
  )
}

function TopPick({ p }) {
  return (
    <div className="flex flex-col rounded-lg border border-slate-200 p-4">
      <div className="flex items-center gap-3">
        <ScoreBadge score={p.score} />
        <div className="min-w-0">
          <Link to={`/assets/${p.ticker}`} className="font-semibold text-slate-900 hover:text-indigo-700">{p.ticker}</Link>
          <p className="truncate text-xs text-slate-500">{p.name}</p>
        </div>
      </div>
      <div className="mt-2 flex flex-wrap gap-1"><VerdictBadge verdict={p.verdict} /><StabilityBadge s={p.rank_stability} /></div>
      <p className="mt-3 text-xs font-medium uppercase tracking-wide text-slate-400">Why</p>
      <BulletList items={p.why} />
      <p className="mt-2 text-xs font-medium uppercase tracking-wide text-slate-400">What could go wrong</p>
      <BulletList items={p.what_could_go_wrong} />
      <p className="mt-auto pt-2 text-xs text-slate-400">Confidence: {p.confidence.level}{p.current_weight > 0 ? ` · you hold ${pct(p.current_weight)}` : ''}</p>
    </div>
  )
}

export default function Dashboard() {
  const { meta, userId } = useUser()
  const [windowKey, setWindow] = useState('')
  const [risk, setRisk] = useState('')
  const { data, error, loading } = useApi(() => api.dashboard({ window: windowKey, risk_level: risk }), [windowKey, risk, userId])

  if (error?.status === 404) {
    return (
      <Card title="No investor profile yet">
        <p className="text-sm text-slate-600">Create a fictional profile, or load one of the three personas from the brief.</p>
        <Link to="/profile" className="mt-3 inline-block rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white">Set up profile</Link>
      </Card>
    )
  }
  if (error) return <ErrorBox error={error} />
  if (loading && !data) return <Spinner />

  const p = data.profile
  const stats = data.radar.stats
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-900">{p.display_name}&apos;s dashboard</h1>
          <div className="mt-2 flex flex-wrap gap-2">
            <Badge tone="blue">Risk: {RISK_LABELS[p.risk_level]}</Badge>
            <Badge>Horizon: {p.horizon_years} years</Badge>
            {p.esg_exclusions.length > 0 && <Badge tone="green">ESG screen: {p.esg_exclusions.map(label).join(', ')}</Badge>}
            <Badge>Crypto: {p.crypto}</Badge>
            <Badge>Fictional portfolio: {money(data.portfolio_value)}</Badge>
          </div>
          {p.objective && <p className="mt-2 max-w-3xl text-sm text-slate-500">{p.objective}</p>}
        </div>
        <Controls windowKey={windowKey} setWindow={setWindow} risk={risk} setRisk={setRisk} meta={meta} />
      </div>

      {data.what_if && (
        <div className="rounded-lg border border-indigo-200 bg-indigo-50 px-4 py-2 text-sm text-indigo-900">
          What-if mode: results recomputed as if the risk level were <b>{RISK_LABELS[data.what_if.risk_level]}</b>. The saved profile is unchanged.
        </div>
      )}
      {data.window_note && <div className="rounded-lg bg-slate-100 px-4 py-2 text-sm text-slate-600">{data.window_note}</div>}

      <div className="grid gap-6 lg:grid-cols-3">
        <Card title="Portfolio radar" subtitle={`Your portfolio vs the target for your profile · ${data.period}`} className="lg:col-span-2">
          {data.radar.has_holdings ? (
            <>
              <PortfolioRadar axes={data.radar.axes} />
              <div className="mt-4"><AxisList axes={data.radar.axes} /></div>
            </>
          ) : <p className="text-sm text-slate-500">Add holdings in your profile to see the radar.</p>}
        </Card>

        <div className="space-y-6">
          <Card title="Key figures">
            {stats ? (
              <dl className="grid grid-cols-2 gap-3 text-sm">
                <div><dt className="text-slate-500">Est. volatility</dt><dd className="text-lg font-semibold">{pct(stats.volatility, 1)}</dd></div>
                <div><dt className="text-slate-500">Effective positions</dt><dd className="text-lg font-semibold">{num(stats.effective_n, 1)}</dd></div>
                <div><dt className="text-slate-500">Top-3 share</dt><dd className="text-lg font-semibold">{pct(stats.top3_share)}</dd></div>
                <div><dt className="text-slate-500">Avg. correlation</dt><dd className="text-lg font-semibold">{num(stats.avg_pairwise_corr, 2)}</dd></div>
                <div className="col-span-2">
                  <dt className="text-slate-500">Largest risk contributors</dt>
                  <dd>{stats.risk_contributions.slice(0, 3).map((r) => `${r.ticker} ${pct(r.share_of_risk)}`).join(' · ')}</dd>
                </div>
              </dl>
            ) : <p className="text-sm text-slate-500">No holdings.</p>}
          </Card>

          <Card title="Scenario replay" subtitle={data.stress.label}>
            {data.stress.available ? (
              <>
                <p className="text-2xl font-semibold">{signedPct(data.stress.portfolio_return_on_covered, 1)}</p>
                <p className="text-sm text-slate-500">
                  {data.stress.period}, CAD, on {pct(data.stress.coverage)} of holdings with data.
                  {data.stress.missing.length > 0 && ` No data: ${data.stress.missing.join(', ')}.`}
                </p>
                <p className="mt-2 text-xs text-slate-400">One historical path, not a forecast.</p>
              </>
            ) : <p className="text-sm text-slate-500">Scenario data not available for these holdings.</p>}
          </Card>

          <Card title="Alerts" subtitle={`Severity ${data.alerts.threshold}+ on your holdings, last ${data.alerts.lookback_days} days`}>
            {data.alerts.items.length ? (
              <ul className="space-y-2 text-sm">
                {data.alerts.items.map((a) => (
                  <li key={a.signal_id}><Badge tone="red">sev {a.severity}</Badge> {a.title}{' '}
                    {a.source_url && <a className="text-indigo-600 underline" href={a.source_url} target="_blank" rel="noreferrer">source</a>}</li>
                ))}
              </ul>
            ) : <p className="text-sm text-slate-500">{data.alerts.note || 'No alerts.'}</p>}
          </Card>
        </div>
      </div>

      <Card title="Suggestions" subtitle="Generated by rules from the radar gaps and your constraints; numbers come from the engine, not from an LLM">
        <div className="space-y-3">{data.suggestions.map((s) => <Suggestion key={s.id} s={s} />)}</div>
        <div className="mt-3"><BulletList items={data.suggestion_notes} /></div>
      </Card>

      <Card title="Top picks for this profile" subtitle="Highest fit scores with room under your caps, at most two per asset type. Educational, not a recommendation.">
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">{data.top_picks.map((t) => <TopPick key={t.ticker} p={t} />)}</div>
      </Card>

      <Collapsible title="Assumptions and limitations behind these results">
        <p className="mb-1 font-medium text-slate-700">Assumptions</p>
        <BulletList items={[...data.assumptions, ...data.radar.assumptions]} />
        <p className="mb-1 mt-3 font-medium text-slate-700">Limitations</p>
        <BulletList items={data.radar.limitations} />
        <p className="mt-3 text-xs text-slate-500">{data.notice}</p>
      </Collapsible>
    </div>
  )
}
