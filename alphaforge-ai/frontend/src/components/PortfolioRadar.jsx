import { Legend, PolarAngleAxis, PolarGrid, PolarRadiusAxis, Radar, RadarChart, ResponsiveContainer, Tooltip } from 'recharts'

function RadarTooltip({ active, payload }) {
  if (!active || !payload?.length) return null
  const d = payload[0].payload
  return (
    <div className="max-w-xs rounded-lg border border-slate-200 bg-white p-3 text-xs shadow-lg">
      <p className="font-semibold text-slate-900">{d.axis}</p>
      <p className="mt-1 text-slate-600">
        Yours: <b>{d.available ? d.value.toFixed(0) : 'n/a'}</b> · Target: <b>{d.hasTarget ? d.target.toFixed(0) : 'none'}</b>
      </p>
      <p className="mt-1 text-slate-500">{d.explanation}</p>
    </div>
  )
}

export default function PortfolioRadar({ axes, height = 340 }) {
  const data = axes.map((a) => ({
    axis: a.available ? a.label : `${a.label} (n/a)`,
    value: a.value ?? 0,
    target: a.target ?? a.value ?? 0,
    hasTarget: a.target !== null && a.target !== undefined,
    available: a.available && a.value !== null,
    explanation: a.explanation,
  }))
  return (
    <ResponsiveContainer width="100%" height={height}>
      <RadarChart data={data} outerRadius="72%">
        <PolarGrid stroke="#e2e8f0" />
        <PolarAngleAxis dataKey="axis" tick={{ fontSize: 11, fill: '#475569' }} />
        <PolarRadiusAxis domain={[0, 100]} tick={false} axisLine={false} />
        <Radar name="Target for your profile" dataKey="target" stroke="#0f172a" strokeDasharray="4 3" fill="none" strokeWidth={1.5} />
        <Radar name="Your portfolio" dataKey="value" stroke="#4f46e5" fill="#6366f1" fillOpacity={0.35} />
        <Tooltip content={<RadarTooltip />} />
        <Legend wrapperStyle={{ fontSize: 12 }} />
      </RadarChart>
    </ResponsiveContainer>
  )
}

export function ComponentRadar({ components, height = 240 }) {
  const data = components.filter((c) => c.applicable !== false && c.score !== null).map((c) => ({ axis: c.label, score: c.score }))
  return (
    <ResponsiveContainer width="100%" height={height}>
      <RadarChart data={data} outerRadius="70%">
        <PolarGrid stroke="#e2e8f0" />
        <PolarAngleAxis dataKey="axis" tick={{ fontSize: 10, fill: '#475569' }} />
        <PolarRadiusAxis domain={[0, 100]} tick={false} axisLine={false} />
        <Radar dataKey="score" stroke="#4f46e5" fill="#6366f1" fillOpacity={0.35} />
        <Tooltip />
      </RadarChart>
    </ResponsiveContainer>
  )
}
