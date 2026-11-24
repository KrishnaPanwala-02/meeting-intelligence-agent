import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { onSessionLost, tokens } from '../src/api/client'
import { dayKey } from '../src/lib/format'
import { occurrences } from '../src/lib/recurrence'
import { emptyApp, jsonResponse, me, meHandler, mockServer, pair, renderApp } from './helpers'

const zoneMock = vi.hoisted(() => ({ browser: 'Asia/Kolkata' }))
vi.mock('../src/lib/zone', async (orig) => ({ ...(await orig<typeof import('../src/lib/zone')>()), browserZone: () => zoneMock.browser }))

import { hasEnded, meetingWhen, upcomingOnly } from '../src/components/domain'

beforeEach(() => {
  tokens.clear()
  onSessionLost(null)
  tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
  zoneMock.browser = 'Asia/Kolkata'
  Element.prototype.scrollIntoView = vi.fn()
})
afterEach(() => vi.unstubAllGlobals())

const D = (y: number, m: number, d: number, h = 10, mi = 0) => new Date(y, m - 1, d, h, mi)
const days = (list: Date[]) => list.map((d) => dayKey(d))

describe('repeat dates (the same rules as the server)', () => {
  it('weekly, every 2 weeks, daily', () => {
    expect(days(occurrences(D(2026, 11, 13), 'weekly', 3))).toEqual(['2026-11-13', '2026-11-20', '2026-11-27'])
    expect(days(occurrences(D(2026, 11, 13), 'biweekly', 3))).toEqual(['2026-11-13', '2026-11-27', '2026-12-11'])
    expect(days(occurrences(D(2026, 11, 13), 'daily', 3))).toEqual(['2026-11-13', '2026-11-14', '2026-11-15'])
  })
  it('weekdays skip weekends, even when starting on a Saturday', () => {
    expect(occurrences(D(2026, 11, 13), 'weekdays', 4).map((d) => d.getDay())).toEqual([5, 1, 2, 3])
    expect(occurrences(D(2026, 11, 14), 'weekdays', 3).map((d) => d.getDay())).toEqual([6, 1, 2])
  })
  it('monthly keeps the day and clamps to short months, including leap years', () => {
    expect(occurrences(D(2026, 1, 31), 'monthly', 4).map((d) => [d.getMonth() + 1, d.getDate()])).toEqual([[1, 31], [2, 28], [3, 31], [4, 30]])
    expect(days(occurrences(D(2028, 1, 31), 'monthly', 2))[1]).toBe('2028-02-29')
    expect(days(occurrences(D(2026, 11, 13), 'monthly', 3))[2]).toBe('2027-01-13')
  })
  it('keeps the time of day and ends on the chosen day, inclusive', () => {
    expect(new Set(occurrences(D(2026, 3, 1, 8, 15), 'weekly', 30).map((d) => `${d.getHours()}:${d.getMinutes()}`))).toEqual(new Set(['8:15']))
    expect(days(occurrences(D(2026, 11, 13), 'weekly', null, '2026-12-04'))).toEqual(['2026-11-13', '2026-11-20', '2026-11-27', '2026-12-04'])
  })
  it('gives nothing for unusable input', () => {
    expect(occurrences(D(2026, 11, 13), 'weekly')).toEqual([])
    expect(occurrences(D(2026, 11, 13), 'weekly', 1)).toEqual([])
    expect(occurrences(D(2026, 11, 13), 'weekly', 53)).toEqual([])
    expect(occurrences(D(2026, 11, 13), 'weekly', null, '2026-11-01')).toEqual([])
    expect(occurrences(D(2026, 11, 13), 'weekly', null, '2026-11-13')).toEqual([])
    expect(occurrences(D(2026, 11, 13), 'weekly', 52)).toHaveLength(52)
    expect(occurrences(D(2026, 11, 13), 'daily', null, '2035-01-01')).toHaveLength(52)
  })
})

