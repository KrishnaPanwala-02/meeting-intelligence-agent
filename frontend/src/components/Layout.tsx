import { useEffect, useState } from 'react'
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { systemApi } from '../api/endpoints'
import type { Readiness } from '../api/types'
import {
  IconAlert,
  IconCalendar,
  IconCheck,
  IconFolder,
  IconHome,
  IconLogout,
  IconMenu,
  IconMic,
  IconMoon,
  IconSettings,
  IconSun,
  IconChart,
  IconSearch,
  IconSparkle,
} from './icons'
import BackButton from './BackButton'
import NotificationBell from './NotificationBell'
import { Button } from './ui'
import { initials } from '../lib/format'

type Theme = 'light' | 'dark' | 'system'
const THEME_KEY = 'mia.theme'

function readTheme(): Theme {
  try {
    const t = localStorage.getItem(THEME_KEY)
    if (t === 'light' || t === 'dark') return t
  } catch {
    /* ignore */
  }
  return 'system'
}

function applyTheme(t: Theme) {
  const root = document.documentElement
  if (t === 'system') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', t)
}

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(readTheme)
  useEffect(() => {
    applyTheme(theme)
    try {
      if (theme === 'system') localStorage.removeItem(THEME_KEY)
      else localStorage.setItem(THEME_KEY, theme)
    } catch {
      /* ignore */
    }
  }, [theme])
  const isDark =
    theme === 'dark' || (theme === 'system' && window.matchMedia?.('(prefers-color-scheme: dark)').matches)
  return { theme, isDark, toggle: () => setTheme(isDark ? 'light' : 'dark') }
}

const NAV = [
  { to: '/', label: 'Dashboard', icon: <IconHome />, end: true },
  { to: '/meetings', label: 'Meetings', icon: <IconMic /> },
  { to: '/folders', label: 'Folders', icon: <IconFolder /> },
  { to: '/tasks', label: 'My tasks', icon: <IconCheck /> },
  { to: '/analytics', label: 'Analytics', icon: <IconChart /> },
  { to: '/search', label: 'Search', icon: <IconSearch /> },
  { to: '/ask', label: 'Ask', icon: <IconSparkle /> },
  { to: '/calendar', label: 'Calendar', icon: <IconCalendar /> },
  { to: '/settings', label: 'Settings', icon: <IconSettings /> },
]

function StatusBanner() {
  const [r, setR] = useState<Readiness | null | undefined>(undefined)
  useEffect(() => {
    let live = true
    const check = async () => {
      const v = await systemApi.ready()
      if (live) setR(v)
    }
    void check()
    const id = setInterval(check, 30_000)
    return () => {
      live = false
      clearInterval(id)
    }
  }, [])

  if (r === undefined) return null
  let text: string | null = null
  let tone = 'warn'
  if (r === null) {
    text = 'Cannot reach the backend. Start it with “docker compose up -d”.'
    tone = 'danger'
  } else if (r.status === 'not_ready') {
    const down = [r.checks.database === 'fail' && 'database', r.checks.redis === 'fail' && 'Redis'].filter(Boolean)
    text = `Backend is not ready (${down.join(' and ')} unavailable).`
    tone = 'danger'
  } else if (r.checks.ollama === 'fail') {
    text = 'The AI engine (Ollama) is not running. Uploads work, but summaries and action items will fail until you start it.'
  }
  if (!text) return null
  return (
    <div className={`alert ${tone}`} role="status" style={{ marginBottom: '1.2rem' }}>
      <IconAlert />
      <div>{text}</div>
    </div>
  )
}

/** Accounts made before names were collected are asked to add theirs once. */
function NameBanner() {
  const { user } = useAuth()
  const loc = useLocation()
  if (!user || user.name || loc.pathname === '/settings') return null
  return (
    <div className="alert warn" role="status" style={{ marginBottom: '1.2rem' }}>
      <IconAlert />
      <div className="grow">Add your full name so your voice and tasks can be matched to you.</div>
      <Link className="btn sm" to="/settings">
        Add name
      </Link>
    </div>
  )
}

export default function Layout() {
  const { user, logout } = useAuth()
  const { isDark, toggle } = useTheme()
  const [open, setOpen] = useState(false)
  const loc = useLocation()

  useEffect(() => setOpen(false), [loc.pathname])

  const sidebar = (
    <aside className={`sidebar ${open ? 'open' : ''}`} aria-label="Main">
      <div className="brand">
        <span className="logo">
          <IconMic size={17} />
        </span>
        Meeting Intelligence
      </div>
      <nav className="nav">
        {NAV.map((n) => (
          <NavLink key={n.to} to={n.to} end={n.end}>
            {n.icon}
            {n.label}
          </NavLink>
        ))}
      </nav>
      <div className="sidebar-foot">
        <button className="btn ghost" onClick={toggle} style={{ justifyContent: 'flex-start' }}>
          {isDark ? <IconSun /> : <IconMoon />}
          {isDark ? 'Light mode' : 'Dark mode'}
        </button>
        <div className="usercard">
          <span className="avatar">{initials(user?.name || user?.email || '')}</span>
          <div className="grow">
            <div className="truncate small" style={{ fontWeight: 600 }} title={user?.email}>
              {user?.name || user?.email}
            </div>
            {user?.name && <div className="truncate muted small">{user.email}</div>}
            {user?.is_admin && <span className="muted small">Administrator</span>}
          </div>
          <Button variant="ghost" size="sm" aria-label="Sign out" onClick={() => void logout()}>
            <IconLogout />
          </Button>
        </div>
      </div>
    </aside>
  )

  return (
    <div className="shell">
      {sidebar}
      {open && <div className="scrim" onClick={() => setOpen(false)} />}
      <div>
        <div className="topbar">
          <Button variant="ghost" aria-label="Open menu" onClick={() => setOpen(true)}>
            <IconMenu />
          </Button>
          <strong>Meeting Intelligence</strong>
        </div>
        <main className="main">
          <StatusBanner />
          <NameBanner />
          <div className="main-top">
            {loc.pathname !== '/' ? <BackButton /> : <span />}
            <NotificationBell />
          </div>
          <Outlet />
        </main>
      </div>
    </div>
  )
}
