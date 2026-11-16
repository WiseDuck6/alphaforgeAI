import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Bar, BarChart, Line, LineChart, Tooltip, XAxis, YAxis } from 'recharts'

const UP = '#059669'
const DOWN = '#e11d48'
const HEIGHT = 320
const Y_AXIS_WIDTH = 64
const MARGIN = { top: 5, right: 10, bottom: 0, left: 0 }
const MIN_BARS = 10 // zooming in stops at this many bars (or points) on screen
const ZOOM_STEP = 1.25 // per click on + / -
const CANDLE_PX = 4 // minimum horizontal pixels per candle; sets the candle interval for the visible range

const price = (x) => Number(x).toLocaleString('en-CA', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const axisPrice = (x) => Number(x).toLocaleString('en-CA', { maximumFractionDigits: x < 10 ? 2 : 0 })

// Candle intervals, finest first. The chart uses the finest one that fits the visible range.
const INTERVALS = [
  ['daily', 'Daily'],
  ['weekly', 'Weekly'],
  ['monthly', 'Monthly'],
]

// [label, kind, amount]: 'days' counts trading days back from the last one (the data is daily,
// with no intraday prices), 'months' counts calendar months, 'ytd' starts on 1 January, 'all' is everything.
const PRESETS = [
  ['1D', 'days', 1], ['5D', 'days', 5], ['1M', 'months', 1], ['6M', 'months', 6],
  ['YTD', 'ytd'], ['1Y', 'months', 12], ['3Y', 'months', 36], ['ALL', 'all'],
]
const PRESET_TITLES = {
  '1D': 'Last trading day', '5D': 'Last 5 trading days', '1M': 'Last month', '6M': 'Last 6 months',
  YTD: 'Year to date', '1Y': 'Last year', '3Y': 'Last 3 years', ALL: 'Full history',
}

const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi)
const toDate = (iso) => new Date(`${iso}T00:00:00Z`)
const daysBetween = (a, b) => Math.round((toDate(b) - toDate(a)) / 86400000)

// Monday (UTC) of the ISO week containing an ISO date string.
function weekKey(date) {
  const d = toDate(date)
  d.setUTCDate(d.getUTCDate() - ((d.getUTCDay() + 6) % 7))
  return d.toISOString().slice(0, 10)
}

// Same day N months earlier, clamped to the end of a shorter month (e.g. Aug 31 - 6 months = Feb 28).
function monthsBefore(iso, months) {
  const [y, m, d] = iso.split('-').map(Number)
  const total = y * 12 + (m - 1) - months
  const year = Math.floor(total / 12), month = total % 12
  const lastDay = new Date(Date.UTC(year, month + 1, 0)).getUTCDate()
  return new Date(Date.UTC(year, month, Math.min(d, lastDay))).toISOString().slice(0, 10)
}

// Date range for a preset (null = full history). `points` are the daily rows, oldest first.
// For 1D and 5D a line also includes the previous close, so it shows the move over those days
// (a line needs two points); candles show exactly that many daily candles.
export function presetRange(key, points, kind) {
  const [, type, amount] = PRESETS.find(([k]) => k === key)
  const n = points.length
  if (type === 'all' || !n) return null
  const lastDate = points[n - 1].date
  if (type === 'days') {
    const back = amount + (kind === 'line' ? 1 : 0)
    return { from: points[Math.max(0, n - back)].date, to: lastDate }
  }
  return { from: type === 'ytd' ? `${lastDate.slice(0, 4)}-01-01` : monthsBefore(lastDate, amount), to: lastDate }
}

