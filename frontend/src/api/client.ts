// HTTP client: bearer-token handling, one-shot transparent refresh, and
// human-readable error messages.
//
// Each browser TAB keeps its own session (access and refresh token in
// sessionStorage), so two tabs can be signed in as different people (say,
// Krishna and Rahul) without overwriting each other. A reload keeps the tab
// signed in; closing the tab ends the session.
//
// The server rotates the refresh token on every use and treats re-use of a
// consumed one as theft (it revokes the session). Tabs that share ONE session
// (a duplicated tab) therefore hand each new token pair to each other through a
// short-lived localStorage entry, and refresh one at a time (Web Locks).

import type { TokenPair } from './types'

const REFRESH_KEY = 'mia.refresh'
const ACCESS_KEY = 'mia.access'
const HANDOFF_KEY = 'mia.handoff' // localStorage: lets tabs of the SAME session follow a rotation
const HANDOFF_MAX_AGE_MS = 120_000

let accessToken: string | null = null
let refreshInFlight: Promise<boolean> | null = null
let sessionLostHandler: (() => void) | null = null
let refreshTimer: ReturnType<typeof setTimeout> | null = null

export class ApiError extends Error {
  status: number
  detail: unknown
  retryAfter?: number

  constructor(status: number, message: string, detail?: unknown, retryAfter?: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.retryAfter = retryAfter
  }
}

interface Handoff {
  from: string // the refresh token the other tab was holding
  pair?: TokenPair
  loggedOut?: boolean
  at: number
}

function readHandoff(): Handoff | null {
  try {
    const raw = localStorage.getItem(HANDOFF_KEY)
    if (!raw) return null
    const h = JSON.parse(raw) as Handoff
    return h && typeof h.from === 'string' && Date.now() - h.at < HANDOFF_MAX_AGE_MS ? h : null
  } catch {
    return null
  }
}

function writeHandoff(h: Omit<Handoff, 'at'>) {
  try {
    const at = Date.now()
    localStorage.setItem(HANDOFF_KEY, JSON.stringify({ ...h, at }))
    // Best effort: do not leave tokens lying in shared storage.
    setTimeout(() => {
      try {
        const cur = JSON.parse(localStorage.getItem(HANDOFF_KEY) ?? 'null') as Handoff | null
        if (cur && cur.at === at) localStorage.removeItem(HANDOFF_KEY)
      } catch {
        /* ignore */
      }
    }, HANDOFF_MAX_AGE_MS)
  } catch {
    /* storage unavailable */
  }
}

/** Another tab rotated (or ended) the session this tab shares: follow it. True if followed. */
function followHandoff(): boolean {
  const h = readHandoff()
  const mine = tokens.refresh
  if (!h || !mine || h.from !== mine) return false
  if (h.loggedOut) {
    tokens.clear({ silent: true })
    sessionLostHandler?.()
    return true
  }
  if (h.pair) {
    tokens.set(h.pair, { silent: true })
    return true
  }
  return false
}

export const tokens = {
  get access() {
    return accessToken
  },
  get refresh(): string | null {
    try {
      const own = sessionStorage.getItem(REFRESH_KEY)
      if (own) return own
      // One-time upgrade from the old shared storage: the first tab adopts it.
      const legacy = localStorage.getItem(REFRESH_KEY)
      if (legacy) {
        sessionStorage.setItem(REFRESH_KEY, legacy)
        localStorage.removeItem(REFRESH_KEY)
        return legacy
      }
    } catch {
      /* storage unavailable */
    }
    return null
  },
  set(pair: TokenPair, opts: { silent?: boolean } = {}) {
    const before = tokens.refresh
    accessToken = pair.access_token
    try {
      sessionStorage.setItem(REFRESH_KEY, pair.refresh_token)
      sessionStorage.setItem(
        ACCESS_KEY,
        JSON.stringify({ token: pair.access_token, exp: Date.now() + pair.expires_in * 1000 }),
      )
    } catch {
      /* storage unavailable: the session just will not survive a reload */
    }
    if (!opts.silent && before && before !== pair.refresh_token) writeHandoff({ from: before, pair })
    scheduleRefresh(pair.expires_in)
  },
  /**
   * After a page reload, reuse this tab's still-valid access token instead of
   * spending a refresh (the refresh token rotates on every use, and reloads
   * are frequent). Returns whether a usable token was restored.
   */
  restoreAccess(): boolean {
    try {
      const raw = sessionStorage.getItem(ACCESS_KEY)
      if (!raw) return false
      const { token, exp } = JSON.parse(raw) as { token: string; exp: number }
      const left = (exp - Date.now()) / 1000
      if (!token || left <= 30) return false
      accessToken = token
      scheduleRefresh(left)
      return true
    } catch {
      return false
    }
  },
  clear(opts: { silent?: boolean } = {}) {
    const before = tokens.refresh
    accessToken = null
    if (refreshTimer) clearTimeout(refreshTimer)
    refreshTimer = null
    try {
      sessionStorage.removeItem(REFRESH_KEY)
      sessionStorage.removeItem(ACCESS_KEY)
      localStorage.removeItem(REFRESH_KEY)
    } catch {
      /* ignore */
    }
    // Tabs that share this session (a duplicated tab) are signed out with it.
    if (!opts.silent && before) writeHandoff({ from: before, loggedOut: true })
  },
}

if (typeof window !== 'undefined') {
  window.addEventListener('storage', (e) => {
    if (e.key === HANDOFF_KEY && e.newValue) followHandoff()
  })
}

/** Called when the session can no longer be renewed (e.g. revoked elsewhere). */
export function onSessionLost(handler: (() => void) | null) {
  sessionLostHandler = handler
}