const meetingOf = (over: Record<string, unknown> = {}) => ({
  id: 'm1', title: 'Standup', status: 'scheduled', scheduled_time: '2026-11-13T10:00:00', end_time: '2026-11-13T10:30:00', duration_minutes: 30,
  participants: [], owner_email: 'x@y.com', owner_name: null, is_owner: true, started_at: null, ended_at: null, has_audio: false,
  created_at: '2026-11-01T12:00:00', can_write: true, has_transcript: false, has_summary: false, action_count: 0, open_action_count: 0, timezone: null, ...over,
})

describe('showing a meeting’s time with its zone', () => {
  it('without a zone it is the plain time', () => {
    expect(meetingWhen(meetingOf() as never)).not.toMatch(/IST|GMT|your time/)
  })
  it('in the viewer’s own zone it adds the zone name only', () => {
    const t = meetingWhen(meetingOf({ timezone: 'Asia/Kolkata' }) as never)
    expect(t).toMatch(/IST|GMT\+5:30/)
    expect(t).not.toMatch(/your time/)
  })
  it('in another zone it also says what time that is for the viewer', () => {
    // 10:00 London in November is 15:30 in Kolkata
    const t = meetingWhen(meetingOf({ timezone: 'Europe/London' }) as never)
    expect(t).toMatch(/GMT/)
    expect(t).toMatch(/3:30\s?PM your time/)
  })
  it('zones with the same clock offset need no extra note', () => {
    zoneMock.browser = 'Asia/Kolkata'
    expect(meetingWhen(meetingOf({ timezone: 'Asia/Calcutta' }) as never)).not.toMatch(/your time/)
  })
  it('recordings without a scheduled time keep showing when they were made', () => {
    expect(meetingWhen(meetingOf({ scheduled_time: null, status: 'uploaded', timezone: 'Europe/London' }) as never)).toMatch(/2026/)
  })
})

describe('has a meeting ended, in its own zone', () => {
  // 10:00-10:30 in Kolkata is 04:30-05:00 UTC
  const m = meetingOf({ timezone: 'Asia/Kolkata' }) as never
  it('compares real moments', () => {
    expect(hasEnded(m, new Date('2026-11-13T04:59:00Z'))).toBe(false)
    expect(hasEnded(m, new Date('2026-11-13T05:00:00Z'))).toBe(true)
    // the same wall-clock time in New York (15:00-15:30 UTC) is still ahead at 05:00 UTC
    expect(hasEnded(meetingOf({ timezone: 'America/New_York' }) as never, new Date('2026-11-13T05:00:00Z'))).toBe(false)
  })
  it('the dashboard list keeps meetings that are still ahead in their own zone', () => {
    const ev = (id: string, tz: string | null) => ({ id, start: '2026-11-13T10:00:00', end: '2026-11-13T11:00:00', timezone: tz })
    const now = new Date('2026-11-13T05:45:00Z')      // Kolkata's 10:00-11:00 ended at 05:30 UTC; New York's is hours away
    expect(upcomingOnly([ev('kolkata', 'Asia/Kolkata'), ev('ny', 'America/New_York')], now).map((e) => e.id)).toEqual(['ny'])
  })
  it('a plain meeting is judged on the viewer’s own clock, as before', () => {
    const plain = meetingOf({ scheduled_time: '2026-11-13T10:00:00', end_time: '2026-11-13T11:00:00' }) as never
    expect(hasEnded(plain, D(2026, 11, 13, 10, 59))).toBe(false)
    expect(hasEnded(plain, D(2026, 11, 13, 11, 0))).toBe(true)
  })
})

