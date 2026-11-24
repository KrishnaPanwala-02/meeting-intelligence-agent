import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { onSessionLost, tokens } from '../src/api/client'
import { deadlinePresets } from '../src/lib/deadlines'
import { emptyApp, jsonResponse, meHandler, mockServer, pair, renderApp } from './helpers'

beforeEach(() => {
  tokens.clear()
  onSessionLost(null)
  tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
  Element.prototype.scrollIntoView = vi.fn()
})
afterEach(() => vi.unstubAllGlobals())

const labelsByDate = (d: Date) => Object.fromEntries(deadlinePresets(d).map((p) => [p.label, p.date]))

describe('deadlinePresets', () => {
  it('offers tomorrow, three days, the end of this and next week, and the end of the month', () => {
    // Wednesday 11 Nov 2026
    expect(labelsByDate(new Date(2026, 10, 11))).toEqual({
      Tomorrow: '2026-11-12', 'In 3 days': '2026-11-14', 'End of this week': '2026-11-13', 'End of next week': '2026-11-20', 'End of this month': '2026-11-30',
    })
  })
  it('on a Friday or weekend, "this week" means the coming Friday', () => {
    expect(labelsByDate(new Date(2026, 10, 13))['End of this week']).toBe('2026-11-20') // Friday
    expect(labelsByDate(new Date(2026, 10, 14))['End of this week']).toBe('2026-11-20') // Saturday
    expect(labelsByDate(new Date(2026, 10, 15))['End of this week']).toBe('2026-11-20') // Sunday
    expect(labelsByDate(new Date(2026, 10, 15))['End of next week']).toBe('2026-11-27')
  })
  it('never offers the same date twice, or today, or the past', () => {
    const tue = deadlinePresets(new Date(2026, 10, 10)) // Tue: in 3 days is Friday, the end of the week
    expect(tue.map((p) => p.date)).toEqual([...new Set(tue.map((p) => p.date))])
    expect(tue.find((p) => p.date === '2026-11-13')!.label).toBe('In 3 days')
    const lastDay = deadlinePresets(new Date(2026, 10, 30))
    expect(lastDay.some((p) => p.label === 'End of this month')).toBe(false)         // that would be today
    expect(lastDay.every((p) => p.date > '2026-11-30')).toBe(true)
    expect(labelsByDate(new Date(2026, 11, 20))['End of this month']).toBe('2026-12-31')
    expect(labelsByDate(new Date(2028, 1, 10))['End of this month']).toBe('2028-02-29')   // leap year
  })
})

const task = (id: string, name: string, phrase: string | null, over: Record<string, unknown> = {}) => ({
  id, task: name, assignee: 'Rahul Patel', due_date: null, due_phrase: phrase, needs_review: !!phrase, priority: 'medium', status: 'pending',
  created_at: null, completed_at: null, meeting_id: 'm1', meeting_title: 'ZOMATO', can_write: true, mine: false, can_update_status: false, ...over,
})

function setup(reviewRows: ReturnType<typeof task>[], extra: Record<string, unknown> = {}) {
  let rows = reviewRows
  return mockServer({
    ...emptyApp,
    'GET /auth/me': meHandler,
    '/api/v1/actions': ({ url }) => {
      const flagged = url.includes('needs_review=true')
      const list = flagged ? rows : []
      return { count: list.length, total: list.length, action_items: list }
    },
    'PUT /api/v1/meetings/m1/actions/a': ({ body }) => {
      rows = rows.filter((r) => r.id !== 'a')
      return { ...task('a', 'Book the room', null), due_date: body.due_date }
    },
    ...extra,
  })
}

