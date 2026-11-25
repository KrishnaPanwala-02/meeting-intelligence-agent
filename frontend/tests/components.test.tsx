import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { onSessionLost, tokens } from '../src/api/client'
import type { TaskRow } from '../src/api/types'
import { TaskRowItem } from '../src/components/domain'
import { ChipInput, ToastProvider } from '../src/components/ui'
import { emptyApp, jsonResponse, me, meHandler, mockServer, pair, renderApp } from './helpers'

beforeEach(() => {
  tokens.clear()
  onSessionLost(null)
})
afterEach(() => {
  vi.unstubAllGlobals()
})

const MEMBERS = [
  { name: 'Darshan Patel', email: 'darshanpatel@gmail.com' },
  { name: 'Jai Jain', email: 'jaijain@gmail.com' },
  { name: 'Meet Oza', email: 'meetoza@gmail.com' },
]

describe('ChipInput member picker', () => {
  function Harness({ start = [] as string[] }) {
    const [v, setV] = useState<string[]>(start)
    return (
      <>
        <ChipInput label="Participants" values={v} onChange={setV} suggestions={MEMBERS} />
        <output data-testid="values">{v.join('|')}</output>
      </>
    )
  }

  it('lists every member when the box is focused, with name and e-mail', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await user.click(screen.getByLabelText('Participants'))
    const list = await screen.findByRole('listbox')
    expect(within(list).getAllByRole('option')).toHaveLength(3)
    expect(within(list).getByText('Jai Jain')).toBeInTheDocument()
    expect(within(list).getByText('jaijain@gmail.com')).toBeInTheDocument()
  })

  it('clicking a member adds their e-mail and keeps the list for the next one', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await user.click(screen.getByLabelText('Participants'))
    await user.click(await screen.findByRole('option', { name: /Jai Jain/ }))
    expect(screen.getByTestId('values')).toHaveTextContent('jaijain@gmail.com')
    expect(screen.getByText('jaijain@gmail.com', { selector: '.chip' })).toBeInTheDocument()
    expect(screen.getAllByRole('option')).toHaveLength(2)                       // already added: not offered again
  })

  it('filters by name or by e-mail as you type', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await user.type(screen.getByLabelText('Participants'), 'oza')
    expect(screen.getAllByRole('option')).toHaveLength(1)
    await user.clear(screen.getByLabelText('Participants'))
    await user.type(screen.getByLabelText('Participants'), 'DARSHAN')
    expect(screen.getByRole('option', { name: /Darshan Patel/ })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /Jai Jain/ })).not.toBeInTheDocument()
  })

  it('arrow keys and Enter pick a member; Enter with nothing highlighted still adds typed text', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    const box = screen.getByLabelText('Participants')
    await user.click(box)
    await user.keyboard('{ArrowDown}{ArrowDown}{Enter}')
    expect(screen.getByTestId('values')).toHaveTextContent('jaijain@gmail.com')
    await user.type(box, 'someone@else.com{Enter}')
    expect(screen.getByTestId('values')).toHaveTextContent('jaijain@gmail.com|someone@else.com')
  })

  it('Escape closes the list, and an e-mail typed in full still works for people who are not members', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    const box = screen.getByLabelText('Participants')
    await user.click(box)
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    await user.type(box, 'guest@x.com,')
    expect(screen.getByTestId('values')).toHaveTextContent('guest@x.com')
  })

  it('shows no list when there are no suggestions (behaves as before)', async () => {
    const user = userEvent.setup()
    render(<ChipInput label="Participants" values={[]} onChange={() => undefined} />)
    await user.click(screen.getByLabelText('Participants'))
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })
})

describe('ChipInput', () => {
  function Harness({ initial = [] as string[] }) {
    const [v, setV] = useState(initial)
    return (
      <>
        <ChipInput label="Participants" values={v} onChange={setV} />
        <output data-testid="out">{JSON.stringify(v)}</output>
      </>
    )
  }
  const out = () => JSON.parse(screen.getByTestId('out').textContent!)

  it('adds on Enter and on comma, ignoring duplicates case-insensitively', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    const box = screen.getByLabelText('Participants')
    await user.type(box, 'a@x.com{Enter}')
    await user.type(box, 'b@x.com,')
    await user.type(box, 'A@X.COM{Enter}')
    expect(out()).toEqual(['a@x.com', 'b@x.com'])
  })

  it('accepts several addresses pasted at once', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await user.click(screen.getByLabelText('Participants'))
    await user.paste('a@x.com, b@x.com; c@x.com')
    await user.keyboard('{Enter}')
    expect(out()).toEqual(['a@x.com', 'b@x.com', 'c@x.com'])
  })

  it('removes with the button and with Backspace; commits on blur', async () => {
    const user = userEvent.setup()
    render(<Harness initial={['a@x.com', 'b@x.com']} />)
    await user.click(screen.getByRole('button', { name: 'Remove a@x.com' }))
    expect(out()).toEqual(['b@x.com'])
    await user.click(screen.getByLabelText('Participants'))
    await user.keyboard('{Backspace}')
    expect(out()).toEqual([])
    await user.type(screen.getByLabelText('Participants'), 'late@x.com')
    await user.tab()
    expect(out()).toEqual(['late@x.com'])
  })
})

