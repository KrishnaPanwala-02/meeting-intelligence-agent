import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { onSessionLost, tokens } from '../src/api/client'
import { columnPath, niceMax } from '../src/pages/AnalyticsPage'
import { emptyApp, jsonResponse, meHandler, mockServer, pair, renderApp } from './helpers'

beforeEach(() => {
  tokens.clear()
  onSessionLost(null)
  tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
})
afterEach(() => {
  vi.unstubAllGlobals()
})

describe('chart helpers', () => {
  it('niceMax gives round, even axis ends at or above the data', () => {
    expect([0, 1, 1.5, 2, 3, 5, 6, 11, 47, 101, 999].map(niceMax)).toEqual([2, 2, 2, 2, 4, 6, 6, 12, 60, 120, 1000])
    expect(niceMax(250_000)).toBe(300_000)
    for (const v of [1, 3, 7, 12, 83, 450, 12345]) {
      expect(niceMax(v)).toBeGreaterThanOrEqual(v)
      expect(niceMax(v) % 2).toBe(0)
    }
  })
  it('a column is square at the baseline and rounded at the top, even when very short', () => {
    expect(columnPath(10, 20, 24, 50)).toBe('M10,70V24Q10,20 14,20H30Q34,20 34,24V70Z')
    expect(columnPath(0, 9, 24, 1)).toBe('M0,10V10Q0,9 1,9H23Q24,9 24,10V10Z') // radius shrinks to the height
  })
})

const wl = (iso: string) => new Date(`${iso}T00:00:00`).toLocaleDateString([], { day: 'numeric', month: 'short' }) // the page's own, locale-aware format
const week = (start: string, over: Record<string, number> = {}) => ({ week_start: start, meetings: 0, tasks_created: 0, tasks_completed: 0, talk_minutes: 0, ...over })
const analytics = (over: Record<string, unknown> = {}) => ({
  days: 90,
  overview: {
    meetings: 4, meetings_with_transcript: 3, recorded_seconds: 3725, tasks: 8, tasks_done: 3, tasks_open: 5, tasks_overdue: 2,
    tasks_unassigned_open: 1, completion_rate: 0.375, unnamed_talk_seconds: 12,
  },
  open_by_priority: { high: 1, medium: 3, low: 1 },
  people: [
    { name: 'Rahul Patel', talk_seconds: 300, talk_share: 0.6, words: 410, meetings: 3, tasks_total: 5, tasks_done: 3, tasks_open: 2, tasks_overdue: 2, completion_rate: 0.6 },
    { name: 'Krishna Panwala', talk_seconds: 150, talk_share: 0.3, words: 200, meetings: 2, tasks_total: 2, tasks_done: 0, tasks_open: 2, tasks_overdue: 0, completion_rate: 0 },
    { name: 'Anna Larsen', talk_seconds: 0, talk_share: 0, words: 0, meetings: 0, tasks_total: 0, tasks_done: 0, tasks_open: 0, tasks_overdue: 0, completion_rate: null },
  ],
  weeks: [week('2026-10-26', { meetings: 1, tasks_created: 4 }), week('2026-11-02', { meetings: 0 }), week('2026-11-09', { meetings: 3, tasks_created: 4, tasks_completed: 3 })],
  ...over,
})

function setup(reply: (days: number) => unknown) {
  return mockServer({
    ...emptyApp,
    'GET /auth/me': meHandler,
    'GET /api/v1/analytics': ({ url }) => reply(Number(new URL(url, 'http://x').searchParams.get('days'))),
  })
}

