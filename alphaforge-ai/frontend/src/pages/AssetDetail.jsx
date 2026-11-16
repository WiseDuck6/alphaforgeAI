import { useState } from 'react'
import { Link, useLocation, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { useApi, useUser } from '../UserContext.jsx'
import { ComponentRadar } from '../components/PortfolioRadar.jsx'
import PriceChart from '../components/PriceChart.jsx'
import ScoreBreakdown from '../components/ScoreBreakdown.jsx'
import GovernanceResult from '../components/GovernanceResult.jsx'
import { Badge, Card, ErrorBox, ScoreBadge, Spinner, StatusBadge, VerdictBadge } from '../components/ui.jsx'
import { BulletList, NewsList } from '../components/Evidence.jsx'
import { byUnit, label, pct, signedPct } from '../format.js'

function CheckPosition({ ticker }) {
  const [weight, setWeight] = useState(10)
  const [state, setState] = useState({ result: null, error: null, busy: false })
  const run = () => {
    setState({ result: null, error: null, busy: true })
    api.check({ type: 'add', ticker, weight: weight / 100 })
      .then((result) => setState({ result, error: null, busy: false }))
      .catch((error) => setState({ result: null, error, busy: false }))
  }
  return (
    <Card title="Test a fictional position" subtitle="The governance check compares the portfolio before and after against your constraints">
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <label className="flex items-center gap-2">Add
          <input type="range" min="1" max="50" value={weight} onChange={(e) => setWeight(Number(e.target.value))} />
          <b className="w-10">{weight}%</b> of {ticker}
        </label>
        <button onClick={run} disabled={state.busy} className="rounded-lg bg-indigo-600 px-3 py-1.5 font-medium text-white disabled:opacity-50">
          {state.busy ? 'Checking...' : 'Check'}
        </button>
      </div>
      <div className="mt-4">
        <ErrorBox error={state.error} />
        {state.result && <GovernanceResult result={state.result} />}
      </div>
    </Card>
  )
}

function FeatureGroups({ groups }) {
  const [open, setOpen] = useState(() => new Set(['returns', 'risk', 'risk_adjusted', 'technical']))
  if (!groups?.length) return <p className="text-sm text-slate-500">Feature engine output is not loaded.</p>
  return (
    <div className="space-y-2">
      {groups.map((g) => (
        <details key={g.group} open={open.has(g.group)} onToggle={(e) => setOpen((s) => {
          const n = new Set(s); e.target.open ? n.add(g.group) : n.delete(g.group); return n
        })} className="rounded-lg border border-slate-200">
          <summary className="cursor-pointer px-3 py-2 text-sm font-medium text-slate-700">{g.label}
            <span className="ml-2 text-xs font-normal text-slate-400">{g.features.filter((f) => f.value !== null && f.value !== undefined).length}/{g.features.length}</span>
          </summary>
          <dl className="grid gap-x-6 gap-y-2 border-t border-slate-100 px-3 py-2 text-sm sm:grid-cols-2">
            {g.features.map((f) => (
              <div key={f.key} title={`${f.method} Window: ${f.window}.`} className="flex justify-between gap-3">
                <dt className="text-slate-500">{f.label}</dt>
                <dd className={`text-right font-medium ${f.value === null || f.value === undefined ? 'text-slate-400' : 'text-slate-900'}`}>
                  {f.value === null || f.value === undefined ? <span title={f.missing_reason}>n/a ⓘ</span> : byUnit(f.value, f.unit)}
                </dd>
              </div>
            ))}
          </dl>
        </details>
      ))}
      <p className="text-xs text-slate-400">Hover a value for its method and window; hover n/a for the reason it is missing.</p>
    </div>
  )
}

function Moves({ details, signals }) {
  const moves = details?.big_moves || []
  if (!moves.length) return <p className="text-sm text-slate-500">No unusual daily moves (3+ times the usual size) in the data.</p>
  const near = (d) => {
    const t = new Date(d).getTime()
    return signals.filter((s) => {
      const x = new Date(s.published_at.slice(0, 10)).getTime()
      return (s.kind === 'news' || s.kind === 'filing') && x >= t - 2 * 864e5 && x <= t + 864e5
    }).slice(0, 2)
  }
  return (
    <ul className="space-y-2 text-sm">
      {[...moves].reverse().map((m) => (
        <li key={m.date}>
          <span className="font-mono text-xs text-slate-500">{m.date}</span>{' '}
          <b className={m.return >= 0 ? 'text-emerald-700' : 'text-rose-700'}>{signedPct(m.return, 1)}</b>
          <span className="text-xs text-slate-400"> ({Math.abs(m.z).toFixed(1)}x usual)</span>
          {near(m.date).map((s) => (
            <p key={s.signal_id} className="ml-4 text-slate-600">
              {s.source_url ? <a href={s.source_url} target="_blank" rel="noreferrer" className="hover:underline">{s.title}</a> : s.title}
              <span className="text-xs text-slate-400"> · {s.publisher}</span>
            </p>
          ))}
        </li>
      ))}
      <li className="text-xs text-slate-400">Items published from 2 days before to 1 day after a move are context, not proof of cause.</li>
    </ul>
  )
}

export default function AssetDetail() {
  const { ticker } = useParams()
  const fromHoldings = useLocation().state?.from === 'holdings'
  const { userId } = useUser()
  const { data, error, loading } = useApi(() => api.asset(ticker), [ticker, userId])
  const [kind, setKind] = useState('')
  if (error) return <ErrorBox error={error} />
  if (loading && !data) return <Spinner />
  const { asset, score: s } = data
  const m = s.metrics
  const k = s.key_features || {}
  const signals = data.signals || []
  const shown = signals.filter((x) => !kind || x.kind === kind)
  const dd = data.feature_details?.drawdown_5y
  return (
    <div className="space-y-6">
      {fromHoldings
        ? <Link to="/holdings" className="text-sm text-indigo-600">← My holdings</Link>
        : <Link to="/assets" className="text-sm text-indigo-600">← My top picks</Link>}
      <PriceChart key={asset.ticker} asset={asset} history={data.price_history} />
      <div className="flex flex-wrap items-center gap-4">
        <ScoreBadge score={s.score} size="lg" />
        <div>
          <p className="text-sm font-medium text-slate-700">Profile fit {s.score}/100 <VerdictBadge verdict={s.verdict} /></p>
          <div className="mt-1 flex flex-wrap gap-2">
            {s.badges.length === 0 && <StatusBadge status={s.status} />}
            {s.badges.map((b) => <Badge key={b} tone={s.status === 'excluded' || s.status === 'over_cap' ? 'red' : 'amber'}>{b}</Badge>)}
            <Badge>{label(asset.asset_class)}</Badge><Badge>{asset.region}</Badge><Badge>{asset.listing_currency}</Badge>
            <Badge>{asset.sector}</Badge>
          </div>
          {s.gates.map((g) => <p key={g} className="mt-1 text-sm text-amber-800">{g}</p>)}
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <Card title="Why this score" subtitle={`Each component compares features with your profile · features as of ${s.features_as_of || s.period}`} className="lg:col-span-2">
          <ScoreBreakdown components={s.components} />
        </Card>
        <div className="space-y-6">
          <Card title="Fit shape"><ComponentRadar components={s.components} /></Card>
          <Card title="Key figures" subtitle="CAD base">
            <dl className="grid grid-cols-2 gap-3 text-sm">
              <div><dt className="text-slate-500">1Y return*</dt><dd className="font-semibold">{signedPct(k.ret_1y)}</dd></div>
              <div><dt className="text-slate-500">Volatility 1Y</dt><dd className="font-semibold">{pct(k.vol_1y ?? m.ann_vol)}</dd></div>
              <div><dt className="text-slate-500">Worst fall 5Y</dt><dd className="font-semibold">{pct(k.mdd_5y ?? m.max_drawdown)}</dd></div>
              <div><dt className="text-slate-500">2022</dt><dd className="font-semibold">{signedPct(m.stress?.['2022_rates_shock'])}</dd></div>
              <div><dt className="text-slate-500">Dividend yield</dt><dd className="font-semibold">{pct(k.dividend_yield_ttm ?? m.dividend_yield_ttm, 1)}</dd></div>
              <div><dt className="text-slate-500">Your weight</dt><dd className="font-semibold">{pct(s.current_weight)} (cap {pct(s.cap.value)})</dd></div>
            </dl>
            {dd && <p className="mt-2 text-xs text-slate-500">Worst fall ran from {dd.peak_date} to {dd.trough_date}; {dd.recovered_after_days ? `recovered after ${dd.recovered_after_days} trading days` : 'not recovered by the end of the data'}.</p>}
            <p className="mt-2 text-xs text-slate-400">*Past returns describe the window; they do not predict future returns.</p>
          </Card>
          <Card title="Confidence">
            <Badge tone={s.confidence.level === 'high' ? 'green' : s.confidence.level === 'medium' ? 'amber' : 'red'}>{s.confidence.level}</Badge>
            <div className="mt-2"><BulletList items={s.confidence.reasons} empty="No caveats." /></div>
          </Card>
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <Card title="All features" subtitle="Computed by the feature engine from prices, filings and news" className="lg:col-span-2">
          <FeatureGroups groups={data.features} />
        </Card>
        <Card title="Unusual price moves" subtitle="Daily moves 3+ times the usual size, with items published around them">
          <Moves details={data.feature_details} signals={signals} />
        </Card>
      </div>

      <CheckPosition ticker={asset.ticker} />

      <div className={`grid gap-6 ${asset.esg?.notes ? 'lg:grid-cols-3' : ''}`}>
        <Card title="News and filings" subtitle={`Last 12 months to ${data.features_as_of || 'today'} · SEC EDGAR and allow-listed outlets only`}
          className={asset.esg?.notes ? 'lg:col-span-2' : ''}
          actions={
            <select value={kind} onChange={(e) => setKind(e.target.value)} className="rounded-lg border border-slate-300 px-2 py-1 text-xs">
              <option value="">All ({signals.length})</option>
              <option value="news">Articles ({signals.filter((x) => x.kind === 'news').length})</option>
              <option value="filing">Filings ({signals.filter((x) => x.kind === 'filing').length})</option>
            </select>
          }>
          <div className="max-h-[28rem] overflow-y-auto pr-2"><NewsList items={shown.slice(0, 60)} /></div>
        </Card>
        {asset.esg?.notes && (
          <Card title="ESG note"><p className="text-sm text-slate-600">{asset.esg.notes}</p></Card>
        )}
      </div>
    </div>
  )
}
