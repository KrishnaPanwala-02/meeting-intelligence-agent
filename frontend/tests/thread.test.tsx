import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { onSessionLost, tokens } from '../src/api/client'
import TaskThreadDialog from '../src/components/TaskThreadDialog'
import { ToastProvider } from '../src/components/ui'
import { dayKey } from '../src/lib/format'
import { emptyApp, jsonResponse, meHandler, mockServer, pair, renderApp } from './helpers'

beforeEach(() => {
  tokens.clear()
  onSessionLost(null)
  tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
  Element.prototype.scrollIntoView = vi.fn()
})
afterEach(() => vi.unstubAllGlobals())

const who = (name: string | null, email = 'x@y.com') => ({ id: 'u', name, email })
const entry = (id: string, over: Record<string, unknown> = {}) => ({
  id, kind: 'comment', body: `comment ${id}`, author: who('Anna Larsen'), mine: false, created_at: new Date(Date.now() - 120_000).toISOString().replace('Z', ''),
  requested_due: null, status: null, decided_by: null, decided_at: null, decision_note: null, ...over,
})
const request = (id: string, status: string, over: Record<string, unknown> = {}) =>
  entry(id, { kind: 'extension_request', body: 'Waiting on the data', author: who('Rahul Patel'), requested_due: '2026-12-01', status, ...over })

const abilities = (over: Record<string, boolean> = {}) => ({ can_comment: true, can_request_extension: false, can_decide: false, ...over })
const task = { id: 'a', task: 'Write the report', due_date: '2026-11-16T00:00:00' }

function open(thread: { comments: unknown[] } & Record<string, unknown>, routes: Record<string, unknown> = {}) {
  const server = mockServer({
    'GET /auth/me': meHandler,
    'GET /api/v1/meetings/m1/actions/a/thread': () => thread,
    ...routes,
  } as never)
  const onClose = vi.fn()
  const onChanged = vi.fn()
  render(
    <MemoryRouter>
      <ToastProvider>
        <TaskThreadDialog meetingId="m1" task={task} onClose={onClose} onChanged={onChanged} />
      </ToastProvider>
    </MemoryRouter>,
  )
  return { ...server, onClose, onChanged }
}

describe('reading the discussion', () => {
  it('shows who said what and when, in order', async () => {
    open({ comments: [entry('1'), entry('2', { author: who(null, 'sam@example.com'), body: 'line one\nline two' })], ...abilities() })
    const list = await screen.findByRole('list', { name: 'Comments' })
    const items = within(list).getAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(within(items[0]).getByText('Anna Larsen')).toBeInTheDocument()
    expect(within(items[0]).getByText(/2 min ago/)).toBeInTheDocument()
    expect(within(items[1]).getByText('sam@example.com')).toBeInTheDocument()   // no name: the e-mail
    expect(items[1].textContent).toContain('line one\nline two')
    expect(screen.getByRole('dialog', { name: 'Discussion: Write the report' })).toBeInTheDocument()
  })

  it('says so when nobody has commented', async () => {
    open({ comments: [], ...abilities() })
    expect(await screen.findByText('No comments yet.')).toBeInTheDocument()
  })

  it('shows each kind of request: waiting, approved, declined', async () => {
    open({
      comments: [
        request('p', 'pending'),
        request('ok', 'approved', { decided_by: who('Krishna Panwala'), decision_note: 'Fine' }),
        request('no', 'declined', { decided_by: who('Krishna Panwala'), decision_note: null }),
      ],
      ...abilities(),
    })
    await screen.findByRole('list', { name: 'Comments' })
    expect(screen.getAllByText('Rahul Patel asked for more time')).toHaveLength(3)
    expect(screen.getByText('waiting for an answer')).toBeInTheDocument()
    expect(screen.getByText('approved')).toBeInTheDocument()
    expect(screen.getByText('declined')).toBeInTheDocument()
    expect(screen.getByText(/Approved by Krishna Panwala: “Fine”/)).toBeInTheDocument()
    expect(screen.getByText(/Declined by Krishna Panwala$/)).toBeInTheDocument()
    expect(screen.getAllByText(/New deadline asked:/)).toHaveLength(3)
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument() // not the owner
  })

  it('shows why it could not load, with a retry', async () => {
    const user = userEvent.setup()
    let fail = true
    mockServer({ 'GET /auth/me': meHandler, 'GET /api/v1/meetings/m1/actions/a/thread': () => (fail ? jsonResponse({ detail: 'Not found.' }, 404) : { comments: [], ...abilities() }) } as never)
    render(<MemoryRouter><ToastProvider><TaskThreadDialog meetingId="m1" task={task} onClose={() => undefined} onChanged={() => undefined} /></ToastProvider></MemoryRouter>)
    expect(await screen.findByRole('alert')).toHaveTextContent('Not found.')
    fail = false
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('No comments yet.')).toBeInTheDocument()
  })
})

