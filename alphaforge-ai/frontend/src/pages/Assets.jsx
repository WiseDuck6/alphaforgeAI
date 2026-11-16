import { Fragment, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useApi, useUser } from '../UserContext.jsx'
import { Badge, Bar, Card, Collapsible, ErrorBox, scoreTone, Spinner, StabilityBadge, StatusBadge, VerdictBadge } from '../components/ui.jsx'
import Sparkline from '../components/Sparkline.jsx'
import { NewsList } from '../components/Evidence.jsx'
import { label, pct, signedPct } from '../format.js'

// [key, header, sortable, title]
const COLUMNS = [
  ['rank', '#', true, 'Rank by fit score'],
  ['ticker', 'Asset', true],
  ['trend', 'Price trend', false, 'Adjusted close, listing currency'],
  ['score', 'Fit with your profile', true],
  ['stability', 'Rank stability', true, 'Share of 300 weight variations in which the asset ranks in the top five'],
  ['ret_1y', '1Y return', true, 'Total return in CAD over 252 trading days'],
  ['vol_1y', 'Volatility', true, 'Annualised, last 12 months'],
  ['mdd_5y', 'Worst fall', true, 'Largest peak-to-trough fall, 5 years'],
  ['sharpe_3y', 'Sharpe 3Y', true, 'Return above the Bank of Canada rate per unit of volatility'],
  ['px_vs_sma_200', 'Trend', true, 'Price vs 200-day average, and RSI 14'],
  ['news', 'News 30d', true, 'Articles from allow-listed outlets, and recency-weighted sentiment'],
  ['current_weight', 'Held', true],
]

const kf = (r, k) => r.key_features?.[k]

const sortValue = (r, key) => {
  if (key === 'stability') return r.rank_stability?.p_top5 ?? -1
  if (key === 'news') return kf(r, 'news_count_30d') ?? -1
  if (key === 'mdd_5y') return kf(r, 'mdd_5y') ?? r.metrics.max_drawdown ?? -Infinity
  if (key === 'vol_1y') return kf(r, 'vol_1y') ?? r.metrics.ann_vol ?? -Infinity
  if (['ret_1y', 'sharpe_3y', 'px_vs_sma_200'].includes(key)) return kf(r, key) ?? -Infinity
  return r[key]
}

function Profile({ inv, weights, labels, reasons }) {
  const sorted = Object.entries(weights).sort((a, b) => b[1] - a[1])
  return (
    <Collapsible title="How AlphaForge reads your profile (classifications and weights)">
      <div className="grid gap-6 lg:grid-cols-2">
        <dl className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {inv.classifications.map((c) => (
            <div key={c.key} className="rounded-lg bg-slate-50 p-2" title={c.why}>
              <dt className="text-xs text-slate-500">{c.label}</dt>
              <dd className="font-medium text-slate-900">{c.value}</dd>
              <p className="mt-0.5 text-xs text-slate-500">{c.why}</p>
            </div>
          ))}
        </dl>
        <div>
          <p className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500">Weight of each component in your fit score</p>
          <div className="space-y-1.5">
            {sorted.map(([k, w]) => (
              <div key={k} className="flex items-center gap-2 text-sm">
                <span className="w-48 shrink-0 text-slate-700">{labels[k]}</span>
                <Bar value={w * 100} max={30} />
                <span className="w-10 text-right font-medium">{Math.round(w * 100)}%</span>
              </div>
            ))}
          </div>
          <ul className="mt-3 list-disc space-y-0.5 pl-5 text-xs text-slate-500">{reasons.map((r) => <li key={r}>{r}</li>)}</ul>
        </div>
      </div>
    </Collapsible>
  )
}

function RowDetail({ r }) {
  return (
    <div className="grid gap-4 bg-slate-50 p-4 md:grid-cols-3">
      <div className="md:col-span-2 space-y-3 text-sm">
        {r.drivers.up.length > 0 && (
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-emerald-700">Pushing the score up</p>
            {r.drivers.up.map((d) => <p key={d.key} className="mt-1 text-slate-700"><b>{d.label}.</b> {d.explanation}</p>)}
          </div>
        )}
        {r.drivers.down.length > 0 && (
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-rose-700">Pulling it down</p>
            {r.drivers.down.map((d) => <p key={d.key} className="mt-1 text-slate-700"><b>{d.label}.</b> {d.explanation}</p>)}
          </div>
        )}
        {r.gates.map((g) => <p key={g} className="text-amber-800">{g}</p>)}
        {r.badges.length > 0 && <div className="flex flex-wrap gap-1">{r.badges.map((b) => <Badge key={b} tone="amber">{b}</Badge>)}</div>}
        <p className="text-xs text-slate-500">Confidence {r.confidence.level}{r.confidence.reasons.length ? `: ${r.confidence.reasons.join(' ')}` : '.'}</p>
      </div>
      <div>
        <p className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-500">Latest items</p>
        <NewsList items={r.news.slice(0, 3)} empty="No recent filings or allow-listed articles." />
        <Link to={`/assets/${r.ticker}`} className="mt-3 inline-block text-sm font-medium text-indigo-600">Full breakdown, features and news →</Link>
      </div>
    </div>
  )
}

