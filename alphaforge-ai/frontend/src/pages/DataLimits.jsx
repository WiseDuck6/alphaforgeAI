import { api } from '../api.js'
import { useApi } from '../UserContext.jsx'
import { Badge, Card, ErrorBox, Spinner } from '../components/ui.jsx'
import { BulletList } from '../components/Evidence.jsx'

// Known limitations of the starter data (from alphaforge_starter/README.md) and of the method.
const LIMITS = [
  'Survivorship bias: the 31 assets were chosen in 2026 among names that are large today, so history looks better than a 2021 selection would have.',
  'Prices are in listing currency (.TO in CAD, others in USD). Metrics are converted to CAD with Bank of Canada USD/CAD, carried forward on Canadian holidays.',
  'adj_close is back-adjusted by Yahoo for splits and dividends; values for older dates change on re-download.',
  'TSX, U.S. and crypto calendars differ; crypto trades 7 days a week (UTC calendar days). Calendar alignment is documented in the pipeline.',
  'Monthly CPI and unemployment are dated by reference month, not release date; they must be lagged before use in any simulation. FRED values are revised, not first-release.',
  'CNQ.TO shows a 1.021 "split" on 2022-08-22 that is likely a special distribution; to be verified before relying on it.',
  'No licensed ESG ratings: ESG screens are sector-based flags plus controversy evidence from filings and allow-listed news. Index funds are not looked through yet.',
  'News coverage is limited to SEC filings, central-bank releases and allow-listed outlets; no coverage is not evidence that nothing happened. Headline sentiment uses a team-built word list.',
  'Fundamentals come from SEC XBRL annual filings; Alimentation Couche-Tard and Constellation Software are not SEC filers, and some issuers publish no full-year XBRL facts.',
  'Live prices after the starter window are spliced onto the starter adj_close on the last shared date; a split after that date stops the splice for the asset.',
  'Volatility and drawdowns describe the past window. They do not predict future returns.',
]

function Provenance({ title, p }) {
  if (!p) return null
  return (
    <div className="rounded-lg bg-slate-50 p-3 text-sm">
      <p className="font-medium text-slate-800">{title} <Badge tone={p.kind === 'placeholder' ? 'amber' : 'green'}>{p.kind}</Badge></p>
      <p className="mt-1 text-slate-600">{p.description}</p>
      {p.source_files && <p className="mt-1 font-mono text-xs text-slate-500">{p.source_files.join(', ')}</p>}
      {p.generated_at && <p className="mt-1 text-xs text-slate-400">Generated {p.generated_at}{p.transform_version ? ` · ${p.transform_version}` : ''}</p>}
    </div>
  )
}

const signed = (x) => (x === null || x === undefined ? 'n/a' : `${x > 0 ? '+' : ''}${(x * 100).toFixed(1)}%`)

function QualityReport({ q }) {
  const cal = q.calendar || {}
  const sanity = Object.entries(q.sanity_checks?.values || {})
  return (
    <Card title="Data quality" subtitle={q.provenance?.generated_at ? `Pipeline run ${q.provenance.generated_at} · ${q.provenance.transform_version}` : undefined}>
      <div className="grid gap-4 text-sm md:grid-cols-2">
        <div>
          <p className="font-medium text-slate-800">Calendar</p>
          <p className="mt-1 text-slate-600">{cal.rule}</p>
          <p className="mt-1 text-slate-600">
            {cal.union_days} trading days from {cal.start} to {cal.end}: {cal.tsx_only_days} TSX-only and {cal.us_only_days} U.S.-only days.
            Equity prices carried forward at most {cal.max_carry_days} days; USD/CAD carried on {q.fx?.union_days_with_carried_rate} days.
          </p>
          <p className="mt-3 font-medium text-slate-800">Known anomalies</p>
          <BulletList items={(q.known_anomalies || []).map((a) => `${a.ticker} ${a.date}: ${a.note}`)} empty="None." />
        </div>
        <div>
          <p className="font-medium text-slate-800">Sanity check: calendar-2022 returns</p>
          <table className="mt-1 text-slate-600">
            <thead><tr className="text-xs text-slate-500"><th className="pr-4 text-left font-normal">Asset</th><th className="pr-4 text-right font-normal">CAD</th><th className="text-right font-normal">Listing currency</th></tr></thead>
            <tbody>{sanity.map(([t, v]) => (
              <tr key={t}><td className="pr-4">{t}</td><td className="pr-4 text-right">{signed(v.calendar_2022_return_cad)}</td><td className="text-right">{signed(v.calendar_2022_return_local)}</td></tr>
            ))}</tbody>
          </table>
          <p className="mt-3 font-medium text-slate-800">Warnings</p>
          <BulletList items={q.warnings || []} empty="No warnings." />
        </div>
      </div>
    </Card>
  )
}