describe('commenting', () => {
  it('posts a comment, clears the box and refreshes', async () => {
    const user = userEvent.setup()
    const comments: unknown[] = [entry('1')]
    const { calls, onChanged } = open(
      { comments, ...abilities() },
      { 'POST /api/v1/meetings/m1/actions/a/comments': ({ body }: { body: { body: string } }) => (comments.push(entry('2', { body: body.body, mine: true, author: who('Rahul Patel') })), entry('2')) },
    )
    const box = await screen.findByLabelText('Add a comment')
    const post = screen.getByRole('button', { name: 'Post comment' })
    expect(post).toBeDisabled()
    await user.type(box, '  Sending the data today  ')
    await user.click(post)
    expect(await screen.findByText('Sending the data today')).toBeInTheDocument()
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ body: 'Sending the data today' })
    expect(box).toHaveValue('')
    expect(onChanged).toHaveBeenCalledOnce()
  })

  it('Ctrl+Enter posts too, and a failure keeps the text', async () => {
    const user = userEvent.setup()
    const { calls } = open({ comments: [], ...abilities() }, { 'POST /api/v1/meetings/m1/actions/a/comments': () => jsonResponse({ detail: 'This task has too many comments' }, 409) })
    const box = await screen.findByLabelText('Add a comment')
    await user.type(box, 'hello{Control>}{Enter}{/Control}')
    expect(await screen.findByText('This task has too many comments')).toBeInTheDocument()
    expect(calls.some((c) => c.method === 'POST')).toBe(true)
    expect(box).toHaveValue('hello')
  })

  it('people who cannot comment get no box', async () => {
    open({ comments: [entry('1')], ...abilities({ can_comment: false }) })
    await screen.findByRole('list', { name: 'Comments' })
    expect(screen.queryByLabelText('Add a comment')).not.toBeInTheDocument()
  })

  it('you can delete your own comments, and the owner can delete any', async () => {
    const user = userEvent.setup()
    const { calls } = open(
      { comments: [entry('1', { mine: true }), entry('2')], ...abilities() },
      { 'DELETE /api/v1/meetings/m1/actions/a/comments/1': () => undefined },
    )
    await screen.findByRole('list', { name: 'Comments' })
    expect(screen.getAllByRole('button', { name: /Delete comment by/ })).toHaveLength(1)    // only the one that is yours
    await user.click(screen.getByRole('button', { name: /Delete comment by/ }))
    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE' && c.url.endsWith('/comments/1'))).toBe(true))
  })

  it('the owner sees delete on everyone’s comments', async () => {
    open({ comments: [entry('1'), entry('2')], ...abilities({ can_decide: true }) })
    await screen.findByRole('list', { name: 'Comments' })
    expect(screen.getAllByRole('button', { name: /Delete comment by/ })).toHaveLength(2)
  })
})

