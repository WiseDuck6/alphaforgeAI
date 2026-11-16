// API client. In development Vite proxies /api to the local Python server.
// In production set VITE_API_BASE to the API Gateway URL.
const BASE = (import.meta.env.VITE_API_BASE || `${import.meta.env.BASE_URL.replace(/\/$/, '')}/api`).replace(/\/$/, '')
const AUTH_MODE = import.meta.env.VITE_AUTH_MODE || 'header'

export const getUserId = () => localStorage.getItem('af_user') || 'robert'
export const setUserId = (id) => localStorage.setItem('af_user', id)

// `asUser` pins a request to a given user (header mode only). Used by Ask, whose job polling can
// outlive a persona switch: the job belongs to the user who asked, and the API rejects anyone else.
function authHeaders(asUser) {
  if (AUTH_MODE === 'cognito') {
    // Set by the Cognito sign-in flow (see docs/aws_deployment.md, step 8).
    const token = sessionStorage.getItem('af_id_token')
    return token ? { Authorization: `Bearer ${token}` } : {}
  }
  return { 'X-User-Id': asUser || getUserId() }
}

export class ApiError extends Error {
  constructor(status, message) {
    super(message)
    this.status = status
  }
}

let requestSeq = 0

async function request(method, path, body, asUser) {
  // A unique query parameter on reads, so no proxy or CDN between the browser and the API
  // (e.g. the code-server CloudFront distribution) can answer with a stored copy. The API ignores it.
  const url = method === 'GET'
    ? `${BASE}${path}${path.includes('?') ? '&' : '?'}_=${Date.now().toString(36)}${(requestSeq++).toString(36)}`
    : `${BASE}${path}`
  const res = await fetch(url, {
    method,
    // Answers depend on the user (X-User-Id / token), not only the URL: never reuse a cached copy.
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json', ...authHeaders(asUser) },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new ApiError(res.status, data.error || res.statusText)
  return data
}

const qs = (params) => {
  const p = Object.entries(params || {}).filter(([, v]) => v !== undefined && v !== null && v !== '')
  return p.length ? `?${new URLSearchParams(p)}` : ''
}

export const api = {
  meta: () => request('GET', '/meta'),
  presets: () => request('GET', '/presets'),
  profile: () => request('GET', '/me/profile'),
  saveProfile: (profile) => request('PUT', '/me/profile', profile),
  loadPreset: (id) => request('POST', `/me/profile/preset/${id}`),
  dashboard: (params) => request('GET', `/me/dashboard${qs(params)}`),
  assets: (params) => request('GET', `/assets${qs(params)}`),
  news: (params) => request('GET', `/news${qs(params)}`),
  marketRegime: () => request('GET', '/market/regime'),
  asset: (ticker, params) => request('GET', `/assets/${encodeURIComponent(ticker)}${qs(params)}`),
  sparklines: () => request('GET', '/prices/sparklines'),
  check: (proposal, window) => request('POST', '/governance/check', { proposal, window }),
  ask: (question, extra, asUser) => request('POST', '/ask', { question, ...extra }, asUser),
  askJob: (jobId, asUser) => request('GET', `/ask/${jobId}`, undefined, asUser),
  sources: () => request('GET', '/sources'),
  setSourceRate: (id, rate) => request('PUT', `/sources/${id}`, { rate }),
  alerts: () => request('GET', '/alerts'),
  dataStatus: () => request('GET', '/data/status'),
}
