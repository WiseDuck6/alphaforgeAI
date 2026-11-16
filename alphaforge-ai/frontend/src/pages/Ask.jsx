import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { clearHistory, sendQuestion, useAskHistory } from '../askHistory.js'
import { useApi, useUser } from '../UserContext.jsx'
import GovernanceResult from '../components/GovernanceResult.jsx'
import { Badge, Card, ErrorBox } from '../components/ui.jsx'
import { BulletList, CitedText, EvidenceList, NewsList } from '../components/Evidence.jsx'

const GENERIC = [
  'Which assets fit me best?',
  'Why is NVDA ranked where it is?',
  'Why did Shopify fall in 2022?',
  "What's the latest news on my holdings?",
  'What is the market regime right now?',
  'Compare SPY and ESGV',
  'How would my portfolio have done in 2022?',
  'How can I diversify my portfolio?',
]
const TABS = ['Answer', 'Evidence', 'News', 'Assumptions', 'Limits', 'Trace']

function TraceView({ trace }) {
  if (!trace?.length) return <p className="text-sm text-slate-500">No trace steps.</p>
  return (
    <ol className="space-y-2">
      {trace.map((t, i) => (
        <li key={i} className="rounded-lg bg-slate-50 p-2 font-mono text-xs text-slate-700">
          <span className="font-semibold">{t.step ?? i + 1}. {t.agent ? `${t.agent} → ` : ''}{t.tool}</span>
          {t.input && <pre className="mt-1 whitespace-pre-wrap text-slate-500">in: {JSON.stringify(t.input)}</pre>}
          {t.output && <pre className="mt-1 whitespace-pre-wrap text-slate-500">out: {typeof t.output === 'string' ? t.output : JSON.stringify(t.output)}</pre>}
        </li>
      ))}
    </ol>
  )
}

function Reasoning({ steps, onCite }) {
  const [open, setOpen] = useState(null)
  if (!steps?.length) return null
  return (
    <div className="rounded-lg border border-slate-200 bg-slate-50/60 p-3">
      <p className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500">How AlphaForge reasoned ({steps.length} steps)</p>
      <ol className="relative space-y-2 border-l border-indigo-200 pl-4">
        {steps.map((s) => (
          <li key={s.step} className="relative">
            <span className="absolute -left-[1.4rem] top-0.5 flex h-4 w-4 items-center justify-center rounded-full bg-indigo-600 text-[9px] font-bold text-white">{s.step}</span>
            <button type="button" className="text-left" onClick={() => setOpen(open === s.step ? null : s.step)}>
              <span className="text-sm font-medium text-slate-900">{s.title}</span>
              {s.tool && <span className="ml-2 font-mono text-[10px] text-slate-400">{s.tool}</span>}
              {s.details?.length > 0 && <span className="ml-2 text-xs text-indigo-600">{open === s.step ? 'hide' : `details (${s.details.length})`}</span>}
            </button>
            <p className="text-sm text-slate-600"><CitedText text={s.summary} onCite={onCite} /></p>
            {open === s.step && (
              <ul className="mt-1 list-disc space-y-1 pl-5 text-xs text-slate-600">
                {s.details.map((d, i) => <li key={i}><CitedText text={d} onCite={onCite} /></li>)}
              </ul>
            )}
          </li>
        ))}
      </ol>
      <p className="mt-2 text-[11px] text-slate-400">Steps summarise what was looked at and which rule decided. Numbers in brackets point to the evidence list.</p>
    </div>
  )
}