describe('reviewing unclear deadlines on the Tasks page', () => {
  it('lists each task with what was said, and quick answers', async () => {
    setup([task('a', 'Book the room', 'soon'), task('b', 'Send the invite', 'next month', { assignee: 'TBD' })])
    renderApp('/tasks?review=1')
    const heading = await screen.findByText(/Needs a deadline/)
    expect(within(heading.parentElement as HTMLElement).getByText('2')).toBeInTheDocument()
    expect(screen.getByText('Book the room')).toBeInTheDocument()
    expect(screen.getByText('soon')).toBeInTheDocument()
    expect(screen.getByText('next month')).toBeInTheDocument()
    expect(screen.getAllByRole('link', { name: 'ZOMATO' })[0]).toHaveAttribute('href', '/meetings/m1?tab=actions')
    expect(screen.getAllByRole('button', { name: 'Tomorrow' })).toHaveLength(2)
  })

  it('a quick answer sets that date, and the task leaves the list', async () => {
    const user = userEvent.setup()
    const { calls } = setup([task('a', 'Book the room', 'soon'), task('b', 'Send the invite', 'later')])
    renderApp('/tasks')
    const row = (await screen.findByText('Book the room')).closest('.review-row') as HTMLElement
    const tomorrow = deadlinePresets().find((p) => p.label === 'Tomorrow')!.date
    await user.click(within(row).getByRole('button', { name: 'Tomorrow' }))
    await waitFor(() => expect(screen.queryByText('Book the room')).not.toBeInTheDocument())
    expect(calls.find((c) => c.method === 'PUT')!.body).toEqual({ due_date: `${tomorrow}T00:00:00` })
    expect(screen.getByText('Send the invite')).toBeInTheDocument()
    expect(await screen.findByText(/Deadline set for “Book the room”/)).toBeInTheDocument()
  })

  it('a chosen date is saved with "Set date", which needs a date first', async () => {
    const user = userEvent.setup()
    const { calls } = setup([task('a', 'Book the room', 'soon')])
    renderApp('/tasks')
    const save = await screen.findByRole('button', { name: 'Save deadline for Book the room' })
    expect(save).toBeDisabled()
    const input = screen.getByLabelText('Deadline for Book the room') as HTMLInputElement
    await user.type(input, '2030-05-17')
    expect(save).toBeEnabled()
    await user.click(save)
    await waitFor(() => expect(calls.find((c) => c.method === 'PUT')!.body).toEqual({ due_date: '2030-05-17T00:00:00' }))
    await waitFor(() => expect(screen.queryByText(/Needs a deadline/)).not.toBeInTheDocument()) // nothing left: the card goes
  })

  it('leaves it to the owner when you cannot edit the meeting', async () => {
    setup([task('a', 'Book the room', 'soon', { can_write: false })])
    renderApp('/tasks')
    expect(await screen.findByText('Only the meeting owner can set it.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Tomorrow' })).not.toBeInTheDocument()
  })

  it('shows why saving failed and keeps the task', async () => {
    const user = userEvent.setup()
    setup([task('a', 'Book the room', 'soon')], { 'PUT /api/v1/meetings/m1/actions/a': () => jsonResponse({ detail: 'Only the meeting owner can modify this meeting' }, 403) })
    renderApp('/tasks')
    await user.click(await screen.findByRole('button', { name: 'In 3 days' }))
    expect(await screen.findByText('Only the meeting owner can modify this meeting')).toBeInTheDocument()
    expect(screen.getByText('Book the room')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'In 3 days' })).toBeEnabled()
  })

  it('is not shown at all when every deadline is clear', async () => {
    setup([])
    renderApp('/tasks')
    await screen.findByRole('heading', { level: 1, name: 'My tasks' })
    expect(screen.queryByText(/Needs a deadline/)).not.toBeInTheDocument()
  })
})

describe('the dashboard reminder', () => {
  const open = (rows: ReturnType<typeof task>[]) =>
    mockServer({
      ...emptyApp,
      'GET /auth/me': meHandler,
      '/api/v1/actions': () => ({ count: rows.length, total: rows.length, action_items: rows }),
    })

  it('tells the owner how many tasks need a date, and links to them', async () => {
    open([task('a', 'A', 'soon'), task('b', 'B', 'later'), task('c', 'C', null)])
    renderApp('/')
    expect(await screen.findByText(/2 tasks need a deadline/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Review' })).toHaveAttribute('href', '/tasks?review=1')
  })

  it('uses the singular for one', async () => {
    open([task('a', 'A', 'soon')])
    renderApp('/')
    expect(await screen.findByText(/1 task needs a deadline/)).toBeInTheDocument()
  })

  it('says nothing to someone who cannot fix them, or when there are none', async () => {
    open([task('a', 'A', 'soon', { can_write: false }), task('b', 'B', null)])
    renderApp('/')
    await screen.findByText('Assigned to you')
    expect(screen.queryByText(/need a deadline|needs a deadline/)).not.toBeInTheDocument()
  })
})

describe('in the meeting’s task list', () => {
  it('shows what was said next to the date field', async () => {
    mockServer({
      ...emptyApp,
      'GET /auth/me': meHandler,
      '/api/v1/meetings/m1': () => ({
        id: 'm1', title: 'ZOMATO', status: 'uploaded', scheduled_time: null, end_time: null, duration_minutes: 1, participants: [], owner_email: 'x@y.com',
        owner_name: null, is_owner: true, started_at: null, ended_at: null, has_audio: false, created_at: '2026-11-13T12:00:00', can_write: true,
        has_transcript: true, has_summary: true, action_count: 2, open_action_count: 2,
      }),
      '/api/v1/meetings/m1/jobs/latest': () => jsonResponse({ detail: 'none' }, 404),
      '/api/v1/meetings/m1/transcript': () => ({ meeting_id: 'm1', transcript_id: 't', language: 'en', transcript: '', word_count: 1, speaker_count: 1, created_at: null, segments: [] }),
      '/api/v1/meetings/m1/summary': () => ({ meeting_id: 'm1', executive_summary: 'x', key_points: [], decisions: [], next_steps: [], key_quotes: [], created_at: null }),
      '/api/v1/meetings/m1/actions': () => ({ meeting_id: 'm1', count: 2, action_items: [task('a', 'Book the room', 'soon'), { ...task('b', 'Done thing', null), due_date: '2026-11-20T00:00:00' }] }),
    })
    renderApp('/meetings/m1?tab=actions')
    expect(await screen.findByText('Said: “soon”')).toBeInTheDocument()
    expect(screen.getAllByText(/Said:/)).toHaveLength(1)   // only the unclear one
  })
})
