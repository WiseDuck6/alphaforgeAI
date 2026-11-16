import { NavLink } from 'react-router-dom'
import { useUser } from '../UserContext.jsx'

const NAV = [
  ['/', 'Dashboard'],
  ['/profile', 'Investor profile'],
  ['/holdings', 'My holdings'],
  ['/assets', 'My top picks'],
  ['/ask', 'Ask AlphaForge'],
  ['/monitoring', 'Monitoring'],
  ['/data', 'Data & limits'],
]

const USERS = [
  ['amira', 'Amira (demo persona)'],
  ['mateo', 'Mateo (demo persona)'],
  ['robert', 'Robert (demo persona)'],
  ['me', 'My own profile'],
]

export default function Layout({ children, placeholder }) {
  const { userId, switchUser, meta } = useUser()
  return (
    <div className="flex min-h-screen">
      <aside className="hidden w-56 shrink-0 flex-col border-r border-slate-200 bg-white md:flex">
        <div className="px-5 py-5">
          <p className="text-lg font-bold tracking-tight text-slate-900">AlphaForge <span className="text-indigo-600">AI</span></p>
          <p className="text-xs text-slate-500">Explainable investment simulator</p>
        </div>
        <nav className="flex flex-col gap-0.5 px-3">
          {NAV.map(([to, text]) => (
            <NavLink key={to} to={to} end={to === '/'}
              className={({ isActive }) => `rounded-lg px-3 py-2 text-sm ${isActive ? 'bg-indigo-50 font-medium text-indigo-700' : 'text-slate-600 hover:bg-slate-50'}`}>
              {text}
            </NavLink>
          ))}
        </nav>
        <p className="mt-auto px-5 py-4 text-[11px] leading-snug text-slate-400">{meta?.notice}</p>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 bg-white px-6 py-3">
          <nav className="flex gap-3 overflow-x-auto text-sm md:hidden">
            {NAV.map(([to, text]) => <NavLink key={to} to={to} end={to === '/'} className="whitespace-nowrap text-slate-600">{text}</NavLink>)}
          </nav>
          <span className="hidden text-sm text-slate-500 md:block">Simulation only · fictional portfolios · not financial advice</span>
          <label className="flex items-center gap-2 text-sm text-slate-600">
            Viewing as
            <select value={userId} onChange={(e) => switchUser(e.target.value)}
              className="rounded-lg border border-slate-300 bg-white px-2 py-1 text-sm">
              {USERS.map(([id, text]) => <option key={id} value={id}>{text}</option>)}
            </select>
          </label>
        </header>
        {placeholder && (
          <div className="border-b border-amber-200 bg-amber-50 px-6 py-2 text-sm text-amber-900">
            Placeholder metrics: numbers are rounded exploratory figures, not pipeline output. Run the data pipeline before drawing conclusions.
          </div>
        )}
        <main className="mx-auto w-full max-w-7xl flex-1 p-6">{children}</main>
      </div>
    </div>
  )
}