function AnswerCard({ a }) {
  const [tab, setTab] = useState('Answer')
  const [hl, setHl] = useState(null)
  const tone = { high: 'green', medium: 'amber', low: 'red' }[a.confidence?.level] || 'slate'
  const cite = (n) => {
    setHl(n)
    setTab('Evidence')
    setTimeout(() => document.getElementById(`ev-${n}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 50)
  }
  const counts = { Evidence: a.evidence?.length || 0, News: a.news?.length || 0 }
  return (
    <div className="rounded-xl border border-slate-200 bg-white shadow-sm">
      <div className="flex flex-wrap items-center gap-2 border-b border-slate-100 px-4 py-2">
        <Badge tone="blue">{a.mode === 'agent' ? 'AgentCore agent' : 'Local reasoning engine (no LLM)'}</Badge>
        {a.parsed?.intent_label && <Badge>{a.parsed.intent_label}</Badge>}
        <Badge tone={tone}>confidence: {a.confidence?.level}</Badge>
        {a.verification && (
          <Badge tone={a.verification.unmatched.length ? 'red' : 'green'} title={a.verification.method}>
            figures verified: {a.verification.matched}/{a.verification.checked}
          </Badge>
        )}
        <div className="ml-auto flex flex-wrap gap-1">
          {TABS.map((t) => (
            <button key={t} onClick={() => setTab(t)}
              className={`rounded-md px-2 py-1 text-xs ${tab === t ? 'bg-indigo-600 text-white' : 'text-slate-600 hover:bg-slate-100'}`}>
              {t}{counts[t] !== undefined ? ` (${counts[t]})` : ''}
            </button>
          ))}
        </div>
      </div>
      <div className="p-4">
        {tab === 'Answer' && (
          <div className="space-y-4">
            <p className="whitespace-pre-line text-sm leading-relaxed text-slate-800"><CitedText text={a.answer} onCite={cite} /></p>
            <Reasoning steps={a.reasoning} onCite={cite} />
            {a.governance && (
              <details><summary className="cursor-pointer text-sm text-indigo-600">Governance check details</summary>
                <div className="mt-2"><GovernanceResult result={a.governance} /></div></details>
            )}
          </div>
        )}
        {tab === 'Evidence' && <EvidenceList items={a.evidence} highlight={hl} />}
        {tab === 'News' && <NewsList items={a.news} onCite={cite} empty="No news or filings were cited in this answer." />}
        {tab === 'Assumptions' && <BulletList items={a.assumptions} />}
        {tab === 'Limits' && <BulletList items={[...a.limitations, ...(a.confidence?.reasons || [])]} />}
        {tab === 'Trace' && <TraceView trace={a.trace} />}
        <p className="mt-3 text-xs text-slate-400">{a.notice} · trace {a.trace_id.slice(0, 8)}</p>
      </div>
    </div>
  )
}

export default function Ask() {
  const { userId } = useUser()
  const presets = useApi(() => api.presets(), [])
  // The conversation lives in askHistory.js, not in this page, so it is still there after
  // visiting other pages. Each persona keeps its own conversation.
  const { thread, busy, error } = useAskHistory(userId)
  const [question, setQuestion] = useState('')
  const end = useRef(null)
  const shown = useRef(false)

  // Braces matter: newer browsers return a Promise from scrollIntoView, and an
  // effect may only return a cleanup function.
  useEffect(() => {
    // Jump straight to the latest message when coming back; scroll smoothly for new ones.
    end.current?.scrollIntoView({ behavior: shown.current ? 'smooth' : 'auto' })
    shown.current = true
  }, [thread, busy])

  const demo = presets.data?.find((p) => p.id === userId)?.demo_question
  const prompts = demo ? [demo, ...GENERIC] : ['Should I put 30% of my portfolio in Tesla?', ...GENERIC]

  const send = (q) => {
    const text = (q ?? question).trim()
    if (!text || busy) return
    setQuestion('')
    // The user id is fixed when the question is sent, so the answer joins the right
    // conversation even if the persona or the page changes before it arrives.
    const asker = userId
    sendQuestion(asker, text, async (t) => {
      let a = await api.ask(t, undefined, asker)
      // Agent mode answers asynchronously: poll the job for up to ~2 minutes, always as the asker.
      for (let i = 0; a.status === 'pending' && i < 60; i++) {
        await new Promise((r) => setTimeout(r, 2000))
        a = await api.askJob(a.job_id, asker)
      }
      if (a.status !== 'done') throw new Error(a.error || 'The agent did not answer in time; try again.')
      return a
    })
  }

  return (
    <div className="mx-auto max-w-4xl space-y-4">
      <Card title="Ask AlphaForge" subtitle="Every answer shows how it was reasoned, its evidence and news sources, assumptions, limits and tool calls"
        actions={thread.length > 0 && (
          <button type="button" onClick={() => clearHistory(userId)} disabled={busy}
            className="rounded-lg border border-slate-300 px-3 py-1 text-xs text-slate-600 hover:border-rose-300 hover:text-rose-700 disabled:opacity-50">
            Clear chat
          </button>
        )}>
        <div className="flex flex-wrap gap-2">
          {prompts.map((p) => (
            <button key={p} onClick={() => send(p)} disabled={busy}
              className="rounded-full border border-slate-300 px-3 py-1 text-xs text-slate-700 hover:border-indigo-400 hover:text-indigo-700">{p}</button>
          ))}
        </div>
      </Card>
      <div className="space-y-4">
        {thread.map((m, i) => m.role === 'user'
          ? <div key={i} className="ml-auto max-w-xl rounded-xl bg-indigo-600 px-4 py-2 text-sm text-white">{m.text}</div>
          : <AnswerCard key={i} a={m.a} />)}
        {busy && <p className="animate-pulse text-sm text-slate-500">Planning, gathering evidence and checking constraints...</p>}
        <ErrorBox error={error} />
        <div ref={end} />
      </div>
      <form onSubmit={(e) => { e.preventDefault(); send() }} className="sticky bottom-4 flex gap-2 rounded-xl border border-slate-200 bg-white p-2 shadow">
        <input value={question} onChange={(e) => setQuestion(e.target.value)} maxLength={2000}
          placeholder="Ask about an asset, a ranking, news, a price move, an allocation or the market"
          className="flex-1 rounded-lg px-3 py-2 text-sm outline-none" />
        <button disabled={busy || !question.trim()} className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50">Ask</button>
      </form>
    </div>
  )
}