describe('the Analytics page', () => {
  it('is in the menu and shows the headline numbers', async () => {
    setup(() => analytics())
    renderApp('/analytics')
    expect(await screen.findByRole('heading', { level: 1, name: 'Analytics' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Analytics' })).toHaveAttribute('href', '/analytics')
    await screen.findByText('Recorded time') // the numbers have loaded
    const tile = (label: string) => screen.getAllByText(label).find((e) => e.classList.contains('label'))!.parentElement as HTMLElement
    expect(await within(tile('Meetings')).findByText('4')).toBeInTheDocument()
    expect(within(tile('Recorded time')).getByText('1 h 2 min')).toBeInTheDocument()
    expect(within(tile('Recorded time')).getByText('3 with a transcript')).toBeInTheDocument()
    expect(within(tile('Tasks done')).getByText('38%')).toBeInTheDocument() // 0.375 rounds to 38%
    expect(within(tile('Tasks done')).getByText('3 of 8 tasks')).toBeInTheDocument()
    expect(within(tile('Overdue')).getByText('2')).toBeInTheDocument()
  })

  it('shows talk time per person, biggest first, only for people who talked', async () => {
    setup(() => analytics())
    renderApp('/analytics')
    const list = await screen.findByRole('list', { name: 'Talk time per person' })
    const rows = within(list).getAllByRole('listitem')
    expect(rows.map((r) => r.querySelector('.name')!.textContent)).toEqual(['Rahul Patel', 'Krishna Panwala']) // Anna talked 0s
    expect(within(list).getByText('5 min · 60%')).toBeInTheDocument()
    expect(within(list).getByText('2 min 30 s · 30%')).toBeInTheDocument()
    expect(rows[0].getAttribute('title')).toContain('410 words in 3 meetings')
    const widths = rows.map((r) => parseFloat((r.querySelector('.hbar') as HTMLElement).style.width))
    expect(widths[0]).toBeGreaterThan(widths[1])
    expect(screen.getByText(/Speakers not named yet: 12 seconds/)).toBeInTheDocument()
  })

  it('shows completion per person, with overdue as text and an icon, and who has no tasks left out', async () => {
    setup(() => analytics())
    renderApp('/analytics')
    await screen.findByText('Tasks by person')
    expect(screen.getByRole('img', { name: 'Rahul Patel: 3 of 5 tasks done' })).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Krishna Panwala: 0 of 2 tasks done' })).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: /Anna Larsen/ })).not.toBeInTheDocument()
    expect(screen.getByText('3 of 5 done · 60%')).toBeInTheDocument()
    expect(screen.getByText('2 overdue')).toBeInTheDocument()
    expect(screen.getByText('1 open task not assigned to anyone yet.')).toBeInTheDocument()
  })

  it('the weekly charts: legend only for two series, accessible columns, tooltip on hover and focus', async () => {
    const user = userEvent.setup()
    setup(() => analytics())
    renderApp('/analytics')
    const created = (await screen.findByRole('img', { name: /Tasks created and completed per week, 3 weeks/ })).closest('.cols') as HTMLElement
    expect(within(created).getByText('Created')).toBeInTheDocument()
    expect(within(created).getByText('Completed')).toBeInTheDocument()
    const meetings = screen.getByRole('img', { name: /Meetings per week, 3 weeks/ }).closest('.cols') as HTMLElement
    expect(within(meetings).queryByText('Meetings')).not.toBeInTheDocument() // one series: the title says it

    const last = within(created).getByRole('img', { name: `Week of ${wl('2026-11-09')}: 4 created, 3 completed` })
    expect(created.querySelector('.tip')).toBeNull()
    await user.hover(last)
    const tip = within(created).getByRole('status')
    expect(tip).toHaveTextContent(`Week of ${wl('2026-11-09')}`)
    expect(tip).toHaveTextContent('Created: 4')
    expect(tip).toHaveTextContent('Completed: 3')
    await user.unhover(last)
    expect(created.querySelector('.tip')).toBeNull()
    fireEvent.focus(within(created).getByRole('img', { name: `Week of ${wl('2026-10-26')}: 4 created, 0 completed` }))
    expect(within(created).getByRole('status')).toHaveTextContent(`Week of ${wl('2026-10-26')}`)
  })

  it('marks are thin with rounded tops, and an empty week draws nothing', async () => {
    setup(() => analytics())
    renderApp('/analytics')
    const svg = (await screen.findByRole('img', { name: /Meetings per week/ })) as unknown as SVGElement
    const bars = Array.from(svg.querySelectorAll('path'))
    expect(bars).toHaveLength(2) // weeks with 1 and 3 meetings; the empty week has no mark
    for (const b of bars) {
      const nums = (b.getAttribute('d') ?? '').match(/-?\d+(\.\d+)?/g)!.map(Number)
      expect(b.getAttribute('d')).toMatch(/Q/) // rounded data end
      expect(Math.max(...nums)).toBeLessThan(700)
    }
  })

  it('offers the same numbers as tables', async () => {
    const user = userEvent.setup()
    setup(() => analytics())
    renderApp('/analytics')
    await user.click(await screen.findByText('Show the numbers as tables'))
    const people = screen.getByRole('table', { name: 'Per person' })
    expect(within(people).getByRole('row', { name: /Rahul Patel 5 min 60% 3 5 3 2 2/ })).toBeInTheDocument()
    const weeks = screen.getByRole('table', { name: 'Per week' })
    expect(within(weeks).getAllByRole('row')).toHaveLength(4) // header + 3 weeks
  })

  it('changing the period asks again with that many days', async () => {
    const user = userEvent.setup()
    const { calls } = setup((d) => analytics({ days: d }))
    renderApp('/analytics')
    await screen.findByText('Tasks by person')
    expect(screen.getByRole('button', { name: '90 days' })).toHaveAttribute('aria-pressed', 'true')
    await user.click(screen.getByRole('button', { name: '30 days' }))
    await waitFor(() => expect(calls.some((c) => c.url === '/api/v1/analytics?days=30')).toBe(true))
    expect(screen.getByRole('button', { name: '30 days' })).toHaveAttribute('aria-pressed', 'true')
    expect(calls.find((c) => c.url.startsWith('/api/v1/analytics'))!.url).toBe('/api/v1/analytics?days=90')
  })

  it('has friendly messages when there is nothing to show', async () => {
    setup(() => analytics({ overview: { ...analytics().overview, meetings: 0 }, people: [] }))
    renderApp('/analytics')
    expect(await screen.findByText('Nothing to show yet')).toBeInTheDocument()
    expect(screen.getByText(/no meetings in the last 90 days/)).toBeInTheDocument()
  })

  it('explains when no speaker has been named, and when there are no tasks', async () => {
    const o = analytics().overview
    setup(() => analytics({ people: [], overview: { ...o, tasks: 0, tasks_done: 0, completion_rate: null, tasks_overdue: 0, tasks_unassigned_open: 0, unnamed_talk_seconds: 0 } }))
    renderApp('/analytics')
    expect(await screen.findByText(/No named speakers yet/)).toBeInTheDocument()
    expect(screen.getByText('No tasks in this period.')).toBeInTheDocument()
    expect(screen.getByText('nothing is late')).toBeInTheDocument()
    const tile = screen.getAllByText('Tasks done').find((e) => e.classList.contains('label'))!.parentElement as HTMLElement
    expect(within(tile).getByText('—')).toBeInTheDocument()
  })

  it('shows an error with a retry', async () => {
    const user = userEvent.setup()
    let fail = true
    setup(() => (fail ? jsonResponse({ detail: 'The server had a problem. Please try again.' }, 500) : analytics()))
    renderApp('/analytics')
    expect(await screen.findByRole('alert')).toHaveTextContent('The server had a problem')
    fail = false
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('Tasks by person')).toBeInTheDocument()
  })
})

describe('the Overdue tile', () => {
  it('never borrows the alert-banner style (it would lay out sideways)', async () => {
    setup(() => analytics())
    renderApp('/analytics')
    await screen.findByText('Recorded time')
    const tile = screen.getAllByText('Overdue').find((e) => e.classList.contains('label'))!.parentElement as HTMLElement
    expect(tile.className).toContain('is-late')
    expect(tile.className).not.toMatch(/\balert\b/)
  })

  it('and neither does the Dashboard’s', async () => {
    mockServer({
      ...emptyApp,
      'GET /auth/me': meHandler,
      '/api/v1/actions': ({ url }) => ({ count: 1, total: url.includes('due_to') ? 3 : 5, action_items: [] }),
    })
    renderApp('/')
    const label = await screen.findByText('Overdue')
    const tile = label.parentElement as HTMLElement
    await waitFor(() => expect(tile.className).toContain('is-late'))
    expect(tile.className).not.toMatch(/\balert\b/)
  })
})