describe('asking for more time', () => {
  it('is offered to the assignee, with a date that must be later than the deadline', async () => {
    open({ comments: [], ...abilities({ can_request_extension: true }) })
    const date = (await screen.findByLabelText('New deadline')) as HTMLInputElement
    const dueNextDay = '2026-11-17'
    const tomorrow = new Date()
    tomorrow.setDate(tomorrow.getDate() + 1)
    expect(date.min >= dueNextDay).toBe(true)
    expect(date.min >= dayKey(tomorrow)).toBe(true)
    expect(screen.getByRole('button', { name: 'Ask for more time' })).toBeDisabled()
    expect(screen.getByText(/The meeting owner will be told/)).toBeInTheDocument()
  })

  it('is not offered to anyone else', async () => {
    open({ comments: [], ...abilities() })
    await screen.findByText('No comments yet.')
    expect(screen.queryByLabelText('New deadline')).not.toBeInTheDocument()
  })

  it('sends the request with the reason', async () => {
    const user = userEvent.setup()
    const { calls, onChanged } = open({ comments: [], ...abilities({ can_request_extension: true }) }, { 'POST /api/v1/meetings/m1/actions/a/extension': () => request('r', 'pending') })
    await user.type(await screen.findByLabelText('New deadline'), '2030-01-20')
    await user.type(screen.getByLabelText('Reason (optional)'), 'Waiting on the data')
    await user.click(screen.getByRole('button', { name: 'Ask for more time' }))
    expect(await screen.findByText('Request sent.')).toBeInTheDocument()
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ requested_due: '2030-01-20T00:00:00', reason: 'Waiting on the data' })
    expect(onChanged).toHaveBeenCalled()
    expect(screen.getByLabelText('New deadline')).toHaveValue('')
  })

  it('works without a reason, and shows the server’s objection', async () => {
    const user = userEvent.setup()
    const { calls } = open({ comments: [], ...abilities({ can_request_extension: true }) }, { 'POST /api/v1/meetings/m1/actions/a/extension': () => jsonResponse({ detail: 'There is already a request waiting for an answer' }, 409) })
    await user.type(await screen.findByLabelText('New deadline'), '2030-01-20')
    await user.click(screen.getByRole('button', { name: 'Ask for more time' }))
    expect(await screen.findByText('There is already a request waiting for an answer')).toBeInTheDocument()
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ requested_due: '2030-01-20T00:00:00' })
    expect(screen.getByLabelText('New deadline')).toHaveValue('2030-01-20')
  })
})

describe('answering a request', () => {
  const answer = (over = {}) => ({ ...task, comments: [request('r', 'approved', { decided_by: who('Krishna Panwala') })], ...abilities({ can_decide: true }), ...over })

  it('the owner approves, with a note, and the deadline moves', async () => {
    const user = userEvent.setup()
    const { calls, onChanged } = open(
      { comments: [request('r', 'pending')], ...abilities({ can_decide: true }) },
      { 'POST /api/v1/meetings/m1/actions/a/extension/r/decision': () => answer() },
    )
    await user.type(await screen.findByLabelText('Note for your answer (optional)'), 'Fine, but no later.')
    await user.click(screen.getByRole('button', { name: 'Approve' }))
    expect(await screen.findByText('Deadline moved.')).toBeInTheDocument()
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ approve: true, note: 'Fine, but no later.' })
    expect(onChanged).toHaveBeenCalled()
  })

  it('or declines, without a note', async () => {
    const user = userEvent.setup()
    const { calls } = open(
      { comments: [request('r', 'pending')], ...abilities({ can_decide: true }) },
      { 'POST /api/v1/meetings/m1/actions/a/extension/r/decision': () => answer() },
    )
    await user.click(await screen.findByRole('button', { name: 'Decline' }))
    expect(await screen.findByText('Request declined.')).toBeInTheDocument()
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ approve: false })
  })

  it('an answered request offers no buttons', async () => {
    open({ comments: [request('r', 'approved', { decided_by: who('Krishna Panwala') })], ...abilities({ can_decide: true }) })
    await screen.findByRole('list', { name: 'Comments' })
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Decline' })).not.toBeInTheDocument()
  })
})

