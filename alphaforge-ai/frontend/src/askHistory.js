// Ask AlphaForge conversations, kept outside the page so they survive navigation.
// One conversation per user (persona), saved to sessionStorage: it survives page reloads
// in the same tab and is cleared when the tab closes. Requests still running when the user
// leaves the page finish here and are added to the right conversation.
import { useSyncExternalStore } from 'react'

const KEY = 'af_ask_history_v1'
const MAX_MESSAGES = 60 // per user; oldest are dropped first

const load = () => {
  try {
    const raw = JSON.parse(sessionStorage.getItem(KEY) || '{}')
    return raw && typeof raw === 'object' ? raw : {}
  } catch {
    return {}
  }
}

let threads = load() // { [userId]: [{ role: 'user', text } | { role: 'agent', a }] }
let pending = {} // { [userId]: { busy, error } }, memory only
const listeners = new Set()
const EMPTY = Object.freeze([])
const IDLE = Object.freeze({ busy: false, error: null })

function save() {
  // Answers carry evidence and traces; if storage is full, keep the newest messages that fit.
  let copy = threads
  for (let attempt = 0; attempt < 8; attempt++) {
    try {
      sessionStorage.setItem(KEY, JSON.stringify(copy))
      return
    } catch {
      copy = Object.fromEntries(Object.entries(copy).map(([u, t]) => [u, t.slice(Math.ceil(t.length / 2))]))
    }
  }
}

function emit() {
  listeners.forEach((l) => l())
}

function setThread(user, fn) {
  threads = { ...threads, [user]: fn(threads[user] || EMPTY).slice(-MAX_MESSAGES) }
  save()
  emit()
}

function setPending(user, value) {
  pending = { ...pending, [user]: value }
  emit()
}

const subscribe = (l) => {
  listeners.add(l)
  return () => listeners.delete(l)
}

export function useAskHistory(user) {
  const thread = useSyncExternalStore(subscribe, () => threads[user] || EMPTY)
  const status = useSyncExternalStore(subscribe, () => pending[user] || IDLE)
  return { thread, ...status }
}

export function clearHistory(user) {
  setThread(user, () => EMPTY)
  setPending(user, IDLE)
}

// `ask` runs the request (and any polling) and resolves with the finished answer.
export async function sendQuestion(user, text, ask) {
  if ((pending[user] || IDLE).busy) return
  setPending(user, { busy: true, error: null })
  setThread(user, (t) => [...t, { role: 'user', text }])
  try {
    const a = await ask(text)
    setThread(user, (t) => [...t, { role: 'agent', a }])
    setPending(user, IDLE)
  } catch (e) {
    setPending(user, { busy: false, error: { message: String(e?.message || e) } })
  }
}