// One candle per period: first open, highest high, lowest low, last close.
// Days without a valid open/high/low are left out, not filled in.
export function buildCandles(points, interval) {
  const bars = points.filter((p) => p.open != null && p.high != null && p.low != null && p.close != null)
  const keyOf = interval === 'monthly' ? (d) => d.slice(0, 7) : interval === 'weekly' ? weekKey : (d) => d
  const candles = []
  let cur = null
  for (const p of bars) {
    const key = keyOf(p.date)
    if (!cur || cur.key !== key) {
      cur = { key, date: p.date, end: p.date, open: p.open, high: p.high, low: p.low, close: p.close, days: 0 }
      candles.push(cur)
    }
    cur.end = p.date
    cur.high = Math.max(cur.high, p.high)
    cur.low = Math.min(cur.low, p.low)
    cur.close = p.close
    cur.days += 1
  }
  return candles.map((c) => ({ ...c, range: [c.low, c.high] }))
}

const itemEnd = (s) => s.end ?? s.date

// Visible index window [i0, i1] for a date range (null = everything). Always at least one item.
export function windowFor(series, range) {
  const n = series.length
  if (!n) return [0, -1]
  if (!range) return [0, n - 1]
  let i0 = series.findIndex((s) => itemEnd(s) >= range.from)
  if (i0 < 0) i0 = n - 1
  let i1 = -1
  for (let i = n - 1; i >= 0; i--) if (series[i].date <= range.to) { i1 = i; break }
  if (i1 < 0) i1 = 0
  if (i1 < i0) i1 = i0
  return [i0, i1]
}

// Date range covering items i0..i1 (null when that is the whole series).
export function rangeFor(series, i0, i1) {
  if (i0 <= 0 && i1 >= series.length - 1) return null
  return { from: series[i0].date, to: itemEnd(series[i1]) }
}

// Zoom by `factor` (>1 zooms out) keeping the item under fraction `f` (0..1 across the plot) in place.
export function zoomWindow([i0, i1], total, factor, f) {
  const n = i1 - i0 + 1
  const anchor = i0 + f * (n - 1)
  // Zooming out always adds at least one bar, so very short windows (1D, 5D) can grow.
  const target = factor > 1 ? Math.max(n + 1, Math.round(n * factor)) : Math.round(n * factor)
  const next = clamp(target, Math.min(n, MIN_BARS), total)
  if (next === n) return [i0, i1]
  const start = clamp(Math.round(anchor - f * (next - 1)), 0, total - next)
  return [start, start + next - 1]
}

export function panWindow([i0, i1], total, shift) {
  const n = i1 - i0 + 1
  const start = clamp(i0 + shift, 0, total - n)
  return [start, start + n - 1]
}

// Width of an element in CSS pixels, kept current on resize. The chart gets an explicit width from
// here instead of relying on Recharts' ResponsiveContainer, which draws nothing while it thinks the
// parent has no size.
function useElementWidth(ref) {
  const [width, setWidth] = useState(0)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return undefined
    const measure = () => setWidth(Math.floor(el.getBoundingClientRect().width))
    measure()
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(measure) : null
    ro?.observe(el)
    window.addEventListener('resize', measure)
    return () => {
      ro?.disconnect()
      window.removeEventListener('resize', measure)
    }
  }, [ref])
  return width
}

// Custom bar shape: the bar spans low..high; draw the wick there and the body from open to close.
function Candle({ x, y, width, height, payload }) {
  if (!payload || width === undefined || y === undefined) return null
  const { open, close, high, low } = payload
  const top = Math.min(y, y + height)
  const h = Math.abs(height)
  const span = high - low
  const yOf = (v) => (span === 0 ? top : top + ((high - v) / span) * h)
  const color = close >= open ? UP : DOWN
  // Bodies get at least 1px so flat candles stay visible, kept inside the wick.
  const bodyHeight = Math.max(yOf(Math.min(open, close)) - yOf(Math.max(open, close)), 1)
  const bodyTop = Math.max(top, Math.min(yOf(Math.max(open, close)), top + h - bodyHeight))
  const cx = x + width / 2
  return (
    <g>
      <line x1={cx} x2={cx} y1={top} y2={top + h} stroke={color} strokeWidth={1} />
      <rect x={x} y={bodyTop} width={Math.max(width, 1)} height={bodyHeight} fill={color} />
    </g>
  )
}

