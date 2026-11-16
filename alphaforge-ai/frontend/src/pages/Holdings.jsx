import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useApi, useUser } from '../UserContext.jsx'
import { Bar, Card, ErrorBox, scoreTone, Spinner, StatusBadge, VerdictBadge } from '../components/ui.jsx'
import Sparkline from '../components/Sparkline.jsx'
import { label, money, pct, signedPct } from '../format.js'

// [key, header, sortable, title]. Same features as My top picks, plus amount and weight.
const COLUMNS = [
  ['ticker', 'Asset', true],
  ['trend', 'Price trend', false, 'Adjusted close, listing currency'],
  ['amount', 'Amount', true, 'Fictional amount in CAD, from the investor profile'],
  ['weight', 'Weight', true, 'Share of the fictional portfolio'],
  ['score', 'Fit with your profile', true],
  ['ret_1y', '1Y return', true, 'Total return in CAD over 252 trading days'],
  ['vol_1y', 'Volatility', true, 'Annualised, last 12 months'],
  ['mdd_5y', 'Worst fall', true, 'Largest peak-to-trough fall, 5 years'],
]

const kf = (r, k) => r.key_features?.[k]

const sortValue = (r, key) => {
  if (key === 'mdd_5y') return kf(r, 'mdd_5y') ?? r.metrics?.max_drawdown ?? -Infinity
  if (key === 'vol_1y') return kf(r, 'vol_1y') ?? r.metrics?.ann_vol ?? -Infinity
  if (key === 'ret_1y') return kf(r, 'ret_1y') ?? -Infinity
  return r[key] ?? -Infinity
}

const NoProfile = () => (
  <Card title="No investor profile yet">
    <p className="text-sm text-slate-600">Create a fictional profile, or load one of the three personas from the brief.</p>
    <Link to="/profile" className="mt-3 inline-block rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white">Set up profile</Link>
  </Card>
)

export default function Holdings() {
  const { userId } = useUser()
  // Amounts come from the saved profile; scores and features from the same ranking as My top picks.
  const { data, error, loading } = useApi(() => Promise.all([api.profile(), api.assets()]), [userId])
  const spark = useApi(() => api.sparklines(), [])
  const [sort, setSort] = useState(['amount', -1])

  const rows = useMemo(() => {
    if (!data) return []
    const [{ profile }, ranked] = data
    const scored = Object.fromEntries(ranked.results.map((r) => [r.ticker, r]))
    // The profile wizard allows the same ticker more than once; show one row per asset.
    const byTicker = {}
    for (const h of profile.holdings) {
      const row = byTicker[h.ticker] || (byTicker[h.ticker] = { ticker: h.ticker, ...scored[h.ticker], amount: 0, lots: 0 })
      row.amount += Number(h.amount) || 0
      row.lots += 1
    }
    const total = Object.values(byTicker).reduce((s, r) => s + r.amount, 0)
    return Object.values(byTicker)
      .map((r) => ({ ...r, weight: total ? r.amount / total : 0 }))
      .sort((a, b) => {
        const [k, dir] = sort
        const va = sortValue(a, k), vb = sortValue(b, k)
        return (va > vb ? 1 : va < vb ? -1 : 0) * dir
      })
  }, [data, sort])

  if (error?.status === 404) return <NoProfile />
  if (error) return <ErrorBox error={error} />
  if (loading && !data) return <Spinner />

  const [{ profile }, ranked] = data
  const total = rows.reduce((s, r) => s + r.amount, 0)
  const series = spark.data?.series || {}

  if (!rows.length) {
    return (
      <Card title="My holdings" subtitle={`${profile.display_name}'s fictional portfolio`}>
        <p className="text-sm text-slate-600">This profile has no holdings yet.</p>
        <Link to="/profile" className="mt-3 inline-block rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white">Add holdings</Link>
      </Card>
    )
  }

  return (
    <Card title="My holdings"
      subtitle={`${profile.display_name}'s fictional portfolio: ${rows.length} asset${rows.length === 1 ? '' : 's'}, ${money(total)}. `
        + `Features as of ${ranked.features_as_of || ranked.period}.`}>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              {COLUMNS.map(([k, text, sortable, title]) => (
                <th key={k} title={title} className={`whitespace-nowrap px-2 py-2 ${sortable ? 'cursor-pointer' : ''}`}
                  onClick={sortable ? () => setSort(([sk, d]) => [k, sk === k ? -d : (k === 'ticker' ? 1 : -1)]) : undefined}>
                  {text}{sort[0] === k ? (sort[1] < 0 ? ' ↓' : ' ↑') : ''}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const s = series[r.ticker]
              const vol = kf(r, 'vol_1y') ?? r.metrics?.ann_vol
              const mdd = kf(r, 'mdd_5y') ?? r.metrics?.max_drawdown
              return (
                <tr key={r.ticker} className={`border-b border-slate-100 hover:bg-slate-50 ${r.status === 'excluded' ? 'opacity-60' : ''}`}>
                  <td className="px-2 py-2">
                    <Link to={`/assets/${r.ticker}`} state={{ from: 'holdings' }} className="font-semibold text-slate-900 hover:text-indigo-700">{r.ticker}</Link>
                    <p className="text-xs text-slate-500">
                      {r.name ? `${r.name} · ${label(r.asset_class)}` : 'Not in the asset universe'}{r.lots > 1 ? ` · ${r.lots} entries combined` : ''}
                    </p>
                  </td>
                  <td className="px-2 py-2">
                    {s ? (
                      <Link to={`/assets/${r.ticker}`} state={{ from: 'holdings' }} aria-label={`Open ${r.ticker} price chart`}>
                        <Sparkline values={s.values} title={`${r.ticker} ${s.start} to ${s.end}`} />
                      </Link>
                    ) : spark.loading ? <span className="text-xs text-slate-400">...</span> : <Sparkline values={null} />}
                  </td>
                  <td className="whitespace-nowrap px-2 py-2 font-medium">{money(r.amount)}</td>
                  <td className="px-2 py-2">{pct(r.weight, 1)}</td>
                  <td className="w-52 px-2 py-2">
                    {r.score === undefined ? <span className="text-slate-400">n/a</span> : (
                      <>
                        <div className="flex items-center gap-2">
                          <span className="w-7 font-semibold">{r.score}</span>
                          <Bar value={r.score} tone={scoreTone(r.score)} />
                        </div>
                        <div className="mt-1 flex flex-wrap gap-1">
                          {r.verdict && <VerdictBadge verdict={r.verdict} />}
                          {r.status && r.status !== 'ok' && r.status !== 'caution' && <StatusBadge status={r.status} />}
                        </div>
                      </>
                    )}
                  </td>
                  <td className="px-2 py-2">{signedPct(kf(r, 'ret_1y'))}</td>
                  <td className="px-2 py-2">{pct(vol)}</td>
                  <td className="px-2 py-2">{pct(mdd)}</td>
                </tr>
              )
            })}
          </tbody>
          <tfoot>
            <tr className="font-semibold text-slate-900">
              <td className="px-2 py-2" colSpan={2}>Total</td>
              <td className="whitespace-nowrap px-2 py-2">{money(total)}</td>
              <td className="px-2 py-2">100%</td>
              <td colSpan={COLUMNS.length - 4} />
            </tr>
          </tfoot>
        </table>
      </div>
      <p className="mt-3 text-xs text-slate-400">
        Amounts are in CAD and come from your investor profile; change them on the{' '}
        <Link to="/profile" className="text-indigo-600">Investor profile</Link> page. Past returns, volatility and drawdowns describe
        the data window; they do not predict future returns.
      </p>
    </Card>
  )
}
