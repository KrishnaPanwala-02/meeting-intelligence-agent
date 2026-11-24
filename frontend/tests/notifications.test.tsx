import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { onSessionLost, tokens } from '../src/api/client'
import { ago } from '../src/lib/format'
import { allZones, browserZone, instantOf, offsetMinutes, zoneAbbr } from '../src/lib/zone'
import { emptyApp, jsonResponse, me, meHandler, mockServer, pair, renderApp } from './helpers'

beforeEach(() => {
  tokens.clear()
  onSessionLost(null)
  tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
  Element.prototype.scrollIntoView = vi.fn()
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('ago', () => {
  const now = new Date('2026-11-13T12:00:00Z')
  const at = (ms: number) => new Date(now.getTime() - ms).toISOString().replace('Z', '')
  it('reads naturally from seconds to days', () => {
    expect(ago(at(10_000), now)).toBe('just now')
    expect(ago(at(60_000), now)).toBe('1 min ago')
    expect(ago(at(5 * 60_000), now)).toBe('5 min ago')
    expect(ago(at(3 * 3600_000), now)).toBe('3 h ago')
    expect(ago(at(26 * 3600_000), now)).toBe('1 day ago')
    expect(ago(at(3 * 86400_000), now)).toBe('3 days ago')
    expect(ago(at(30 * 86400_000), now)).toMatch(/Oct|14/)
    expect(ago(new Date(now.getTime() + 5000).toISOString().replace('Z', ''), now)).toBe('just now') // clock skew
  })
})

describe('time zone helpers', () => {
  it('turns a wall-clock time in a zone into the real moment', () => {
    expect(instantOf('2026-11-13T10:00:00', 'Asia/Kolkata').toISOString()).toBe('2026-11-13T04:30:00.000Z')
    expect(instantOf('2026-01-15T09:00:00', 'America/New_York').toISOString()).toBe('2026-01-15T14:00:00.000Z') // winter
    expect(instantOf('2026-07-15T09:00:00', 'America/New_York').toISOString()).toBe('2026-07-15T13:00:00.000Z') // summer
    expect(instantOf('2026-11-13T10:00:00', 'UTC').toISOString()).toBe('2026-11-13T10:00:00.000Z')
    expect(instantOf('2026-11-13T10:00:00Z', 'Asia/Kolkata').toISOString()).toBe('2026-11-13T04:30:00.000Z') // a stray offset is ignored
    expect(instantOf('2026-11-13T10:00', 'Pacific/Auckland').toISOString()).toBe('2026-11-12T21:00:00.000Z')
  })
  it('without a zone it is the viewer’s own clock', () => {
    const d = instantOf('2026-11-13T10:00:00', null)
    expect([d.getFullYear(), d.getMonth(), d.getDate(), d.getHours(), d.getMinutes()]).toEqual([2026, 10, 13, 10, 0])
  })
  it('finds the offset and a short name', () => {
    expect(offsetMinutes(new Date('2026-11-13T00:00:00Z'), 'Asia/Kolkata')).toBe(330)
    expect(offsetMinutes(new Date('2026-07-01T00:00:00Z'), 'Europe/London')).toBe(60)
    expect(offsetMinutes(new Date('2026-11-13T00:00:00Z'), 'Not/AZone')).toBe(0)
    expect(zoneAbbr('Asia/Kolkata', new Date('2026-11-13T00:00:00Z'))).toMatch(/IST|GMT\+5:30/)
    expect(zoneAbbr('Not/AZone')).toBe('Not/AZone')
  })
  it('lists zones with UTC first, including any extra one, without repeats', () => {
    const z = allZones('Asia/Kolkata', 'Made/Up', null)
    expect(z[0]).toBe('UTC')
    expect(z).toContain('Asia/Kolkata')
    expect(z).toContain('Made/Up')
    expect(new Set(z).size).toBe(z.length)
    expect(typeof browserZone()).toBe('string')
  })
})

const note = (n: number, over: Record<string, unknown> = {}) => ({
  id: `n${n}`, kind: 'task_assigned', title: `New task ${n}`, body: `Krishna assigned you task ${n}.`, link: `/tasks?review=${n}`,
  meeting_id: 'm1', created_at: new Date(Date.now() - n * 60_000).toISOString().replace('Z', ''), read: false, ...over,
})

function inbox(items: ReturnType<typeof note>[], extra: Record<string, unknown> = {}) {
  let current = items
  const server = mockServer({
    ...emptyApp,
    'GET /auth/me': meHandler,
    'GET /api/v1/notifications': () => ({ unread: current.filter((n) => !n.read).length, items: current }),
    'POST /api/v1/notifications/read': ({ body }) => {
      current = current.map((n) => (!body.ids || body.ids.includes(n.id) ? { ...n, read: true } : n))
      return { marked: 1, unread: current.filter((n) => !n.read).length }
    },
    'DELETE /api/v1/notifications/n1': () => {
      current = current.filter((n) => n.id !== 'n1')
      return undefined
    },
    ...extra,
  })
  return server
}

describe('the notification bell', () => {
  it('shows how many are unread, in its label and badge', async () => {
    inbox([note(1), note(2), note(3, { read: true })])
    renderApp('/')
    const bell = await screen.findByRole('button', { name: 'Notifications, 2 unread' })
    expect(within(bell).getByText('2')).toBeInTheDocument()
  })

  it('has no badge when everything is read, and says 9+ for many', async () => {
    inbox([note(1, { read: true })])
    renderApp('/')
    expect(await screen.findByRole('button', { name: 'Notifications' })).toBeInTheDocument()
    expect(document.querySelector('.bell-count')).toBeNull()
  })

  it('caps the badge at 9+', async () => {
    mockServer({ ...emptyApp, 'GET /auth/me': meHandler, '/api/v1/notifications': () => ({ unread: 14, items: [note(1)] }) })
    renderApp('/')
    expect(within(await screen.findByRole('button', { name: 'Notifications, 14 unread' })).getByText('9+')).toBeInTheDocument()
  })

  it('opens a list with what happened and when, oldest dimmed once read', async () => {
    const user = userEvent.setup()
    inbox([note(1), note(2, { read: true })])
    renderApp('/')
    await user.click(await screen.findByRole('button', { name: /^Notifications/ }))
    const panel = screen.getByRole('region', { name: 'Notifications' })
    expect(within(panel).getByText('New task 1')).toBeInTheDocument()
    expect(within(panel).getByText('Krishna assigned you task 2.')).toBeInTheDocument()
    expect(within(panel).getByText('1 min ago')).toBeInTheDocument()
    const items = panel.querySelectorAll('li')
    expect(items[0].className).toBe('unread')
    expect(items[1].className).toBe('read')
  })

  it('opening one goes to it and marks it read', async () => {
    const user = userEvent.setup()
    const { calls } = inbox([note(1), note(2)], { '/api/v1/actions': () => ({ count: 0, total: 0, action_items: [] }) })
    renderApp('/')
    await user.click(await screen.findByRole('button', { name: 'Notifications, 2 unread' }))
    await user.click(screen.getByRole('button', { name: /New task 1\s+Krishna assigned/ }))
    expect(await screen.findByRole('heading', { level: 1, name: 'My tasks' })).toBeInTheDocument() // followed the link
    expect(screen.queryByRole('region', { name: 'Notifications' })).not.toBeInTheDocument()
    await waitFor(() => expect(calls.some((c) => c.url === '/api/v1/notifications/read' && c.body.ids?.[0] === 'n1')).toBe(true))
    expect(await screen.findByRole('button', { name: 'Notifications, 1 unread' })).toBeInTheDocument()
  })

  it('"Mark all read" clears the badge', async () => {
    const user = userEvent.setup()
    const { calls } = inbox([note(1), note(2)])
    renderApp('/')
    await user.click(await screen.findByRole('button', { name: 'Notifications, 2 unread' }))
    await user.click(screen.getByRole('button', { name: 'Mark all read' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Mark all read' })).toBeDisabled())
    expect(calls.find((c) => c.url === '/api/v1/notifications/read')!.body).toEqual({})
    expect(screen.getByRole('button', { name: 'Notifications' })).toBeInTheDocument()
  })

  it('dismissing removes one from the list', async () => {
    const user = userEvent.setup()
    const { calls } = inbox([note(1), note(2)])
    renderApp('/')
    await user.click(await screen.findByRole('button', { name: /^Notifications/ }))
    await user.click(screen.getByRole('button', { name: 'Dismiss: New task 1' }))
    await waitFor(() => expect(screen.queryByText('New task 1')).not.toBeInTheDocument())
    expect(screen.getByText('New task 2')).toBeInTheDocument()
    expect(calls.some((c) => c.method === 'DELETE' && c.url === '/api/v1/notifications/n1')).toBe(true)
    expect(screen.getByRole('button', { name: 'Notifications, 1 unread' })).toBeInTheDocument()
  })

  it('says so when there is nothing, and closes on Escape or a click elsewhere', async () => {
    const user = userEvent.setup()
    inbox([])
    renderApp('/')
    await user.click(await screen.findByRole('button', { name: 'Notifications' }))
    expect(screen.getByText('You are all caught up.')).toBeInTheDocument()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('region', { name: 'Notifications' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Notifications' }))
    await user.click(document.body)
    expect(screen.queryByRole('region', { name: 'Notifications' })).not.toBeInTheDocument()
  })

  it('explains a failure to load instead of showing a broken list', async () => {
    const user = userEvent.setup()
    inbox([], { 'GET /api/v1/notifications': () => jsonResponse({ detail: 'The server had a problem. Please try again.' }, 500) })
    renderApp('/')
    await user.click(await screen.findByRole('button', { name: 'Notifications' }))
    expect(await screen.findByText(/Could not load notifications/)).toBeInTheDocument()
  })

  it('checks for new ones every minute', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    const { calls } = inbox([])
    renderApp('/')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10)
    })
    const count = () => calls.filter((c) => c.url.startsWith('/api/v1/notifications') && c.method === 'GET').length
    const before = count()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_500)
    })
    expect(count()).toBeGreaterThan(before)
  })
})

