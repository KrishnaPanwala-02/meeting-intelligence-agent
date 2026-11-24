import { render } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { vi } from 'vitest'
import App from '../src/App'
import { AuthProvider } from '../src/auth/AuthContext'
import { ToastProvider } from '../src/components/ui'

export type Handler = (req: { url: string; method: string; body: any; headers: Record<string, string> }) => unknown | Response

/** Route-table fetch mock. A handler returns data (200), or a Response for other statuses. */
export function mockServer(routes: Record<string, Handler>) {
  const calls: { url: string; method: string; body: any }[] = []
  const fn = vi.fn(async (input: string, init: RequestInit = {}) => {
    const url = String(input)
    const method = init.method ?? 'GET'
    const path = url.split('?')[0]
    let body: any
    try {
      body = typeof init.body === 'string' ? JSON.parse(init.body) : init.body
    } catch {
      body = init.body
    }
    calls.push({ url, method, body })
    const key = `${method} ${path}`
    const h = routes[key] ?? routes[path]
    if (!h) return new Response(JSON.stringify({ detail: `no mock for ${key}` }), { status: 599 })
    const out = h({ url, method, body, headers: (init.headers ?? {}) as Record<string, string> })
    if (out instanceof Response) return out
    return new Response(out === undefined ? null : JSON.stringify(out), { status: out === undefined ? 204 : 200 })
  })
  vi.stubGlobal('fetch', fn)
  return { fn, calls }
}

export const jsonResponse = (body: unknown, status: number) => new Response(JSON.stringify(body), { status })

export const pair = (n = 1) => ({
  access_token: `access-${n}`, refresh_token: `refresh-${n}`, token_type: 'bearer',
  expires_in: 900, refresh_expires_in: 1_209_600,
})

export const me = { id: 'u1', email: 'anna@example.com', name: 'Anna Larsen' as string | null, is_admin: false, session_id: 's1' }

/** /auth/me as the real server behaves: 401 without a bearer token. */
export const meHandler: Handler = ({ headers }) =>
  headers.Authorization ? me : jsonResponse({ detail: 'Not authenticated' }, 401)

/** Endpoints the dashboard and layout hit, returning empty data. */
export const emptyApp: Record<string, Handler> = {
  '/health/ready': () => ({ status: 'ready', checks: { database: 'ok', redis: 'ok', ollama: 'ok' } }),
  '/api/v1/meetings': () => ({ count: 0, total: 0, meetings: [] }),
  '/api/v1/actions': () => ({ count: 0, total: 0, action_items: [] }),
  '/api/v1/notifications': () => ({ unread: 0, items: [] }),
  '/api/v1/calendar/anna%40example.com/events': () => ({ user_email: me.email, calendar_id: 'internal', count: 0, total: 0, events: [] }),
}

export function renderApp(path = '/') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <ToastProvider>
        <AuthProvider>
          <App />
        </AuthProvider>
      </ToastProvider>
    </MemoryRouter>,
  )
}