function CandleTooltip({ active, payload, currency }) {
  if (!active || !payload?.length) return null
  const c = payload[0].payload
  const rows = [['Open', c.open], ['High', c.high], ['Low', c.low], ['Close', c.close]]
  return (
    <div className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs shadow">
      <p className="mb-1 font-medium text-slate-800">{c.date === c.end ? c.date : `${c.date} to ${c.end}`}</p>
      {rows.map(([k, v]) => (
        <p key={k} className="flex justify-between gap-4 text-slate-600"><span>{k}</span><span>{price(v)} {currency}</span></p>
      ))}
    </div>
  )
}

function Toggle({ options, value, onChange, label, size = 'md' }) {
  const pad = size === 'sm' ? 'px-2 py-0.5 text-xs' : 'px-2.5 py-1 text-sm'
  return (
    <div role="group" aria-label={label} className="inline-flex rounded-lg border border-slate-300 p-0.5">
      {options.map(([k, text, title]) => (
        <button key={k} type="button" title={title} onClick={() => onChange(k)} aria-pressed={value === k}
          className={`rounded-md ${pad} ${value === k ? 'bg-indigo-600 font-medium text-white' : 'text-slate-600 hover:bg-slate-100'}`}>
          {text}
        </button>
      ))}
    </div>
  )
}