export default function Assets() {
  const { userId } = useUser()
  const { data, error, loading } = useApi(() => api.assets(), [userId])
  const spark = useApi(() => api.sparklines(), [])
  const [query, setQuery] = useState('')
  const [cls, setCls] = useState('')
  const [verdict, setVerdict] = useState('')
  const [hideExcluded, setHideExcluded] = useState(false)
  const [sort, setSort] = useState(['rank', 1])
  const [open, setOpen] = useState(null)

  const rows = useMemo(() => {
    if (!data) return []
    const q = query.toLowerCase()
    return data.results
      .filter((r) => (!cls || r.asset_class === cls) && (!verdict || r.verdict.key === verdict)
        && (!hideExcluded || r.status !== 'excluded')
        && (!q || r.ticker.toLowerCase().includes(q) || r.name.toLowerCase().includes(q)))
      .sort((a, b) => {
        const [k, dir] = sort
        const va = sortValue(a, k), vb = sortValue(b, k)
        return (va > vb ? 1 : va < vb ? -1 : 0) * dir
      })
  }, [data, query, cls, verdict, hideExcluded, sort])

  if (error) return <ErrorBox error={error} />
  if (loading && !data) return <Spinner />

  const classes = [...new Set(data.results.map((r) => r.asset_class))]
  const verdicts = [...new Map(data.results.map((r) => [r.verdict.key, r.verdict.label])).entries()]
  const series = spark.data?.series || {}
  const regime = data.market_regime
  const through = data.data_through || {}
  return (
    <div className="space-y-4">
      <Card title="My top picks"
        subtitle={`All ${data.results.length} assets, ranked by fit with your profile. Features as of ${data.features_as_of || data.period}${through.live ? ` (live data through ${through.live})` : ''}. `
          + 'Each score compares the asset\'s features with your tolerances and needs; excluded assets are shown, not hidden.'}>
        <div className="space-y-3">
          {regime && (
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <Badge tone={regime.label === 'calm_uptrend' ? 'green' : regime.label === 'mixed' ? 'slate' : 'amber'}>market: {label(regime.label)}</Badge>
              <span className="text-slate-600">{regime.summary}</span>
            </div>
          )}
          <Profile inv={data.investor} weights={data.component_weights} labels={data.component_labels} reasons={data.investor.weight_reasons} />
          <Collapsible title="Method, gates and sensitivity test">
            <ul className="list-disc space-y-1 pl-5">{data.assumptions.map((a) => <li key={a}>{a}</li>)}</ul>
            <p className="mt-2">{data.sensitivity.method}</p>
          </Collapsible>
        </div>
      </Card>
      <Card>
        <div className="mb-4 flex flex-wrap items-center gap-3">
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search ticker or name"
            className="w-56 rounded-lg border border-slate-300 px-3 py-1.5 text-sm" />
          <select value={cls} onChange={(e) => setCls(e.target.value)} className="rounded-lg border border-slate-300 px-2 py-1.5 text-sm">
            <option value="">All classes</option>
            {classes.map((c) => <option key={c} value={c}>{label(c)}</option>)}
          </select>
          <select value={verdict} onChange={(e) => setVerdict(e.target.value)} className="rounded-lg border border-slate-300 px-2 py-1.5 text-sm">
            <option value="">All verdicts</option>
            {verdicts.map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
          <label className="flex items-center gap-1.5 text-sm text-slate-600">
            <input type="checkbox" checked={hideExcluded} onChange={(e) => setHideExcluded(e.target.checked)} /> Hide excluded
          </label>
          <span className="ml-auto text-xs text-slate-400">Click a row for the reasons behind its score.</span>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
              <tr>
                {COLUMNS.map(([k, text, sortable, title]) => (
                  <th key={k} title={title} className={`whitespace-nowrap px-2 py-2 ${sortable ? 'cursor-pointer' : ''}`}
                    onClick={sortable ? () => setSort(([sk, d]) => [k, sk === k ? -d : (k === 'rank' || k === 'ticker' ? 1 : -1)]) : undefined}>
                    {text}{sort[0] === k ? (sort[1] < 0 ? ' ↓' : ' ↑') : ''}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const vol = kf(r, 'vol_1y') ?? r.metrics.ann_vol
                const mdd = kf(r, 'mdd_5y') ?? r.metrics.max_drawdown
                const trend = kf(r, 'px_vs_sma_200')
                const rsi = kf(r, 'rsi_14')
                const n30 = kf(r, 'news_count_30d')
                const sent = kf(r, 'news_sentiment_30d')
                return (
                  <Fragment key={r.ticker}>
                    <tr onClick={() => setOpen(open === r.ticker ? null : r.ticker)}
                      className={`cursor-pointer border-b border-slate-100 hover:bg-slate-50 ${r.status === 'excluded' ? 'opacity-60' : ''}`}>
                      <td className="px-2 py-2 font-mono text-slate-500">{r.rank}</td>
                      <td className="px-2 py-2">
                        <Link to={`/assets/${r.ticker}`} onClick={(e) => e.stopPropagation()} className="font-semibold text-slate-900 hover:text-indigo-700">{r.ticker}</Link>
                        <p className="text-xs text-slate-500">{r.name} · {label(r.asset_class)}</p>
                      </td>
                      <td className="px-2 py-2">
                        {series[r.ticker] ? <Sparkline values={series[r.ticker].values} title={`${r.ticker} ${series[r.ticker].start} to ${series[r.ticker].end}`} />
                          : spark.loading ? <span className="text-xs text-slate-400">...</span> : <Sparkline values={null} />}
                      </td>
                      <td className="w-52 px-2 py-2">
                        <div className="flex items-center gap-2">
                          <span className="w-7 font-semibold">{r.score}</span>
                          <Bar value={r.score} tone={scoreTone(r.score)} />
                        </div>
                        <div className="mt-1 flex flex-wrap gap-1"><VerdictBadge verdict={r.verdict} />{r.status !== 'ok' && r.status !== 'caution' && r.status !== 'excluded' && <StatusBadge status={r.status} />}</div>
                      </td>
                      <td className="px-2 py-2"><StabilityBadge s={r.rank_stability} /></td>
                      <td className="px-2 py-2">{signedPct(kf(r, 'ret_1y'))}</td>
                      <td className="px-2 py-2">{pct(vol)}</td>
                      <td className="px-2 py-2">{pct(mdd)}</td>
                      <td className="px-2 py-2">{kf(r, 'sharpe_3y') === null || kf(r, 'sharpe_3y') === undefined ? 'n/a' : kf(r, 'sharpe_3y').toFixed(2)}</td>
                      <td className="px-2 py-2 whitespace-nowrap">
                        {trend === null || trend === undefined ? 'n/a' : <span className={trend >= 0 ? 'text-emerald-700' : 'text-rose-700'}>{trend >= 0 ? '▲' : '▼'} {signedPct(trend)}</span>}
                        {rsi !== null && rsi !== undefined && <span className="ml-1 text-xs text-slate-400">RSI {rsi.toFixed(0)}</span>}
                      </td>
                      <td className="px-2 py-2 whitespace-nowrap">
                        {n30 ? <span>{n30} <span className={sent > 0.15 ? 'text-emerald-700' : sent < -0.15 ? 'text-rose-700' : 'text-slate-500'}>
                          {sent === null || sent === undefined ? '' : `(${sent > 0 ? '+' : ''}${sent.toFixed(2)})`}</span></span> : <span className="text-slate-400">0</span>}
                        {kf(r, 'esg_controversy_level') && kf(r, 'esg_controversy_level') !== 'none_found' && data.investor.needs.esg !== 'none' &&
                          <span className="ml-1"><Badge tone="red">ESG {kf(r, 'esg_controversy_level')}</Badge></span>}
                      </td>
                      <td className="px-2 py-2 text-slate-600">{r.current_weight > 0 ? pct(r.current_weight) : ''}</td>
                    </tr>
                    {open === r.ticker && <tr><td colSpan={COLUMNS.length}><RowDetail r={r} /></td></tr>}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  )
}