describe('TaskRowItem', () => {
  const task: TaskRow = {
    id: 'a1', task: 'Finish the report', assignee: 'Speaker 1', due_date: '2026-11-13T00:00:00', priority: 'high',
    status: 'pending', created_at: null, completed_at: null, meeting_id: 'm1', meeting_title: 'Weekly sync', can_write: true, mine: true, can_update_status: true,
  }
  function Harness({ t = task }: { t?: TaskRow }) {
    const [cur, setCur] = useState(t)
    return (
      <MemoryRouter>
        <ToastProvider>
          <TaskRowItem task={cur} onUpdated={setCur} />
          <output data-testid="status">{cur.status}</output>
        </ToastProvider>
      </MemoryRouter>
    )
  }

  it('completes optimistically and saves to the server', async () => {
    const { calls } = mockServer({
      'PUT /api/v1/meetings/m1/actions/a1': ({ body }) => ({ ...task, status: body.status, completed_at: '2026-11-12T10:00:00' }),
    })
    const user = userEvent.setup()
    render(<Harness />)
    await user.click(screen.getByRole('checkbox', { name: /Complete: Finish the report/ }))
    expect(screen.getByTestId('status')).toHaveTextContent('completed')
    await waitFor(() => expect(calls).toHaveLength(1))
    expect(calls[0]).toMatchObject({ method: 'PUT', body: { status: 'completed' } })
  })

  it('rolls back and explains when the server refuses', async () => {
    mockServer({ 'PUT /api/v1/meetings/m1/actions/a1': () => jsonResponse({ detail: 'Only the meeting owner can modify this meeting' }, 403) })
    const user = userEvent.setup()
    render(<Harness />)
    await user.click(screen.getByRole('checkbox'))
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('pending'))
    expect(await screen.findByText('Only the meeting owner can modify this meeting')).toBeInTheDocument()
  })

  it('cannot be ticked by someone it is not assigned to, and says who can', () => {
    render(<Harness t={{ ...task, assignee: 'Rahul Patel', mine: false, can_update_status: false }} />)
    const box = screen.getByRole('checkbox')
    expect(box).toBeDisabled()
    expect(box).toHaveAttribute('title', 'Only Rahul Patel can complete this task')
    expect(screen.getByText('Rahul Patel')).toBeInTheDocument()
    expect(screen.queryByText('You')).not.toBeInTheDocument()
  })

  it('can be ticked by its assignee even without owner rights, and is marked "You"', () => {
    render(<Harness t={{ ...task, assignee: 'Krishna Panwala', can_write: false, mine: true, can_update_status: true }} />)
    expect(screen.getByRole('checkbox')).toBeEnabled()
    expect(screen.getByText('You')).toBeInTheDocument()
  })

  it('points unclaimed tasks at the meeting owner', () => {
    render(<Harness t={{ ...task, assignee: 'Speaker 2', mine: false, can_update_status: false }} />)
    expect(screen.getByRole('checkbox')).toHaveAttribute('title', 'Only the meeting owner can change this task')
  })

  it('flags overdue work', () => {
    render(<Harness t={{ ...task, due_date: '2020-01-01T00:00:00' }} />)
    expect(screen.getByText(/overdue/i)).toBeInTheDocument()
  })

  it('never calls an unfinished task overdue once completed', () => {
    render(<Harness t={{ ...task, due_date: '2020-01-01T00:00:00', status: 'completed' }} />)
    expect(screen.queryByText(/overdue/i)).not.toBeInTheDocument()
  })
})

describe('authentication flow', () => {
  it('sends signed-out visitors to the sign-in page', async () => {
    mockServer({ ...emptyApp })
    renderApp('/meetings')
    expect(await screen.findByRole('heading', { name: 'Welcome back' })).toBeInTheDocument()
  })

  it('signs in, stores tokens, and lands on the dashboard', async () => {
    const user = userEvent.setup()
    const { calls } = mockServer({
      ...emptyApp,
      'POST /auth/login': () => pair(1),
      'GET /auth/me': meHandler,
    })
    renderApp('/')
    await user.type(await screen.findByLabelText('E-mail'), 'anna@example.com')
    await user.type(screen.getByLabelText('Password'), 'a long passphrase')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByText(/Good (morning|afternoon|evening), Anna/)).toBeInTheDocument()
    expect(calls.find((c) => c.url === '/auth/login')!.body).toEqual({ email: 'anna@example.com', password: 'a long passphrase' })
    expect(tokens.refresh).toBe('refresh-1')
    expect(tokens.access).toBe('access-1')
  })

  it('shows the server’s message for a wrong password and stays on the form', async () => {
    const user = userEvent.setup()
    mockServer({ ...emptyApp, 'POST /auth/login': () => jsonResponse({ detail: 'Invalid e-mail or password' }, 401) })
    renderApp('/login')
    await user.type(await screen.findByLabelText('E-mail'), 'anna@example.com')
    await user.type(screen.getByLabelText('Password'), 'wrong')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid e-mail or password')
    expect(tokens.access).toBeNull()
  })

  it('tells the user when the account is temporarily locked', async () => {
    const user = userEvent.setup()
    mockServer({ 'POST /auth/login': () => jsonResponse({ detail: 'Too many failed attempts. Try again later.' }, 429) })
    renderApp('/login')
    await user.type(await screen.findByLabelText('E-mail'), 'a@x.com')
    await user.type(screen.getByLabelText('Password'), 'x')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/Too many failed attempts/)
  })

  it('restores the session after a page reload using the refresh token when no access token survived', async () => {
    localStorage.setItem('mia.refresh', 'refresh-0')
    const { calls } = mockServer({
      ...emptyApp,
      'POST /auth/refresh': () => pair(2),
      'GET /auth/me': meHandler,
    })
    renderApp('/')
    expect(await screen.findByText(/Good (morning|afternoon|evening), Anna/)).toBeInTheDocument()
    expect(calls.find((c) => c.url === '/auth/refresh')!.body).toEqual({ refresh_token: 'refresh-0' })
    expect(tokens.refresh).toBe('refresh-2') // rotated
  })

  it('a reload reuses the tab’s still-valid access token and spends NO refresh', async () => {
    localStorage.setItem('mia.refresh', 'refresh-0')
    sessionStorage.setItem('mia.access', JSON.stringify({ token: 'access-live', exp: Date.now() + 600_000 }))
    const { calls } = mockServer({ ...emptyApp, 'GET /auth/me': meHandler })
    renderApp('/')
    expect(await screen.findByText(/Good (morning|afternoon|evening), Anna/)).toBeInTheDocument()
    expect(calls.some((c) => c.url === '/auth/refresh')).toBe(false)
    expect(tokens.access).toBe('access-live')
  })

  it('ignores an access token that is about to expire and refreshes instead', async () => {
    localStorage.setItem('mia.refresh', 'refresh-0')
    sessionStorage.setItem('mia.access', JSON.stringify({ token: 'access-stale', exp: Date.now() + 10_000 }))
    const { calls } = mockServer({ ...emptyApp, 'POST /auth/refresh': () => pair(2), 'GET /auth/me': meHandler })
    renderApp('/')
    expect(await screen.findByText(/Good (morning|afternoon|evening), Anna/)).toBeInTheDocument()
    expect(calls.some((c) => c.url === '/auth/refresh')).toBe(true)
  })

  it('recovers when the cached access token was revoked server-side', async () => {
    localStorage.setItem('mia.refresh', 'refresh-0')
    sessionStorage.setItem('mia.access', JSON.stringify({ token: 'revoked', exp: Date.now() + 600_000 }))
    mockServer({
      ...emptyApp,
      'POST /auth/refresh': () => pair(2),
      'GET /auth/me': ({ headers }) => (headers.Authorization === 'Bearer access-2' ? me : jsonResponse({ detail: 'x' }, 401)),
    })
    renderApp('/')
    expect(await screen.findByText(/Good (morning|afternoon|evening), Anna/)).toBeInTheDocument()
  })

  it('falls back to sign-in when the stored session was revoked', async () => {
    localStorage.setItem('mia.refresh', 'revoked')
    mockServer({ ...emptyApp, 'GET /auth/me': meHandler, 'POST /auth/refresh': () => jsonResponse({ detail: 'Invalid refresh token' }, 401) })
    renderApp('/tasks')
    expect(await screen.findByRole('heading', { name: 'Welcome back' })).toBeInTheDocument()
    expect(tokens.refresh).toBeNull()
  })

  it('signs out: calls the server, clears tokens, returns to sign-in', async () => {
    localStorage.setItem('mia.refresh', 'refresh-0')
    const user = userEvent.setup()
    const { calls } = mockServer({
      ...emptyApp,
      'POST /auth/refresh': () => pair(2),
      'GET /auth/me': meHandler,
      'POST /auth/logout': () => undefined,
    })
    renderApp('/')
    await user.click(await screen.findByRole('button', { name: 'Sign out' }))
    expect(await screen.findByRole('heading', { name: 'Welcome back' })).toBeInTheDocument()
    expect(calls.find((c) => c.url === '/auth/logout')!.body).toEqual({ refresh_token: 'refresh-2' })
    expect(tokens.refresh).toBeNull()
  })
})