export default function DataLimits() {
  const { data, error, loading } = useApi(() => api.dataStatus(), [])
  if (error) return <ErrorBox error={error} />
  if (loading && !data) return <Spinner />
  return (
    <div className="space-y-6">
      <Card title="Data in use" subtitle={`Source: ${data.source}`}>
        <div className="flex flex-wrap gap-2 text-sm">
          {data.files_present.map((f) => <Badge key={f} tone="green">{f}</Badge>)}
          {data.files_missing.map((f) => <Badge key={f} tone="slate">missing: {f}</Badge>)}
        </div>
        <p className="mt-3 text-sm text-slate-600">Windows: {Object.entries(data.windows).map(([k, v]) => `${k} (${v})`).join(', ')}</p>
        {data.features && (
          <p className="mt-1 text-sm text-slate-600">
            Features as of {data.features.as_of}: starter data to {data.features.data_through?.starter}
            {data.features.data_through?.live ? `, live data to ${data.features.data_through.live}` : ', no live extension loaded'}.
            {' '}{data.signals_count} news, filing and policy items loaded.
          </p>
        )}
        <div className="mt-4 grid gap-3 md:grid-cols-3">
          <Provenance title="Asset metadata" p={data.provenance.assets} />
          <Provenance title="Asset metrics" p={data.provenance.asset_metrics} />
          <Provenance title="Asset features" p={data.provenance.asset_features} />
          <Provenance title="Signals (news, filings, policy)" p={data.provenance.signals} />
          <Provenance title="Fundamentals (SEC XBRL, Form 4)" p={data.provenance.fundamentals} />
          <Provenance title="Live market data" p={data.provenance.live_extension} />
        </div>
      </Card>
      {data.features && (
        <Card title="Features not computed" subtitle="Requested features left out on purpose, with the reason">
          <BulletList items={Object.entries(data.features.unavailable || {}).map(([k, v]) => `${k.replaceAll('_', ' ')}: ${v}`)} />
        </Card>
      )}
      {data.ingest_status && (
        <Card title="Ingest runs" subtitle="Last run of each job against this bundle">
          <ul className="space-y-1 text-sm">
            {Object.entries(data.ingest_status.jobs || {}).map(([j, s]) => (
              <li key={j}><Badge tone={s.status === 'ok' ? 'green' : s.status === 'partial' ? 'amber' : 'red'}>{s.status}</Badge>{' '}
                <b>{j}</b> <span className="text-slate-500">{s.last_run}</span>
                {s.errors?.length > 0 && <span className="text-xs text-rose-700"> · {s.errors.slice(0, 2).join('; ')}</span>}
              </li>
            ))}
          </ul>
        </Card>
      )}
      {data.data_quality_report && <QualityReport q={data.data_quality_report} />}
      <Card title="Known limitations"><BulletList items={LIMITS} /></Card>
      <Card title="Responsible use">
        <BulletList items={[
          'Simulation only: no real transactions, no personalized financial advice, no real client data.',
          'Facts (data values), assumptions (team thresholds) and fictional scenarios are labelled separately in every result.',
          'When data is missing or weak, results say so and confidence is lowered instead of filling the gap.',
        ]} />
      </Card>
    </div>
  )
}