export default function PriceChart({ asset, history }) {
  const [kind, setKind] = useState('line')
  const [range, setRange] = useState(null) // {from, to} ISO dates, or null for the full history
  const [preset, setPreset] = useState('ALL') // highlighted range button; null after a manual zoom or pan
  const [dragging, setDragging] = useState(false)
  const points = history?.points || []
  const candleSets = useMemo(
    () => Object.fromEntries(INTERVALS.map(([k]) => [k, buildCandles(points, k)])), [points])
  const hasOhlc = points.some((p) => p.open != null)
  const currency = history?.currency
  const showCandles = kind === 'candles' && hasOhlc

  const plotRef = useRef(null)
  const chartWidth = useElementWidth(plotRef)
  const drag = useRef(null)

  // Candle interval follows the visible range: the finest interval whose candles are at least
  // CANDLE_PX wide. It stays fixed during a drag so the pan keeps working on the same series.
  const autoInterval = useMemo(() => {
    const room = Math.max(chartWidth - Y_AXIS_WIDTH - MARGIN.right, 0)
    for (const [k] of INTERVALS) {
      const [a, b] = windowFor(candleSets[k], range)
      if ((b - a + 1) * CANDLE_PX <= room) return k
    }
    return INTERVALS[INTERVALS.length - 1][0]
  }, [candleSets, range, chartWidth])
  const interval = dragging && drag.current?.interval ? drag.current.interval : autoInterval
  const candles = candleSets[interval]
  const series = showCandles ? candles : points
  const [i0, i1] = windowFor(series, range)
  const visible = useMemo(() => series.slice(i0, i1 + 1), [series, i0, i1])
  const wheelAcc = useRef({ zoom: 0, pan: 0 })
  // Latest values for the native wheel listener, which is attached once.
  const live = useRef({})
  live.current = { series, win: [i0, i1] }

  const plotWidth = () => Math.max((plotRef.current?.clientWidth || 0) - Y_AXIS_WIDTH - MARGIN.right, 1)
  const applyWindow = (win, s = live.current.series) => {
    setRange(rangeFor(s, win[0], win[1]))
    setPreset(win[0] <= 0 && win[1] >= s.length - 1 ? 'ALL' : null)
  }

  // Wheel zooms around the cursor; a mostly horizontal scroll (trackpad) pans.
  // Native listener because React's wheel handler is passive and cannot stop the page from scrolling.
  useEffect(() => {
    const el = plotRef.current
    if (!el) return undefined
    const onWheel = (e) => {
      const { series: s, win } = live.current
      if (s.length < 2) return
      e.preventDefault()
      const unit = e.deltaMode === 1 ? 33 : e.deltaMode === 2 ? 300 : 1
      const dx = e.deltaX * unit, dy = e.deltaY * unit
      const n = win[1] - win[0] + 1
      const width = Math.max(el.clientWidth - Y_AXIS_WIDTH - MARGIN.right, 1)
      if (Math.abs(dx) > Math.abs(dy)) {
        wheelAcc.current.pan += (dx / width) * n
        const shift = Math.trunc(wheelAcc.current.pan)
        if (shift) {
          wheelAcc.current.pan -= shift
          applyWindow(panWindow(win, s.length, shift), s)
        }
        return
      }
      wheelAcc.current.zoom += clamp(dy, -200, 200) * 0.0015
      const factor = Math.exp(wheelAcc.current.zoom)
      const next = zoomWindow(win, s.length, factor, clamp((e.clientX - el.getBoundingClientRect().left - Y_AXIS_WIDTH) / width, 0, 1))
      if (next[0] !== win[0] || next[1] !== win[1]) {
        wheelAcc.current.zoom = 0
        applyWindow(next, s)
      } else if (Math.abs(wheelAcc.current.zoom) > 1) {
        wheelAcc.current.zoom = 0 // at a limit: don't build up a backlog
      }
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [points.length >= 2])

  const onPointerDown = (e) => {
    if (e.button !== 0 || series.length < 2) return
    drag.current = { x: e.clientX, win: [i0, i1], moved: false, id: e.pointerId, interval }
  }
  const onPointerMove = (e) => {
    const d = drag.current
    if (!d) return
    if (!(e.buttons & 1)) { endDrag(); return } // released outside the chart
    const dx = e.clientX - d.x
    if (!d.moved && Math.abs(dx) < 4) return
    if (!d.moved) {
      d.moved = true
      e.currentTarget.setPointerCapture(d.id)
      setDragging(true)
    }
    const n = d.win[1] - d.win[0] + 1
    applyWindow(panWindow(d.win, series.length, Math.round((-dx / plotWidth()) * n)))
  }
  const endDrag = () => {
    drag.current = null
    setDragging(false)
  }

  const choosePreset = (key, forKind = kind) => {
    setRange(presetRange(key, points, forKind))
    setPreset(key)
  }
  const zoomBy = (factor) => applyWindow(zoomWindow([i0, i1], series.length, factor, 0.5))
  // Keep the selected range button exact when switching chart type (1D/5D differ between line and candles).
  const changeKind = (k) => {
    setKind(k)
    if (preset) choosePreset(preset, k)
  }

  const vFirst = visible[0], vLast = visible[visible.length - 1]
  const shortSpan = vFirst && vLast && daysBetween(vFirst.date, itemEnd(vLast)) < 120
  const tickFormatter = (d) => (shortSpan ? d : d.slice(0, 7))
  const canZoomIn = visible.length > Math.min(MIN_BARS, series.length)
  const canZoomOut = visible.length < series.length

  let subtitle = null
  if (vFirst && showCandles) {
    subtitle = `${INTERVALS.find(([k]) => k === interval)[1]} candles in ${currency}, ${vFirst.date} to ${vLast.end}. `
      + 'Traded prices (split-adjusted, not dividend-adjusted).'
  } else if (vFirst) {
    subtitle = `Adjusted close in ${currency}, ${vFirst.date} to ${vLast.date}: ${price(vFirst.adj_close)} to ${price(vLast.adj_close)}`
  }

  return (
    <section className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <h1 className="text-2xl font-bold text-slate-900">{asset.name} <span className="text-slate-400">{asset.ticker}</span></h1>
        {points.length >= 2 && hasOhlc && (
          <div className="flex flex-wrap gap-2">
            <Toggle label="Chart type" options={[['line', 'Line'], ['candles', 'Candles']]} value={kind} onChange={changeKind} />
          </div>
        )}
      </div>
      {subtitle && <p className="mt-0.5 text-sm text-slate-500">{subtitle}</p>}

      <div ref={plotRef} className={`mt-4 select-none ${series.length >= 2 ? (dragging ? 'cursor-grabbing' : 'cursor-grab') : ''}`}
        style={{ touchAction: 'pan-y' }} onPointerDown={onPointerDown} onPointerMove={onPointerMove}
        onPointerUp={endDrag} onPointerCancel={endDrag} onDoubleClick={() => series.length >= 2 && choosePreset('ALL')}
        data-testid="price-plot">
        {points.length < 2 ? (
          <p className="text-sm text-slate-500">Price history appears once data_pipeline writes prices/&lt;TICKER&gt;.json.</p>
        ) : showCandles && !candles.length ? (
          <p className="text-sm text-slate-500">No open, high and low data for this asset.</p>
        ) : chartWidth <= 0 ? (
          <div style={{ height: HEIGHT }} aria-hidden="true" />
        ) : showCandles ? (
          <BarChart key={`${i0}-${i1}`} width={chartWidth} height={HEIGHT} data={visible} margin={MARGIN} barCategoryGap="12%">
              <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={50} tickFormatter={tickFormatter} />
              <YAxis tick={{ fontSize: 11 }} domain={['auto', 'auto']} width={Y_AXIS_WIDTH} tickFormatter={axisPrice} />
              {!dragging && <Tooltip content={<CandleTooltip currency={currency} />} cursor={{ fill: '#f1f5f9' }} />}
              <Bar dataKey="range" shape={<Candle />} isAnimationActive={false} maxBarSize={40} />
          </BarChart>
        ) : (
          <LineChart key={`${i0}-${i1}`} width={chartWidth} height={HEIGHT} data={visible} margin={MARGIN}>
              <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={50} tickFormatter={tickFormatter} />
              <YAxis tick={{ fontSize: 11 }} domain={['auto', 'auto']} width={Y_AXIS_WIDTH} tickFormatter={axisPrice} />
              {!dragging && <Tooltip formatter={(v) => [`${price(v)} ${currency}`, 'Adjusted close']} labelFormatter={(d) => d} />}
              <Line type="monotone" dataKey="adj_close" stroke="#4f46e5" dot={false} strokeWidth={1.5} isAnimationActive={false} />
          </LineChart>
        )}
      </div>

      {series.length >= 2 && (
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
          <Toggle size="sm" label="Visible range" value={preset} onChange={choosePreset}
            options={PRESETS.map(([k]) => [k, k, PRESET_TITLES[k]])} />
          <div className="flex items-center gap-2">
            <span className="hidden text-xs text-slate-400 sm:inline">Scroll to zoom · drag to pan · double-click to reset</span>
            <div role="group" aria-label="Zoom" className="inline-flex rounded-lg border border-slate-300 p-0.5 text-xs">
              <button type="button" title="Zoom out" aria-label="Zoom out" disabled={!canZoomOut} onClick={() => zoomBy(ZOOM_STEP)}
                className="rounded-md px-2 py-0.5 text-slate-600 hover:bg-slate-100 disabled:opacity-40">−</button>
              <button type="button" title="Zoom in" aria-label="Zoom in" disabled={!canZoomIn} onClick={() => zoomBy(1 / ZOOM_STEP)}
                className="rounded-md px-2 py-0.5 text-slate-600 hover:bg-slate-100 disabled:opacity-40">+</button>
              <button type="button" title="Show the full history" disabled={!canZoomOut} onClick={() => choosePreset('ALL')}
                className="rounded-md px-2 py-0.5 text-slate-600 hover:bg-slate-100 disabled:opacity-40">Reset</button>
            </div>
          </div>
        </div>
      )}

      {points.length >= 2 && (
        <p className="mt-2 text-xs text-slate-400">
          {showCandles
            ? 'Green: closed at or above the open. Red: closed below. Candle levels can sit above the line chart, which uses adj_close (back-adjusted for dividends). '
            : 'Yahoo back-adjusts adj_close for splits and dividends. '}
          Listing currency, not converted to CAD. The 31 assets were chosen in 2026 (survivorship bias). Past prices do not predict future returns.
        </p>
      )}
    </section>
  )
}