describe('registration form', () => {
  it('validates before touching the network', async () => {
    const user = userEvent.setup()
    const { fn } = mockServer({})
    renderApp('/register')
    await user.type(await screen.findByLabelText('Full name'), 'New Person')
    await user.type(screen.getByLabelText('E-mail'), 'new@example.com')
    await user.type(screen.getByLabelText('Password'), 'short')
    expect(screen.getByText(/more characters needed/)).toBeInTheDocument()
    await user.type(screen.getByLabelText('Confirm password'), 'different')
    await user.click(screen.getByRole('button', { name: 'Create account' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/at least 10 characters/)
    expect(fn).not.toHaveBeenCalled()
  })

  it('creates the account and signs in', async () => {
    const user = userEvent.setup()
    const { calls } = mockServer({
      ...emptyApp,
      'POST /auth/register': () => ({ id: 'u1', email: 'anna@example.com', name: 'Anna Larsen' }),
      'POST /auth/login': () => pair(1),
      'POST /api/v1/voice-profiles/me': () => ({ id: 'v1', name: 'Anna Larsen', email: 'anna@example.com', samples: 1, speech_seconds: 12 }),
      'GET /auth/me': meHandler,
    })
    renderApp('/register')
    await user.type(await screen.findByLabelText('Full name'), 'Anna Larsen')
    await user.type(screen.getByLabelText('E-mail'), 'anna@example.com')
    await user.type(screen.getByLabelText('Password'), 'a long passphrase')
    await user.type(screen.getByLabelText('Confirm password'), 'a long passphrase')
    await user.upload(screen.getByTestId('voice-file'), new File(['x'], 'me.m4a', { type: 'audio/mp4' }))
    await user.click(screen.getByRole('button', { name: 'Create account' }))
    expect(await screen.findByText(/Good (morning|afternoon|evening), Anna/)).toBeInTheDocument()
    expect(calls.map((c) => c.url).slice(0, 2)).toEqual(['/auth/register', '/auth/login'])
    expect(calls[0].body).toEqual({ email: 'anna@example.com', password: 'a long passphrase', name: 'Anna Larsen' })
    expect(calls.some((c) => c.url === '/api/v1/voice-profiles/me')).toBe(true)
  })

  it('will not create the account without a voice', async () => {
    const user = userEvent.setup()
    const { fn } = mockServer({})
    renderApp('/register')
    await user.type(await screen.findByLabelText('Full name'), 'Anna Larsen')
    await user.type(screen.getByLabelText('E-mail'), 'anna@example.com')
    await user.type(screen.getByLabelText('Password'), 'a long passphrase')
    await user.type(screen.getByLabelText('Confirm password'), 'a long passphrase')
    await user.click(screen.getByRole('button', { name: 'Create account' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/voice/i)
    expect(fn).not.toHaveBeenCalled()
  })

  it('requires a full name', async () => {
    const user = userEvent.setup()
    const { fn } = mockServer({})
    renderApp('/register')
    await user.type(await screen.findByLabelText('Full name'), '   ')
    await user.type(screen.getByLabelText('E-mail'), 'a@example.com')
    await user.type(screen.getByLabelText('Password'), 'a long passphrase')
    await user.type(screen.getByLabelText('Confirm password'), 'a long passphrase')
    await user.click(screen.getByRole('button', { name: 'Create account' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/full name/i)
    expect(fn).not.toHaveBeenCalled()
  })

  it('shows a conflict when the e-mail is already registered', async () => {
    const user = userEvent.setup()
    mockServer({ 'POST /auth/register': () => jsonResponse({ detail: 'E-mail already registered' }, 409) })
    renderApp('/register')
    await user.type(await screen.findByLabelText('Full name'), 'Anna Larsen')
    await user.type(screen.getByLabelText('E-mail'), 'anna@example.com')
    await user.type(screen.getByLabelText('Password'), 'a long passphrase')
    await user.type(screen.getByLabelText('Confirm password'), 'a long passphrase')
    await user.upload(screen.getByTestId('voice-file'), new File(['x'], 'me.m4a', { type: 'audio/mp4' }))
    await user.click(screen.getByRole('button', { name: 'Create account' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('E-mail already registered')
  })
})

describe('forgot password', () => {
  it('shows the generic confirmation and the local admin command', async () => {
    const user = userEvent.setup()
    mockServer({ 'POST /auth/forgot-password': () => ({ detail: 'If an account exists…' }) })
    renderApp('/forgot-password')
    await user.type(await screen.findByLabelText('E-mail'), 'anna@example.com')
    await user.click(screen.getByRole('button', { name: /Send reset instructions/ }))
    expect(await screen.findByText(/If an account exists for that address/)).toBeInTheDocument()
    expect(screen.getByText(/src.cli reset-token anna@example.com/)).toBeInTheDocument()
    within(document.body).getByRole('link', { name: /I have a reset token/ })
  })
})

describe('dashboard status banner', () => {
  it('warns when Ollama is not running', async () => {
    localStorage.setItem('mia.refresh', 'r')
    mockServer({
      ...emptyApp,
      '/health/ready': () => ({ status: 'degraded', checks: { database: 'ok', redis: 'ok', ollama: 'fail' } }),
      'POST /auth/refresh': () => pair(2),
      'GET /auth/me': meHandler,
    })
    renderApp('/')
    expect(await screen.findByText(/AI engine \(Ollama\) is not running/)).toBeInTheDocument()
  })

  it('shows a helpful message when the backend is down entirely', async () => {
    localStorage.setItem('mia.refresh', 'r')
    mockServer({
      ...emptyApp,
      '/health/ready': () => {
        throw new TypeError('down')
      },
      'POST /auth/refresh': () => pair(2),
      'GET /auth/me': meHandler,
    })
    renderApp('/')
    expect(await screen.findByText(/Cannot reach the backend/)).toBeInTheDocument()
  })
})

describe('Modal', () => {
  it('does not steal focus when the parent re-renders with a new onClose', async () => {
    const { Modal } = await import('../src/components/ui')
    function Parent() {
      const [n, setN] = useState(0)
      return (
        <>
          <button onClick={() => setN((x) => x + 1)}>rerender {n}</button>
          <Modal title="T" onClose={() => undefined} footer={<button>OK</button>}>
            <input aria-label="field" />
          </Modal>
        </>
      )
    }
    const user = userEvent.setup()
    render(<Parent />)
    const field = screen.getByLabelText('field')
    await user.click(field)
    expect(field).toHaveFocus()
    // a parent re-render (e.g. job polling) must leave the user's focus alone
    screen.getByRole('button', { name: /rerender/ }).click()
    await screen.findByRole('button', { name: 'rerender 1' })
    expect(field).toHaveFocus()
  })

  it('closes on Escape using the latest handler', async () => {
    const { Modal } = await import('../src/components/ui')
    const onClose = vi.fn()
    const user = userEvent.setup()
    render(<Modal title="T" onClose={onClose} footer={<button>OK</button>}>body</Modal>)
    await user.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalledOnce()
  })
})

describe('display name', () => {
  const signedIn = () => {
    tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
  }

  it('asks accounts without a name to add one, and not those that have it', async () => {
    signedIn()
    mockServer({ ...emptyApp, 'GET /auth/me': () => ({ ...me, name: null }) })
    renderApp('/')
    expect(await screen.findByText(/Add your full name/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Add name' })).toHaveAttribute('href', '/settings')
  })

  it('does not nag when a name is set, and shows it in the sidebar', async () => {
    signedIn()
    mockServer({ ...emptyApp, 'GET /auth/me': meHandler })
    renderApp('/')
    expect(await screen.findByText(/Good (morning|afternoon|evening), Anna/)).toBeInTheDocument()
    expect(screen.queryByText(/Add your full name/)).not.toBeInTheDocument()
    expect(screen.getByText('Anna Larsen')).toBeInTheDocument()
  })

  it('saves the name from Settings', async () => {
    signedIn()
    const user = userEvent.setup()
    const { calls } = mockServer({
      ...emptyApp,
      'GET /auth/me': () => ({ ...me, name: null }),
      'PATCH /auth/me': ({ body }) => ({ ...me, name: (body as { name: string }).name }),
      '/auth/sessions': () => ({ sessions: [] }),
    })
    renderApp('/settings')
    const field = await screen.findByLabelText('Full name')
    await user.type(field, '  Krishna Panwala ')
    await user.click(screen.getByRole('button', { name: 'Save name' }))
    await waitFor(() => expect(screen.queryByText(/Add your full name/)).not.toBeInTheDocument())
    expect(calls.find((c) => c.url === '/auth/me' && c.method === 'PATCH')!.body).toEqual({ name: 'Krishna Panwala' })
  })
})

describe('speaker naming', () => {
  const speakers = (over: Record<string, unknown>[] = []) => ({
    meeting_id: 'm1',
    voices_available: true,
    speakers: over.length
      ? over
      : [
          { label: 'Speaker 1', segments: 2, seconds: 3.5, words: 9, named: false, has_voice: true },
          { label: 'Speaker 2', segments: 1, seconds: 2.2, words: 5, named: false, has_voice: true },
        ],
  })

  async function renderCard(routes: Record<string, any>, props: Partial<{ canWrite: boolean; hasAudio: boolean; onAnalyze: () => void; onChanged: () => void }> = {}) {
    tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
    const { default: SpeakersCard } = await import('../src/components/Speakers')
    const { AuthProvider } = await import('../src/auth/AuthContext')
    const calls = mockServer({ 'GET /auth/me': meHandler, '/api/v1/voice-profiles': () => ({ count: 0, profiles: [] }), ...routes })
    render(
      <MemoryRouter>
        <ToastProvider>
          <AuthProvider>
            <SpeakersCard
              meetingId="m1"
              canWrite={props.canWrite ?? true}
              hasAudio={props.hasAudio ?? true}
              analyzing={false}
              refreshKey={0}
              onChanged={props.onChanged ?? (() => undefined)}
              onAnalyze={props.onAnalyze ?? (() => undefined)}
            />
          </AuthProvider>
        </ToastProvider>
      </MemoryRouter>,
    )
    return calls
  }

  it('sends the label, the new name and the remember flag', async () => {
    const user = userEvent.setup()
    const onChanged = vi.fn()
    const { calls } = await renderCard(
      {
        'GET /api/v1/meetings/m1/speakers': () => speakers(),
        'PUT /api/v1/meetings/m1/speakers': () => ({ renamed: true, voice_saved: true, label: 'Krishna Panwala' }),
      },
      { onChanged },
    )
    const input = await screen.findByLabelText('Name for Speaker 1')
    expect(screen.getAllByRole('button', { name: 'Save' })[0]).toBeDisabled()
    await user.type(input, 'Krishna Panwala')
    await user.click(screen.getAllByRole('button', { name: 'Save' })[0])
    await waitFor(() => expect(onChanged).toHaveBeenCalledOnce())
    const put = calls.find((c) => c.method === 'PUT')!
    expect(put.body).toEqual({ label: 'Speaker 1', name: 'Krishna Panwala', remember_voice: true })
    expect(await screen.findByText(/voice will be recognised next time/)).toBeInTheDocument()
  })

  it('does not remember the voice when the box is unticked', async () => {
    const user = userEvent.setup()
    const { calls } = await renderCard({
      'GET /api/v1/meetings/m1/speakers': () => speakers(),
      'PUT /api/v1/meetings/m1/speakers': () => ({ renamed: true, voice_saved: false, label: 'Rahul Patel' }),
    })
    await user.type(await screen.findByLabelText('Name for Speaker 2'), 'Rahul Patel')
    await user.click(screen.getAllByRole('checkbox', { name: 'Remember this voice' })[1])
    await user.click(screen.getAllByRole('button', { name: 'Save' })[1])
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    expect(calls.find((c) => c.method === 'PUT')!.body.remember_voice).toBe(false)
  })

  it('offers voice analysis for older meetings, and read-only viewers cannot rename', async () => {
    const user = userEvent.setup()
    const onAnalyze = vi.fn()
    const legacy = speakers([{ label: 'Speaker 1', segments: 2, seconds: 3, words: 9, named: false, has_voice: false }])
    await renderCard({ 'GET /api/v1/meetings/m1/speakers': () => legacy }, { onAnalyze })
    await user.click(await screen.findByRole('button', { name: 'Analyse voices' }))
    expect(onAnalyze).toHaveBeenCalledOnce()
    expect(screen.queryByRole('checkbox', { name: 'Remember this voice' })).not.toBeInTheDocument()
  })

  it('shows nothing editable to a read-only participant', async () => {
    await renderCard({ 'GET /api/v1/meetings/m1/speakers': () => speakers() }, { canWrite: false })
    expect(await screen.findByText('Speaker 1')).toBeInTheDocument()
    expect(screen.queryByLabelText('Name for Speaker 1')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Analyse voices' })).not.toBeInTheDocument()
  })

  it('renders no card when the transcript has no speakers', async () => {
    await renderCard({ 'GET /api/v1/meetings/m1/speakers': () => ({ meeting_id: 'm1', voices_available: false, speakers: [] }) })
    await waitFor(() => expect(screen.queryByText('Who is speaking')).not.toBeInTheDocument())
  })
})

describe('saved voices in settings', () => {
  it('lists saved voices and forgets one after confirmation', async () => {
    tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
    const user = userEvent.setup()
    let profiles = [
      { id: 'p1', name: 'Krishna Panwala', samples: 2, created_at: null },
      { id: 'p2', name: 'Rahul Patel', samples: 1, created_at: null },
    ]
    const { calls } = mockServer({
      ...emptyApp,
      'GET /auth/me': meHandler,
      '/auth/sessions': () => ({ sessions: [] }),
      'GET /api/v1/voice-profiles': () => ({ count: profiles.length, profiles }),
      'DELETE /api/v1/voice-profiles/p2': () => {
        profiles = profiles.filter((p) => p.id !== 'p2')
        return undefined
      },
    })
    renderApp('/settings')
    expect(await screen.findByText('Rahul Patel')).toBeInTheDocument()
    expect(screen.getByText(/2 samples/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Forget Rahul Patel' }))
    await user.click(await screen.findByRole('button', { name: 'Forget voice' }))
    await waitFor(() => expect(screen.queryByText('Rahul Patel')).not.toBeInTheDocument())
    expect(screen.getByText('Krishna Panwala')).toBeInTheDocument()
    expect(calls.some((c) => c.method === 'DELETE' && c.url === '/api/v1/voice-profiles/p2')).toBe(true)
  })
})

describe('tasks page', () => {
  const row = (id: string, task: string, assignee: string, mine: boolean) => ({
    id, task, assignee, due_date: null, priority: 'medium', status: 'pending', created_at: null, completed_at: null,
    meeting_id: 'm1', meeting_title: 'ZOMATO', can_write: true, mine, can_update_status: mine,
  })
  const tasks = {
    count: 3, total: 3,
    action_items: [
      row('a', 'Prepare the project report', 'Rahul Patel', false),
      row('b', 'Complete the model training', 'Krishna Panwala', true),
      row('c', 'Book a room', 'Samira Khan', false),
    ],
  }

  it('lists only my tasks, and never anyone else’s, even if the server sent them', async () => {
    tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
    mockServer({ ...emptyApp, 'GET /auth/me': meHandler, '/api/v1/actions': () => tasks })
    renderApp('/tasks')
    expect(await screen.findByText('Complete the model training')).toBeInTheDocument()
    expect(screen.queryByText('Prepare the project report')).not.toBeInTheDocument()
    expect(screen.queryByText('Book a room')).not.toBeInTheDocument()
    expect(screen.queryByText(/Assigned to others/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Only mine' })).not.toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: /model training/ })).toBeEnabled()
  })

  it('says so when nothing is assigned to me', async () => {
    tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
    mockServer({
      ...emptyApp, 'GET /auth/me': meHandler,
      '/api/v1/actions': () => ({ count: 1, total: 1, action_items: [row('a', 'Prepare the project report', 'Rahul Patel', false)] }),
    })
    renderApp('/tasks')
    expect(await screen.findByText('You are all caught up')).toBeInTheDocument()
    expect(screen.queryByText('Prepare the project report')).not.toBeInTheDocument()
  })
})

describe('inviting a named speaker', () => {
  it('sends the e-mail along with the name and tells the owner who now has access', async () => {
    tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
    const user = userEvent.setup()
    const { default: SpeakersCard } = await import('../src/components/Speakers')
    const { AuthProvider } = await import('../src/auth/AuthContext')
    const { calls } = mockServer({
      'GET /auth/me': meHandler,
      '/api/v1/voice-profiles': () => ({ count: 0, profiles: [] }),
      'GET /api/v1/meetings/m1/speakers': () => ({
        meeting_id: 'm1', voices_available: true,
        speakers: [{ label: 'Speaker 2', segments: 1, seconds: 2, words: 5, named: false, has_voice: true }],
      }),
      'PUT /api/v1/meetings/m1/speakers': () => ({ renamed: true, voice_saved: true, label: 'Rahul Patel', invited: 'rahul@example.com' }),
    })
    render(
      <MemoryRouter><ToastProvider><AuthProvider>
        <SpeakersCard meetingId="m1" canWrite hasAudio analyzing={false} refreshKey={0} onChanged={() => undefined} onAnalyze={() => undefined} />
      </AuthProvider></ToastProvider></MemoryRouter>,
    )
    await user.type(await screen.findByLabelText('Name for Speaker 2'), 'Rahul Patel')
    await user.type(screen.getByLabelText('E-mail for Speaker 2'), 'rahul@example.com')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText(/rahul@example.com can now sign in/)).toBeInTheDocument()
    expect(calls.find((c) => c.method === 'PUT')!.body).toEqual({
      label: 'Speaker 2', name: 'Rahul Patel', remember_voice: true, email: 'rahul@example.com',
    })
  })
})

describe('meetings that have finished', () => {
  const now = new Date(2026, 10, 13, 15, 0) // 13 Nov 2026, 3 pm
  const ev = (id: string, summary: string, start: string, end: string) => ({
    id, summary, start, end, status: 'scheduled', type: 'meeting' as const, meeting_id: id, calendar_id: 'internal',
  })

  it('upcomingOnly drops finished meetings and keeps in-progress and later ones', async () => {
    const { upcomingOnly } = await import('../src/components/domain')
    const list = [
      ev('a', 'research paper', '2026-11-13T10:00:00', '2026-11-13T11:00:00'),   // ended this morning
      ev('b', 'running now', '2026-11-13T14:30:00', '2026-11-13T15:30:00'),
      ev('c', 'later today', '2026-11-13T17:00:00', '2026-11-13T18:00:00'),
      ev('d', 'ends right now', '2026-11-13T14:00:00', '2026-11-13T15:00:00'),
    ]
    expect(upcomingOnly(list, now).map((e) => e.id)).toEqual(['b', 'c'])
  })

  it('hasEnded only applies to scheduled meetings whose end time has passed', async () => {
    const { hasEnded } = await import('../src/components/domain')
    const m = { status: 'scheduled', scheduled_time: '2026-11-13T10:00:00', end_time: '2026-11-13T11:00:00', duration_minutes: 60 }
    expect(hasEnded(m, now)).toBe(true)
    expect(hasEnded({ ...m, end_time: null }, now)).toBe(true)                       // falls back to the duration
    expect(hasEnded({ ...m, scheduled_time: '2026-11-13T14:30:00', end_time: '2026-11-13T15:30:00' }, now)).toBe(false)
    expect(hasEnded({ ...m, status: 'cancelled' }, now)).toBe(false)
    expect(hasEnded({ ...m, status: 'completed' }, now)).toBe(false)
    expect(hasEnded({ ...m, scheduled_time: null, end_time: null }, now)).toBe(false)
  })

  it('the dashboard no longer lists a meeting that finished earlier today', async () => {
    vi.useFakeTimers({ toFake: ['Date'], now })
    try {
      tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
      mockServer({
        ...emptyApp,
        'GET /auth/me': meHandler,
        '/api/v1/calendar/anna%40example.com/events': () => ({
          user_email: me.email, calendar_id: 'internal', count: 2, total: 2,
          events: [
            ev('a', 'research paper', '2026-11-13T10:00:00', '2026-11-13T11:00:00'),
            ev('c', 'later today', '2026-11-13T17:00:00', '2026-11-13T18:00:00'),
          ],
        }),
      })
      renderApp('/')
      expect(await screen.findByText('later today')).toBeInTheDocument()
      expect(screen.queryByText('research paper')).not.toBeInTheDocument()
      const stat = screen.getByText('Upcoming (7 days)').parentElement!
      expect(within(stat).getByText('1')).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('deleting from the meetings list', () => {
  const meeting = (id: string, title: string, can_write: boolean) => ({
    id, title, status: 'uploaded', scheduled_time: null, end_time: null, duration_minutes: 1, participants: [],
    owner_email: 'x@y.com', started_at: null, ended_at: null, has_audio: true, created_at: '2026-11-13T12:00:00',
    can_write, has_transcript: true, has_summary: true, action_count: 0, open_action_count: 0,
  })
  const setup = (rows: ReturnType<typeof meeting>[]) => {
    tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
    let current = rows
    return mockServer({
      ...emptyApp,
      'GET /auth/me': meHandler,
      'GET /api/v1/meetings': () => ({ count: current.length, total: current.length, meetings: current }),
      ...Object.fromEntries(
        rows.map((r) => [
          `DELETE /api/v1/meetings/${r.id}`,
          () => {
            current = current.filter((x) => x.id !== r.id)
            return undefined
          },
        ]),
      ),
    })
  }

  it('offers delete only on meetings the user may delete', async () => {
    setup([meeting('a', 'Mine', true), meeting('b', 'Shared with me', false)])
    renderApp('/meetings')
    await screen.findByText('Mine')
    expect(screen.getByRole('button', { name: 'Delete Mine' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Delete Shared with me' })).not.toBeInTheDocument()
  })

  it('asks first, and cancelling deletes nothing', async () => {
    const user = userEvent.setup()
    const { calls } = setup([meeting('a', 'Mine', true)])
    renderApp('/meetings')
    await user.click(await screen.findByRole('button', { name: 'Delete Mine' }))
    expect(await screen.findByText('Delete this meeting permanently?')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByText('Delete this meeting permanently?')).not.toBeInTheDocument()
    expect(calls.some((c) => c.method === 'DELETE')).toBe(false)
    expect(screen.getByText('Mine')).toBeInTheDocument()
  })

  it('deletes after confirming and removes the row without opening the meeting', async () => {
    const user = userEvent.setup()
    const { calls } = setup([meeting('a', 'Mine', true), meeting('b', 'Keep me', true)])
    renderApp('/meetings')
    await user.click(await screen.findByRole('button', { name: 'Delete Mine' }))
    await user.click(await screen.findByRole('button', { name: 'Delete forever' }))
    await waitFor(() => expect(screen.queryByText('Mine')).not.toBeInTheDocument())
    expect(screen.getByText('Keep me')).toBeInTheDocument()
    expect(calls.filter((c) => c.method === 'DELETE').map((c) => c.url)).toEqual(['/api/v1/meetings/a'])
    expect(screen.getByRole('heading', { level: 1, name: 'Meetings' })).toBeInTheDocument()   // still on the list
  })

  it('shows the error and keeps the row when the server refuses', async () => {
    const user = userEvent.setup()
    tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
    mockServer({
      ...emptyApp,
      'GET /auth/me': meHandler,
      'GET /api/v1/meetings': () => ({ count: 1, total: 1, meetings: [meeting('a', 'Mine', true)] }),
      'DELETE /api/v1/meetings/a': () => jsonResponse({ detail: 'Only the meeting owner can modify this meeting' }, 403),
    })
    renderApp('/meetings')
    await user.click(await screen.findByRole('button', { name: 'Delete Mine' }))
    await user.click(await screen.findByRole('button', { name: 'Delete forever' }))
    expect(await screen.findByText('Only the meeting owner can modify this meeting')).toBeInTheDocument()
    expect(screen.getByText('Mine')).toBeInTheDocument()
  })
})

describe('sorting open tasks', () => {
  const row = (id: string, task: string, priority: string, due: string | null, assignee = 'Krishna Panwala') => ({
    id, task, assignee, due_date: due, priority, status: 'pending', created_at: null, completed_at: null,
    meeting_id: 'm1', meeting_title: 'ZOMATO', can_write: true, mine: true, can_update_status: true,
  })
  const tasks = {
    count: 3, total: 3,
    action_items: [
      row('a', 'Zebra task', 'low', '2026-11-16T00:00:00'),
      row('b', 'Alpha task', 'high', '2026-11-20T00:00:00'),
      row('c', 'Middle task', 'medium', '2026-11-18T00:00:00'),
    ],
  }
  const order = () => screen.getAllByRole('checkbox').map((c) => (c.getAttribute('aria-label') ?? '').replace(/^Complete: /, ''))

  it('the Tasks page re-orders by name and by priority, and filters by priority', async () => {
    tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
    const user = userEvent.setup()
    mockServer({ ...emptyApp, 'GET /auth/me': meHandler, '/api/v1/actions': () => tasks })
    renderApp('/tasks')
    await screen.findByText('Zebra task')
    await user.selectOptions(screen.getByLabelText('Sort by'), 'name')
    expect(order()).toEqual(['Alpha task', 'Middle task', 'Zebra task'])
    await user.selectOptions(screen.getByLabelText('Sort by'), 'priority')
    expect(order()).toEqual(['Alpha task', 'Middle task', 'Zebra task'])
    await user.selectOptions(screen.getByLabelText('Sort by'), 'due')
    expect(order()).toEqual(['Zebra task', 'Middle task', 'Alpha task'])
    await user.selectOptions(screen.getByLabelText('Priority'), 'high')
    expect(order()).toEqual(['Alpha task'])
  })

  it('the dashboard card sorts and filters too', async () => {
    tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
    const user = userEvent.setup()
    mockServer({ ...emptyApp, 'GET /auth/me': meHandler, '/api/v1/actions': () => tasks })
    renderApp('/')
    await screen.findByText('Zebra task')
    await user.selectOptions(screen.getByLabelText('Sort tasks by'), 'name')
    expect(order()).toEqual(['Alpha task', 'Middle task', 'Zebra task'])
    await user.selectOptions(screen.getByLabelText('Filter tasks by priority'), 'medium')
    expect(order()).toEqual(['Middle task'])
    await user.selectOptions(screen.getByLabelText('Filter tasks by priority'), 'all')
    await user.selectOptions(screen.getByLabelText('Sort tasks by'), 'due')
    expect(order()).toEqual(['Zebra task', 'Middle task', 'Alpha task'])
  })
})

describe('back button', () => {
  it('is on every page except the dashboard', async () => {
    tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
    mockServer({ ...emptyApp, 'GET /auth/me': meHandler, '/auth/sessions': () => ({ sessions: [] }), '/api/v1/voice-profiles': () => ({ count: 0, profiles: [] }) })
    for (const path of ['/meetings', '/tasks', '/calendar', '/settings', '/meetings/new', '/nowhere']) {
      const { unmount } = renderApp(path)
      expect(await screen.findByRole('button', { name: /Back/ })).toBeInTheDocument()
      unmount()
    }
    renderApp('/')
    await screen.findByText(/Good (morning|afternoon|evening)/)
    expect(screen.queryByRole('button', { name: /Back/ })).not.toBeInTheDocument()
  })

  it('goes to the parent page when there is nothing earlier to return to', async () => {
    tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
    const user = userEvent.setup()
    mockServer({ ...emptyApp, 'GET /auth/me': meHandler, '/api/v1/meetings/zz': () => jsonResponse({ detail: 'Meeting not found' }, 404) })
    renderApp('/meetings/zz')
    await user.click(await screen.findByRole('button', { name: /Back/ }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Meetings' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /Back/ }))
    expect(await screen.findByText(/Good (morning|afternoon|evening)/)).toBeInTheDocument()
  })

  it('is on the sign-up and password pages, returning to sign in', async () => {
    const user = userEvent.setup()
    mockServer({})
    renderApp('/register')
    await user.click(await screen.findByRole('button', { name: /Back/ }))
    expect(await screen.findByText('Welcome back')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Back/ })).not.toBeInTheDocument()
  })
})

describe('what a shared-with user sees', () => {
  const row = (id: string, task: string, assignee: string, mine: boolean) => ({
    id, task, assignee, due_date: '2026-11-16T00:00:00', priority: 'medium', status: 'pending', created_at: null, completed_at: null,
    meeting_id: 'm1', meeting_title: 'ZOMATO', can_write: false, mine, can_update_status: mine,
  })
  const meeting = (over: Record<string, unknown> = {}) => ({
    id: 'm1', title: 'ZOMATO PROJECT', status: 'uploaded', scheduled_time: null, end_time: null, duration_minutes: 1, participants: [],
    owner_email: 'k@example.com', owner_name: 'Krishna Panwala', is_owner: false, started_at: null, ended_at: null, has_audio: true,
    created_at: '2026-11-13T12:00:00', can_write: false, has_transcript: true, has_summary: true, action_count: 2, open_action_count: 2, ...over,
  })

  function server() {
    tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
    return mockServer({
      ...emptyApp,
      'GET /auth/me': meHandler,
      '/api/v1/meetings': () => ({ count: 1, total: 1, meetings: [meeting()] }),
      '/api/v1/actions': ({ url }) => {
        const all = [row('a', 'Complete the model training', 'Krishna Panwala', false), row('b', 'Prepare the project report', 'Anna Larsen', true)]
        if (url.includes('mine=true')) {
          const mineRows = all.filter((t) => t.mine)
          return { count: mineRows.length, total: url.includes('due_to') ? 0 : mineRows.length, action_items: mineRows }
        }
        return { count: 2, total: 2, action_items: all }
      },
    })
  }

  it('the dashboard counts what is assigned to you, puts it first, and labels shared meetings', async () => {
    const { calls } = server()
    renderApp('/')
    expect(await screen.findByText('Assigned to you')).toBeInTheDocument()
    const stat = screen.getByText('Assigned to you').parentElement!
    expect(within(stat).getByText('1')).toBeInTheDocument()
    expect(within(stat).getByText('open tasks')).toBeInTheDocument()
    const order = screen.getAllByRole('checkbox').map((c) => c.getAttribute('aria-label') ?? '')
    expect(order).toHaveLength(1)                                       // only my own task is listed
    expect(order[0]).toContain('Prepare the project report')
    expect(screen.getByText('Shared by Krishna Panwala')).toBeInTheDocument()
    expect(calls.some((c) => c.url.includes('mine=true') && c.url.includes('due_to'))).toBe(true)
    expect(screen.getByText('your tasks are on track')).toBeInTheDocument()
  })

  it('does not label your own meetings as shared', async () => {
    tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
    mockServer({
      ...emptyApp,
      'GET /auth/me': meHandler,
      '/api/v1/meetings': () => ({ count: 1, total: 1, meetings: [meeting({ is_owner: true, can_write: true })] }),
    })
    renderApp('/meetings')
    await screen.findByText('ZOMATO PROJECT')
    expect(screen.queryByText(/Shared by/)).not.toBeInTheDocument()
  })

  it('falls back to the owner’s e-mail when they have no name', async () => {
    tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
    mockServer({
      ...emptyApp,
      'GET /auth/me': meHandler,
      '/api/v1/meetings': () => ({ count: 1, total: 1, meetings: [meeting({ owner_name: null })] }),
    })
    renderApp('/meetings')
    expect(await screen.findByText('Shared by k@example.com')).toBeInTheDocument()
  })
})

describe('e-mail saved with a voice', () => {
  it('settings shows it and saves a new one, or clears it', async () => {
    tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
    const user = userEvent.setup()
    let profiles: Record<string, unknown>[] = [
      { id: 'p1', name: 'Rahul Patel', samples: 1, created_at: null, email: null },
      { id: 'p2', name: 'Krishna Panwala', samples: 2, created_at: null, email: 'k@example.com' },
    ]
    const { calls } = mockServer({
      ...emptyApp,
      'GET /auth/me': meHandler,
      '/auth/sessions': () => ({ sessions: [] }),
      'GET /api/v1/voice-profiles': () => ({ count: profiles.length, profiles }),
      'PATCH /api/v1/voice-profiles/p1': ({ body }) => {
        profiles = profiles.map((p) => (p.id === 'p1' ? { ...p, email: body.email } : p))
        return profiles[0]
      },
      'PATCH /api/v1/voice-profiles/p2': ({ body }) => {
        profiles = profiles.map((p) => (p.id === 'p2' ? { ...p, email: body.email } : p))
        return profiles[1]
      },
    })
    renderApp('/settings')
    const rahul = await screen.findByLabelText('E-mail for Rahul Patel')
    expect(screen.getByLabelText('E-mail for Krishna Panwala')).toHaveValue('k@example.com')
    const saveButtons = () => screen.getAllByRole('button', { name: 'Save' })
    expect(saveButtons()[0]).toBeDisabled()                                   // nothing changed yet
    await user.type(rahul, 'rahul@example.com')
    await user.click(saveButtons()[0])
    expect(await screen.findByText(/will be shared with rahul@example.com/)).toBeInTheDocument()
    expect(calls.find((c) => c.method === 'PATCH' && c.url.endsWith('/p1'))!.body).toEqual({ email: 'rahul@example.com' })

    await user.clear(screen.getByLabelText('E-mail for Krishna Panwala'))
    await user.click(saveButtons()[1])
    await waitFor(() => expect(calls.find((c) => c.url.endsWith('/p2') && c.method === 'PATCH')!.body).toEqual({ email: null }))
  })

  it('naming a speaker shows the e-mail already saved for that person', async () => {
    tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
    const user = userEvent.setup()
    const { default: SpeakersCard } = await import('../src/components/Speakers')
    const { AuthProvider } = await import('../src/auth/AuthContext')
    mockServer({
      'GET /auth/me': meHandler,
      '/api/v1/voice-profiles': () => ({ count: 1, profiles: [{ id: 'p1', name: 'Rahul Patel', samples: 1, created_at: null, email: 'rahul@example.com' }] }),
      'GET /api/v1/meetings/m1/speakers': () => ({
        meeting_id: 'm1', voices_available: true,
        speakers: [{ label: 'Speaker 2', segments: 1, seconds: 2, words: 5, named: false, has_voice: true }],
      }),
    })
    render(
      <MemoryRouter><ToastProvider><AuthProvider>
        <SpeakersCard meetingId="m1" canWrite hasAudio analyzing={false} refreshKey={0} onChanged={() => undefined} onAnalyze={() => undefined} />
      </AuthProvider></ToastProvider></MemoryRouter>,
    )
    const email = await screen.findByLabelText('E-mail for Speaker 2')
    expect(email).toHaveAttribute('placeholder', 'E-mail (optional)')
    await user.type(screen.getByLabelText('Name for Speaker 2'), 'rahul patel')
    expect(email).toHaveAttribute('placeholder', 'Saved: rahul@example.com')
  })
})