describe('scheduling a repeating meeting', () => {
  const server = (reply: () => unknown) =>
    mockServer({
      ...emptyApp,
      'GET /auth/me': () => ({ ...me, timezone: 'Europe/London' }),
      'POST /api/v1/meetings/schedule': reply,
      '/api/v1/meetings/m1': () => jsonResponse({ detail: 'stop here' }, 404),
    })

  async function fill(user: ReturnType<typeof userEvent.setup>, title = 'Standup') {
    await user.type(await screen.findByLabelText('Title'), title)
  }

  it('starts in the user’s own zone and shows repeat options only when asked', async () => {
    server(() => ({}))
    renderApp('/meetings/new?mode=schedule')
    expect(((await screen.findByLabelText('Time zone')) as HTMLSelectElement).value).toBe('Europe/London')
    expect(screen.queryByLabelText('Number of meetings')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Schedule meeting' })).toBeEnabled()
  })

  it('previews the dates and sends the repeat rule with the zone', async () => {
    const user = userEvent.setup()
    const { calls } = server(() => ({ ...meetingOf({ id: 'm1' }), series: { id: 's', recurrence: 'weekly', created: 4, skipped: [] } }))
    renderApp('/meetings/new?mode=schedule')
    await fill(user)
    await user.selectOptions(screen.getByLabelText('Repeat'), 'weekly')
    expect(await screen.findByText(/^4 meetings, from/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Schedule 4 meetings' }))
    await waitFor(() => expect(calls.some((c) => c.url === '/api/v1/meetings/schedule')).toBe(true))
    expect(calls.find((c) => c.url === '/api/v1/meetings/schedule')!.body).toMatchObject({ title: 'Standup', timezone: 'Europe/London', repeat: 'weekly', repeat_count: 4 })
    expect(await screen.findByText('4 meetings scheduled.')).toBeInTheDocument()
  })

  it('can repeat until a date instead, and refuses an unusable end', async () => {
    const user = userEvent.setup()
    const { calls } = server(() => ({ ...meetingOf({ id: 'm1' }), series: { id: 's', recurrence: 'daily', created: 3, skipped: [] } }))
    renderApp('/meetings/new?mode=schedule')
    await fill(user)
    await user.selectOptions(screen.getByLabelText('Repeat'), 'daily')
    await user.selectOptions(screen.getByLabelText('How the repeat ends'), 'until')
    const submit = () => screen.getByRole('button', { name: /^Schedule/ })
    expect(submit()).toBeDisabled()
    expect(screen.getByText(/Choose the last day/)).toBeInTheDocument()
    const start = ((screen.getByLabelText('Starts') as HTMLInputElement).value || '').slice(0, 10)
    const third = new Date(start + 'T00:00:00')
    third.setDate(third.getDate() + 2)
    await user.type(screen.getByLabelText('Last day'), dayKey(third))
    await waitFor(() => expect(submit()).toBeEnabled())
    expect(screen.getByText(/^3 meetings, from/)).toBeInTheDocument()
    await user.click(submit())
    await waitFor(() => expect(calls.some((c) => c.url === '/api/v1/meetings/schedule')).toBe(true))
    const body = calls.find((c) => c.url === '/api/v1/meetings/schedule')!.body
    expect(body.repeat_until).toBe(dayKey(third))
    expect(body.repeat_count).toBeUndefined()
  })

  it('a count outside 2-52 blocks the button and explains', async () => {
    const user = userEvent.setup()
    server(() => ({}))
    renderApp('/meetings/new?mode=schedule')
    await fill(user)
    await user.selectOptions(screen.getByLabelText('Repeat'), 'weekly')
    const n = screen.getByLabelText('Number of meetings')
    await user.clear(n)
    await user.type(n, '1')
    expect(screen.getByText('Choose between 2 and 52 meetings.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Schedule meeting' })).toBeDisabled()
    await user.clear(n)
    await user.type(n, '53')
    expect(screen.getByRole('button', { name: 'Schedule meeting' })).toBeDisabled()
  })

  it('lists the dates that were skipped because they clashed, then opens the first meeting', async () => {
    const user = userEvent.setup()
    server(() => ({ ...meetingOf({ id: 'm1' }), series: { id: 's', recurrence: 'weekly', created: 3, skipped: [{ scheduled_time: '2026-11-27T10:00:00', reason: 'Conflicts with “Planning”' }] } }))
    renderApp('/meetings/new?mode=schedule')
    await fill(user)
    await user.selectOptions(screen.getByLabelText('Repeat'), 'weekly')
    await user.click(await screen.findByRole('button', { name: 'Schedule 4 meetings' }))
    const dialog = await screen.findByRole('dialog', { name: 'Some dates were skipped' })
    expect(within(dialog).getByText(/3 meetings were scheduled\. 1 date clashed/)).toBeInTheDocument()
    expect(within(dialog).getByText(/Conflicts with “Planning”/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Open the first meeting' }))
    await waitFor(() => expect(screen.queryByRole('heading', { level: 1, name: 'New meeting' })).not.toBeInTheDocument())   // left the form for /meetings/m1
    expect(screen.queryByRole('dialog', { name: 'Some dates were skipped' })).not.toBeInTheDocument()
  })

  it('a clash on the first meeting still shows the usual warning', async () => {
    const user = userEvent.setup()
    server(() => jsonResponse({ detail: { message: 'conflict', conflicts: [{ id: 'x', title: 'Planning', scheduled_time: '2026-11-20T10:00:00' }] } }, 409))
    renderApp('/meetings/new?mode=schedule')
    await fill(user)
    await user.click(screen.getByRole('button', { name: 'Schedule meeting' }))
    expect(await screen.findByText(/This time clashes with another meeting/)).toBeInTheDocument()
  })

  it('a single meeting is scheduled as before, with the zone', async () => {
    const user = userEvent.setup()
    const { calls } = server(() => meetingOf({ id: 'm1' }))
    renderApp('/meetings/new?mode=schedule')
    await fill(user)
    await user.click(screen.getByRole('button', { name: 'Schedule meeting' }))
    await waitFor(() => expect(calls.some((c) => c.url === '/api/v1/meetings/schedule')).toBe(true))
    const body = calls.find((c) => c.url === '/api/v1/meetings/schedule')!.body
    expect(body.timezone).toBe('Europe/London')
    expect('repeat' in body).toBe(false)
    expect(await screen.findByText('Meeting scheduled.')).toBeInTheDocument()
  })
})

describe('a meeting in a series', () => {
  const inSeries = (over: Record<string, unknown> = {}) =>
    meetingOf({ series_id: 's1', recurrence: 'weekly', series: { count: 4, index: 2 }, timezone: 'Asia/Kolkata', ...over })

  function open(over: Record<string, unknown> = {}, routes: Record<string, unknown> = {}) {
    return mockServer({
      ...emptyApp,
      'GET /auth/me': meHandler,
      '/api/v1/meetings/m1': () => inSeries(over),
      '/api/v1/meetings/m1/jobs/latest': () => jsonResponse({ detail: 'none' }, 404),
      '/api/v1/meetings/m1/actions': () => ({ meeting_id: 'm1', action_items: [], count: 0 }),
      '/api/v1/meetings/m1/series': () => ({
        series_id: 's1', recurrence: 'weekly', label: 'every week',
        meetings: [
          { id: 'm0', scheduled_time: '2026-11-06T10:00:00', status: 'completed', is_current: false },
          { id: 'm1', scheduled_time: '2026-11-13T10:00:00', status: 'scheduled', is_current: true },
          { id: 'm2', scheduled_time: '2026-11-20T10:00:00', status: 'cancelled', is_current: false },
        ],
      }),
      ...routes,
    } as never)
  }

  it('says it repeats, and which one this is', async () => {
    open()
    renderApp('/meetings/m1')
    expect(await screen.findByText('Repeats every week · 2 of 4')).toBeInTheDocument()
  })

  it('lists every date, linking to the others', async () => {
    const user = userEvent.setup()
    open()
    renderApp('/meetings/m1')
    await user.click(await screen.findByRole('button', { name: 'See all dates' }))
    const list = await screen.findByRole('list', { name: 'Meetings in this series' })
    const items = within(list).getAllByRole('listitem')
    expect(items).toHaveLength(3)
    expect(within(items[1]).getByText(/\(this one\)/)).toBeInTheDocument()
    expect(within(items[0]).getByRole('link')).toHaveAttribute('href', '/meetings/m0')
    expect(within(items[2]).getByText('cancelled')).toBeInTheDocument()
    expect(within(items[1]).queryByRole('link')).not.toBeInTheDocument()
  })

  it('cancelling asks whether it is this one or all the following', async () => {
    const user = userEvent.setup()
    const { calls } = open({}, { 'POST /api/v1/meetings/m1/cancel': () => ({ ...inSeries(), status: 'cancelled', cancelled_count: 3 }) })
    renderApp('/meetings/m1')
    await user.click(await screen.findByRole('button', { name: 'Cancel meeting' }))
    const dialog = await screen.findByRole('dialog', { name: 'Cancel this meeting?' })
    expect(within(dialog).getByRole('radio', { name: 'Only this meeting' })).toBeChecked()
    await user.click(within(dialog).getByRole('radio', { name: 'This and all following meetings' }))
    await user.click(within(dialog).getByRole('button', { name: 'Cancel these meetings' }))
    await waitFor(() => expect(calls.some((c) => c.url.startsWith('/api/v1/meetings/m1/cancel'))).toBe(true))
    expect(calls.find((c) => c.url.startsWith('/api/v1/meetings/m1/cancel'))!.url).toBe('/api/v1/meetings/m1/cancel?scope=following')
    expect(await screen.findByText('3 meetings cancelled.')).toBeInTheDocument()
  })

  it('cancelling just this one sends no scope', async () => {
    const user = userEvent.setup()
    const { calls } = open({}, { 'POST /api/v1/meetings/m1/cancel': () => ({ ...inSeries(), status: 'cancelled' }) })
    renderApp('/meetings/m1')
    await user.click(await screen.findByRole('button', { name: 'Cancel meeting' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Cancel meeting' }))
    await waitFor(() => expect(calls.some((c) => c.url.startsWith('/api/v1/meetings/m1/cancel'))).toBe(true))
    expect(calls.find((c) => c.url.startsWith('/api/v1/meetings/m1/cancel'))!.url).toBe('/api/v1/meetings/m1/cancel')
    expect(await screen.findByText('Meeting cancelled.')).toBeInTheDocument()
  })

  it('a meeting that does not repeat has no repeat controls', async () => {
    open({ series_id: null, recurrence: null, series: null })
    renderApp('/meetings/m1')
    await screen.findByRole('heading', { level: 1, name: 'Standup' })
    expect(screen.queryByText(/Repeats/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'See all dates' })).not.toBeInTheDocument()
  })

  it('rescheduling can change the zone, and sends it only if changed', async () => {
    const user = userEvent.setup()
    const { calls } = open({}, { 'PUT /api/v1/meetings/m1': () => inSeries() })
    renderApp('/meetings/m1')
    await user.click(await screen.findByRole('button', { name: 'Reschedule' }))
    const dialog = await screen.findByRole('dialog', { name: 'Reschedule meeting' })
    expect((within(dialog).getByLabelText('Time zone') as HTMLSelectElement).value).toBe('Asia/Kolkata')
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    expect(calls.find((c) => c.method === 'PUT')!.body.timezone).toBeUndefined()
  })

  it('sends the new zone when it is changed', async () => {
    const user = userEvent.setup()
    const { calls } = open({}, { 'PUT /api/v1/meetings/m1': () => inSeries() })
    renderApp('/meetings/m1')
    await user.click(await screen.findByRole('button', { name: 'Reschedule' }))
    const dialog = await screen.findByRole('dialog', { name: 'Reschedule meeting' })
    await user.selectOptions(within(dialog).getByLabelText('Time zone'), 'UTC')
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    expect(calls.find((c) => c.method === 'PUT')!.body.timezone).toBe('UTC')
  })
})