describe('where the discussion opens from', () => {
  const row = (over: Record<string, unknown> = {}) => ({
    id: 'a', task: 'Write the report', assignee: 'Rahul Patel', due_date: '2026-11-16T00:00:00', due_phrase: null, needs_review: false, priority: 'medium',
    status: 'pending', created_at: null, completed_at: null, meeting_id: 'm1', meeting_title: 'ZOMATO', can_write: true, mine: true, can_update_status: true,
    comment_count: 3, pending_extension: null, ...over,
  })

  it('a task row shows how many comments, and a waiting request', async () => {
    mockServer({ ...emptyApp, 'GET /auth/me': meHandler, '/api/v1/actions': () => ({ count: 2, total: 2, action_items: [row(), row({ id: 'b', task: 'Other', comment_count: 0, pending_extension: { id: 'r', requested_due: '2026-12-01', by: who('Rahul Patel') } })] }) })
    renderApp('/tasks')
    expect(await screen.findByRole('button', { name: 'Discuss: Write the report, 3 comments' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Discuss: Other, more time asked' })).toBeInTheDocument()
  })

  it('opening it from a row, and changes refresh the row', async () => {
    const user = userEvent.setup()
    let count = 3
    const { calls } = mockServer({
      ...emptyApp,
      'GET /auth/me': meHandler,
      '/api/v1/actions': () => ({ count: 1, total: 1, action_items: [row()] }),
      'GET /api/v1/meetings/m1/actions/a/thread': () => ({ comments: [entry('1')], ...abilities() }),
      'POST /api/v1/meetings/m1/actions/a/comments': () => ((count = 4), entry('9')),
      'GET /api/v1/meetings/m1/actions/a': () => row({ comment_count: count }),
    })
    renderApp('/tasks')
    await user.click(await screen.findByRole('button', { name: 'Discuss: Write the report, 3 comments' }))
    await user.type(await screen.findByLabelText('Add a comment'), 'one more')
    await user.click(screen.getByRole('button', { name: 'Post comment' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'GET' && c.url === '/api/v1/meetings/m1/actions/a')).toBe(true))
    await user.click(screen.getByRole('button', { name: 'Close' }))
    expect(await screen.findByRole('button', { name: 'Discuss: Write the report, 4 comments' })).toBeInTheDocument()
  })

  const meeting = {
    id: 'm1', title: 'ZOMATO', status: 'uploaded', scheduled_time: null, end_time: null, duration_minutes: 1, participants: [], owner_email: 'x@y.com',
    owner_name: null, is_owner: true, started_at: null, ended_at: null, has_audio: false, created_at: '2026-11-13T12:00:00', can_write: true,
    has_transcript: true, has_summary: true, action_count: 1, open_action_count: 1,
  }
  const meetingServer = () =>
    mockServer({
      ...emptyApp,
      'GET /auth/me': meHandler,
      '/api/v1/meetings/m1': () => meeting,
      '/api/v1/meetings/m1/jobs/latest': () => jsonResponse({ detail: 'none' }, 404),
      '/api/v1/meetings/m1/transcript': () => ({ meeting_id: 'm1', transcript_id: 't', language: 'en', transcript: '', word_count: 1, speaker_count: 1, created_at: null, segments: [] }),
      '/api/v1/meetings/m1/summary': () => ({ meeting_id: 'm1', executive_summary: 'x', key_points: [], decisions: [], next_steps: [], key_quotes: [], created_at: null }),
      '/api/v1/meetings/m1/actions': () => ({ meeting_id: 'm1', count: 1, action_items: [row()] }),
      'GET /api/v1/meetings/m1/actions/a/thread': () => ({ comments: [entry('1')], ...abilities({ can_decide: true }) }),
    })

  it('a link from a notification opens that task’s discussion, once', async () => {
    const user = userEvent.setup()
    meetingServer()
    renderApp('/meetings/m1?tab=actions&task=a')
    expect(await screen.findByRole('dialog', { name: 'Discussion: Write the report' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Close' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await new Promise((r) => setTimeout(r, 100))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()          // it does not pop up again
  })

  it('the meeting’s task list has the button too, and ignores an unknown task id', async () => {
    const user = userEvent.setup()
    meetingServer()
    renderApp('/meetings/m1?tab=actions&task=nope')
    const button = await screen.findByRole('button', { name: 'Discuss: Write the report, 3 comments' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await user.click(button)
    expect(await screen.findByRole('dialog', { name: 'Discussion: Write the report' })).toBeInTheDocument()
  })
})