function scheduleRefresh(expiresInSeconds: number) {
  if (refreshTimer) clearTimeout(refreshTimer)
  // Renew a minute before expiry so users are never bounced mid-action.
  const ms = Math.max(5, expiresInSeconds - 60) * 1000
  refreshTimer = setTimeout(() => {
    void refreshSession()
  }, ms)
}

/** Turn a FastAPI error body into a sentence a person can act on. */
export function describeDetail(detail: unknown, status: number): string {
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail)) {
    const parts = detail.map((d) => {
      if (d && typeof d === 'object' && 'msg' in d) {
        const loc = Array.isArray((d as { loc?: unknown[] }).loc)
          ? ((d as { loc: unknown[] }).loc.filter((x) => x !== 'body' && x !== 'query') as string[]).join('.')
          : ''
        const msg = String((d as { msg: unknown }).msg)
        return loc ? `${loc}: ${msg}` : msg
      }
      return String(d)
    })
    if (parts.length) return parts.join('; ')
  }
  if (detail && typeof detail === 'object' && 'message' in detail) {
    return String((detail as { message: unknown }).message)
  }
  if (status === 401) return 'Your session has ended. Please sign in again.'
  if (status === 403) return 'You do not have permission to do that.'
  if (status === 404) return 'Not found.'
  if (status === 429) return 'Too many requests. Please wait a moment and try again.'
  if (status >= 500) return 'The server had a problem. Please try again.'
  return `Request failed (${status}).`
}

export function buildQuery(params?: Record<string, string | number | boolean | undefined | null>): string {
  if (!params) return ''
  const q = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue
    q.set(k, String(v))
  }
  const s = q.toString()
  return s ? `?${s}` : ''
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  json?: unknown
  form?: FormData
  query?: Record<string, string | number | boolean | undefined | null>
  /** Send the bearer token and renew it on 401 (default true). */
  auth?: boolean
  as?: 'json' | 'blob'
  signal?: AbortSignal
}

async function send(path: string, opts: RequestOptions): Promise<Response> {
  const headers: Record<string, string> = {}
  if (opts.auth !== false && accessToken) headers.Authorization = `Bearer ${accessToken}`
  let body: BodyInit | undefined
  if (opts.form) {
    body = opts.form // the browser sets the multipart boundary
  } else if (opts.json !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(opts.json)
  }
  try {
    return await fetch(path + buildQuery(opts.query), {
      method: opts.method ?? 'GET',
      headers,
      body,
      signal: opts.signal,
    })
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e
    throw new ApiError(0, 'Cannot reach the server. Is the backend running?')
  }
}

const REFRESH_TIMEOUT_MS = 15_000
const LOCK_WAIT_MS = 5_000

/**
 * Run the refresh while holding the browser-wide lock, so two tabs of one session do not spend the same
 * single-use token. A tab that was frozen or slept in the background can hold the lock for ever, which
 * would leave every other tab on "Restoring your session…": after a short wait the holder is presumed
 * gone and the refresh goes ahead without the lock (it still follows a rotation another tab made).
 */
async function withLock(locks: LockManager, run: () => Promise<boolean>): Promise<boolean> {
  try {
    return await locks.request('mia-refresh', { signal: AbortSignal.timeout(LOCK_WAIT_MS) }, run)
  } catch (e) {
    if (e instanceof DOMException && (e.name === 'TimeoutError' || e.name === 'AbortError')) return run()
    throw e
  }
}

/**
 * Exchange the stored refresh token for a new pair. Resolves false if the
 * session is over. A network failure does NOT discard the refresh token, so
 * the user can retry once the backend is back.
 */
export function refreshSession(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight
  const before = tokens.refresh
  if (!before) return Promise.resolve(false)

  const run = async (): Promise<boolean> => {
    // A tab sharing this session may have refreshed while we waited: follow it
    // instead of spending (and thereby re-using) the old token.
    if (tokens.refresh !== before || followHandoff()) return tokens.refresh !== null
    const token = tokens.refresh
    if (!token) return false
    try {
      const res = await fetch('/auth/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: token }),
        signal: AbortSignal.timeout(REFRESH_TIMEOUT_MS),    // a stalled request must not leave the app waiting for ever
      })
      if (res.ok) {
        tokens.set((await res.json()) as TokenPair)
        return true
      }
      if (res.status === 401 || res.status === 422) {
        tokens.clear()
        sessionLostHandler?.()
      }
      return false
    } catch {
      return false
    }
  }

  const locks = typeof navigator !== 'undefined' ? navigator.locks : undefined
  refreshInFlight = (locks ? withLock(locks, run) : run()).finally(() => {
    refreshInFlight = null
  })
  return refreshInFlight
}

/** The server no longer accepts this session: leave the user at the sign-in page, not on a broken screen. */
function endSession() {
  tokens.clear()
  sessionLostHandler?.()
}

export async function request<T = unknown>(path: string, opts: RequestOptions = {}): Promise<T> {
  let res = await send(path, opts)

  if (res.status === 401 && opts.auth !== false) {
    if (!tokens.refresh) {
      endSession() // signed out elsewhere (or the stored login is gone)
    } else if (await refreshSession()) {
      res = await send(path, opts)
      if (res.status === 401) endSession() // renewed, yet still refused: revoked or disabled
    }
  }

  if (!res.ok) {
    let detail: unknown
    try {
      detail = (await res.json())?.detail
    } catch {
      /* non-JSON error body */
    }
    const retry = Number(res.headers.get('Retry-After')) || undefined
    throw new ApiError(res.status, describeDetail(detail, res.status), detail, retry)
  }

  if (res.status === 204) return undefined as T
  if (opts.as === 'blob') return (await res.blob()) as T
  return (await res.json()) as T
}
