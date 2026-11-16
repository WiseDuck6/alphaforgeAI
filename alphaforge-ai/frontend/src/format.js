export const pct = (x, digits = 0) =>
  x === null || x === undefined || Number.isNaN(x) ? 'n/a' : `${(x * 100).toFixed(digits)}%`

export const signedPct = (x, digits = 0) =>
  x === null || x === undefined ? 'n/a' : `${x > 0 ? '+' : ''}${(x * 100).toFixed(digits)}%`

export const money = (x, currency = 'CAD') =>
  new Intl.NumberFormat('en-CA', { style: 'currency', currency, maximumFractionDigits: 0 }).format(x || 0)

export const num = (x, digits = 0) => (x === null || x === undefined ? 'n/a' : Number(x).toFixed(digits))

export const label = (s) => (s || '').replaceAll('_', ' ')

export const RISK_LABELS = { low: 'Low', low_moderate: 'Low to moderate', moderate: 'Moderate', high: 'High' }

// Large CAD amounts: 7.4T, 151B, 12.3M.
export const compactMoney = (x) => {
  if (x === null || x === undefined) return 'n/a'
  const a = Math.abs(x)
  const [d, s] = a >= 1e12 ? [1e12, 'T'] : a >= 1e9 ? [1e9, 'B'] : a >= 1e6 ? [1e6, 'M'] : a >= 1e3 ? [1e3, 'k'] : [1, '']
  return `${x < 0 ? '-' : ''}$${(a / d).toFixed(a / d >= 100 ? 0 : 1)}${s}`
}

// Format a value by the unit the backend attached to it (feature definitions and evidence).
export function byUnit(v, unit) {
  if (v === null || v === undefined) return 'n/a'
  if (Array.isArray(v)) return v.length ? v.join(', ') : 'none'
  if (typeof v !== 'number') return label(String(v))
  switch (unit) {
    case 'fraction': return `${(v * 100).toFixed(Math.abs(v) < 0.1 ? 1 : 0)}%`
    case 'percent': return `${v.toFixed(2)}%`
    case 'ratio': return v.toFixed(2)
    case 'sentiment': return `${v > 0 ? '+' : ''}${v.toFixed(2)}`
    case 'index': return v.toFixed(0)
    case 'days':
    case 'count': return String(Math.round(v))
    case 'cad': return compactMoney(v)
    case 'money_local': return compactMoney(v).replace('$', '')
    case 'price': return v.toLocaleString('en-CA', { maximumFractionDigits: 2 })
    case 'number': return Math.round(v).toLocaleString('en-CA')
    default: return Math.abs(v) <= 1.5 && !Number.isInteger(v) ? `${(v * 100).toFixed(1)}%` : String(v)
  }
}