describe('notification and time zone settings', () => {
  function settings(over: Record<string, unknown> = {}, user: Record<string, unknown> = {}) {
    return mockServer({
      ...emptyApp,
      'GET /auth/me': () => ({ ...me, ...user }),
      '/auth/sessions': () => ({ sessions: [] }),
      '/api/v1/voice-profiles': () => ({ count: 0, profiles: [] }),
      'GET /api/v1/features': () => ({ email_enabled: true }),
      'PATCH /auth/me': ({ body }) => ({ ...me, ...user, ...body }),
      ...over,
    })
  }

  it('saves the chosen time zone', async () => {
    const user = userEvent.setup()
    const { calls } = settings()
    renderApp('/settings')
    const select = await screen.findByLabelText('Your time zone')
    await user.selectOptions(select, 'UTC') // present in every browser's list
    await waitFor(() => expect(calls.find((c) => c.method === 'PATCH')!.body).toEqual({ timezone: 'UTC' }))
    expect(await screen.findByText('Time zone saved.')).toBeInTheDocument()
    expect((screen.getByLabelText('Your time zone') as HTMLSelectElement).value).toBe('UTC')
  })

  it('clears it, and offers the browser’s own zone', async () => {
    const user = userEvent.setup()
    const { calls } = settings({}, { timezone: 'Europe/London' })
    renderApp('/settings')
    await user.selectOptions(await screen.findByLabelText('Your time zone'), '')
    await waitFor(() => expect(calls.find((c) => c.method === 'PATCH')!.body).toEqual({ timezone: '' }))
    expect(await screen.findByText('Time zone cleared.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /Use this browser’s zone/ }))
    await waitFor(() => expect(calls.filter((c) => c.method === 'PATCH')[1].body).toEqual({ timezone: browserZone() }))
  })

  it('lets you turn e-mail copies on and off when the server can send mail', async () => {
    const user = userEvent.setup()
    const { calls } = settings()
    renderApp('/settings')
    const box = await screen.findByRole('checkbox', { name: 'E-mail me a copy of notifications' })
    await waitFor(() => expect(box).toBeEnabled())
    await user.click(box)
    await waitFor(() => expect(calls.find((c) => c.method === 'PATCH')!.body).toEqual({ email_notifications: true }))
    expect(await screen.findByText(/You will get e-mail copies/)).toBeInTheDocument()
    await user.click(box)
    expect(await screen.findByText('E-mail copies are off.')).toBeInTheDocument()
  })

  it('explains when the server cannot send mail, and disables the choice', async () => {
    settings({ 'GET /api/v1/features': () => ({ email_enabled: false }) })
    renderApp('/settings')
    expect(await screen.findByText(/not set up to send e-mail, so notifications appear in the bell only/)).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'E-mail me a copy of notifications' })).toBeDisabled()
  })

  it('shows why saving failed', async () => {
    const user = userEvent.setup()
    settings({ 'PATCH /auth/me': () => jsonResponse({ detail: 'Unknown time zone. Use a name like Asia/Kolkata or Europe/London.' }, 422) })
    renderApp('/settings')
    await user.selectOptions(await screen.findByLabelText('Your time zone'), 'UTC')
    expect(await screen.findByText(/Unknown time zone/)).toBeInTheDocument()
  })
})
