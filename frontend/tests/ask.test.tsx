import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { onSessionLost, tokens } from '../src/api/client'
import { AnswerText } from '../src/pages/AskPage'
import { emptyApp, jsonResponse, meHandler, mockServer, pair, renderApp } from './helpers'

beforeEach(() => {
  tokens.clear()
  onSessionLost(null)
  Element.prototype.scrollIntoView = vi.fn()
  tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
})
afterEach(() => {
  vi.unstubAllGlobals()
})

const meeting = (id: string, title: string) => ({
  id, title, status: 'uploaded', scheduled_time: null, end_time: null, duration_minutes: 1, participants: [], owner_email: 'x@y.com',
  owner_name: null, is_owner: true, started_at: null, ended_at: null, has_audio: false, created_at: '2026-11-13T12:00:00', can_write: true,
  has_transcript: true, has_summary: true, action_count: 0, open_action_count: 0,
})

const zomato = { tag: 'M1', meeting_id: 'm1', title: 'ZOMATO PROJECT', date: '2026-11-13', cited: true }

function setup(reply: (body: any) => unknown, saved: unknown[] = []) {
  return mockServer({
    ...emptyApp,
    'GET /auth/me': meHandler,
    'GET /api/v1/ask/history': () => ({ turns: saved }),
    'DELETE /api/v1/ask/history': () => undefined,
    '/api/v1/meetings': () => ({ count: 2, total: 2, meetings: [meeting('m1', 'ZOMATO PROJECT'), meeting('m2', 'SWIGGY')] }),
    'POST /api/v1/ask': ({ body }) => reply(body),
  })
}

describe('AnswerText', () => {
  it('turns citations into links to the meeting and drops invented ones', () => {
    render(
      <MemoryRouter>
        <p data-testid="t">
          <AnswerText text="Rahul writes it [M1]. Also [M9] and [M1]." sources={[zomato]} />
        </p>
      </MemoryRouter>,
    )
    const links = screen.getAllByRole('link', { name: 'ZOMATO PROJECT' })
    expect(links).toHaveLength(2)
    expect(links[0]).toHaveAttribute('href', '/meetings/m1')
    expect(screen.getByTestId('t').textContent).not.toContain('M9')
    expect(screen.getByTestId('t').textContent).toContain('Rahul writes it')
  })

  it('shows plain text untouched when there are no citations', () => {
    render(<MemoryRouter><p data-testid="t"><AnswerText text="I could not find it." sources={[]} /></p></MemoryRouter>)
    expect(screen.getByTestId('t')).toHaveTextContent('I could not find it.')
  })
})

describe('the Ask page', () => {
  it('is in the menu and offers starter questions', async () => {
    setup(() => ({}))
    renderApp('/ask')
    expect(await screen.findByRole('heading', { level: 1, name: 'Ask your meetings' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Ask' })).toHaveAttribute('href', '/ask')
    expect(await screen.findByRole('button', { name: 'What are my open tasks?' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Ask' })).toBeDisabled() // nothing typed yet
  })

  it('answers a typed question, with the citation as a link and the sources listed', async () => {
    const user = userEvent.setup()
    const { calls } = setup(() => ({ answer: 'Rahul Patel will prepare the report by Monday [M1].', sources: [zomato], searched: 2 }))
    renderApp('/ask')
    const box = await screen.findByLabelText('Your question')
    await user.type(box, 'What did Rahul commit to?{Enter}')
    expect(await screen.findByText(/Rahul Patel will prepare the report by Monday/)).toBeInTheDocument()
    expect(screen.getByText('What did Rahul commit to?')).toBeInTheDocument()
    const bubble = screen.getByText(/Rahul Patel will prepare/).closest('.bubble') as HTMLElement
    expect(within(bubble).getAllByRole('link', { name: 'ZOMATO PROJECT' }).length).toBeGreaterThanOrEqual(2) // inline cite + Sources
    expect(within(bubble).getByText(/Sources:/)).toBeInTheDocument()
    expect(calls.find((c) => c.url === '/api/v1/ask')!.body).toEqual({ question: 'What did Rahul commit to?', history: [] })
    expect(box).toHaveValue('')
  })

  it('a starter question is sent as it is', async () => {
    const user = userEvent.setup()
    const { calls } = setup(() => ({ answer: 'You have 2 open tasks.', sources: [], searched: 2 }))
    renderApp('/ask')
    await user.click(await screen.findByRole('button', { name: 'What are my open tasks?' }))
    expect(await screen.findByText('You have 2 open tasks.')).toBeInTheDocument()
    expect(calls.find((c) => c.url === '/api/v1/ask')!.body.question).toBe('What are my open tasks?')
    expect(screen.queryByRole('button', { name: 'What are my open tasks?' })).not.toBeInTheDocument() // starters gone
  })

  it('follow-up questions carry the last three exchanges', async () => {
    const user = userEvent.setup()
    let n = 0
    const { calls } = setup(() => ({ answer: `answer ${++n}`, sources: [], searched: 1 }))
    renderApp('/ask')
    const box = await screen.findByLabelText('Your question')
    for (const q of ['first question', 'second question', 'third question', 'fourth question']) {
      await user.type(box, `${q}{Enter}`)
      await screen.findByText(`answer ${n}`)
    }
    const bodies = calls.filter((c) => c.url === '/api/v1/ask').map((c) => c.body)
    expect(bodies[0].history).toEqual([])
    expect(bodies[1].history).toEqual([{ question: 'first question', answer: 'answer 1' }])
    expect(bodies[3].history.map((h: { question: string }) => h.question)).toEqual(['first question', 'second question', 'third question'])
  })

  it('says when nothing was cited and lists what was searched', async () => {
    const user = userEvent.setup()
    setup(() => ({ answer: 'I could not find that in the meetings.', sources: [{ ...zomato, cited: false }], searched: 1 }))
    renderApp('/ask')
    await user.type(await screen.findByLabelText('Your question'), 'anything about salaries?{Enter}')
    expect(await screen.findByText(/No source was cited. Searched:/)).toBeInTheDocument()
  })

  it('needs at least three characters', async () => {
    const user = userEvent.setup()
    setup(() => ({ answer: 'x', sources: [], searched: 1 }))
    renderApp('/ask')
    const box = await screen.findByLabelText('Your question')
    await user.type(box, 'ab')
    expect(screen.getByRole('button', { name: 'Ask' })).toBeDisabled()
    await user.type(box, 'c')
    expect(screen.getByRole('button', { name: 'Ask' })).toBeEnabled()
  })

  it('shows an unavailable AI service as a message with a retry that replaces it', async () => {
    const user = userEvent.setup()
    let fail = true
    const { calls } = setup(() => (fail ? jsonResponse({ detail: 'The Ollama service is unavailable or failed to respond' }, 503) : { answer: 'Back now.', sources: [], searched: 1 }))
    renderApp('/ask')
    await user.type(await screen.findByLabelText('Your question'), 'What happened?{Enter}')
    expect(await screen.findByRole('alert')).toHaveTextContent('The Ollama service is unavailable')
    fail = false
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByText('Back now.')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getAllByText('What happened?')).toHaveLength(1) // replaced, not duplicated
    expect(calls.filter((c) => c.url === '/api/v1/ask')).toHaveLength(2)
  })

  it('explains the hourly limit in plain words', async () => {
    const user = userEvent.setup()
    setup(() => jsonResponse({ detail: 'Rate limit exceeded' }, 429))
    renderApp('/ask')
    await user.type(await screen.findByLabelText('Your question'), 'again please{Enter}')
    expect(await screen.findByRole('alert')).toHaveTextContent(/asked a lot of questions this hour/)
  })

  it('can be stopped while the model is thinking', async () => {
    const user = userEvent.setup()
    const aborted = vi.fn()
    mockServer({ ...emptyApp, 'GET /auth/me': meHandler, 'GET /api/v1/ask/history': () => ({ turns: [] }), '/api/v1/meetings': () => ({ count: 0, total: 0, meetings: [] }) })
    const base = globalThis.fetch as unknown as (u: string, i?: RequestInit) => Promise<Response>
    vi.stubGlobal('fetch', (url: string, init?: RequestInit) =>
      url === '/api/v1/ask'
        ? new Promise<Response>((_res, rej) => {
            init?.signal?.addEventListener('abort', () => {
              aborted()
              rej(new DOMException('aborted', 'AbortError'))
            })
          })
        : base(url, init),
    )
    renderApp('/ask')
    await user.type(await screen.findByLabelText('Your question'), 'slow question{Enter}')
    expect(await screen.findByText(/Thinking/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(aborted).toHaveBeenCalled())
    await waitFor(() => expect(screen.queryByText('slow question')).not.toBeInTheDocument())
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Your question')).toBeEnabled()
  })

  it('"Clear" deletes the saved conversation, after asking', async () => {
    const user = userEvent.setup()
    setup(() => ({ answer: 'Some answer.', sources: [], searched: 1 }))
    renderApp('/ask')
    await user.type(await screen.findByLabelText('Your question'), 'a question{Enter}')
    await screen.findByText('Some answer.')
    await user.click(screen.getByRole('button', { name: 'Clear' }))
    await user.click(await screen.findByRole('button', { name: 'Delete conversation' }))
    await waitFor(() => expect(screen.queryByText('Some answer.')).not.toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'What are my open tasks?' })).toBeInTheDocument()
  })
})

describe('asking about one meeting', () => {
  it('limits the search to the meeting from the link, with its own starter questions', async () => {
    const user = userEvent.setup()
    const { calls } = setup(() => ({ answer: 'A summary.', sources: [zomato], searched: 1 }))
    renderApp('/ask?meeting=m1')
    expect(await screen.findByText('Ask about “ZOMATO PROJECT”')).toBeInTheDocument()
    expect((screen.getByLabelText('Search in') as HTMLSelectElement).value).toBe('m1')
    await user.click(screen.getByRole('button', { name: 'Summarise this meeting' }))
    await screen.findByText('A summary.')
    expect(calls.find((c) => c.url === '/api/v1/ask')!.body).toMatchObject({ question: 'Summarise this meeting', meeting_id: 'm1' })
  })

  it('switching the scope starts a fresh conversation', async () => {
    const user = userEvent.setup()
    const { calls } = setup(() => ({ answer: 'An answer.', sources: [], searched: 1 }))
    renderApp('/ask')
    await user.type(await screen.findByLabelText('Your question'), 'first question{Enter}')
    await screen.findByText('An answer.')
    await screen.findByRole('option', { name: 'SWIGGY' })
    await user.selectOptions(screen.getByLabelText('Search in'), 'm2')
    expect(screen.queryByText('An answer.')).not.toBeInTheDocument()
    await user.type(screen.getByLabelText('Your question'), 'second question{Enter}')
    await screen.findByText('An answer.')
    const last = calls.filter((c) => c.url === '/api/v1/ask').pop()!.body
    expect(last).toMatchObject({ question: 'second question', meeting_id: 'm2', history: [] })
  })

  it('the meeting page links here', async () => {
    mockServer({
      ...emptyApp,
      'GET /auth/me': meHandler,
      '/api/v1/meetings/m1': () => meeting('m1', 'ZOMATO PROJECT'),
      '/api/v1/meetings/m1/jobs/latest': () => jsonResponse({ detail: 'none' }, 404),
      '/api/v1/meetings/m1/transcript': () => ({ meeting_id: 'm1', transcript_id: 't', language: 'en', transcript: '', word_count: 1, speaker_count: 1, created_at: null, segments: [] }),
      '/api/v1/meetings/m1/summary': () => ({ meeting_id: 'm1', executive_summary: 'x', key_points: [], decisions: [], next_steps: [], key_quotes: [], created_at: null }),
      '/api/v1/meetings/m1/actions': () => ({ meeting_id: 'm1', action_items: [], count: 0 }),
    })
    renderApp('/meetings/m1')
    expect(await screen.findByRole('link', { name: /Ask about this/ })).toHaveAttribute('href', '/ask?meeting=m1')
  })
})

describe('the saved conversation', () => {
  const savedTurn = (n: number, over: Record<string, unknown> = {}) => ({
    id: `t${n}`, question: `saved question ${n}`, answer: `saved answer ${n} [M1]`, sources: [zomato], searched: 2,
    created_at: '2026-11-13T12:00:00', ...over,
  })

  it('is there when you come back, with its links, and is not mixed up with the starters', async () => {
    setup(() => ({}), [savedTurn(1), savedTurn(2)])
    renderApp('/ask')
    expect(await screen.findByText('saved question 1')).toBeInTheDocument()
    expect(screen.getByText(/saved answer 2/)).toBeInTheDocument()
    expect(screen.getAllByRole('link', { name: 'ZOMATO PROJECT' }).length).toBeGreaterThanOrEqual(2)
    expect(screen.queryByRole('button', { name: 'What are my open tasks?' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Clear' })).toBeInTheDocument()
  })

  it('asks for the conversation of the chosen meeting only', async () => {
    const { calls } = setup(() => ({}), [savedTurn(1)])
    renderApp('/ask?meeting=m1')
    await screen.findByText('saved question 1')
    expect(calls.find((c) => c.url.startsWith('/api/v1/ask/history'))!.url).toBe('/api/v1/ask/history?meeting_id=m1')
  })

  it('a new answer continues the saved conversation and sends it as context', async () => {
    const user = userEvent.setup()
    const { calls } = setup(() => ({ id: 'new1', created_at: '2026-11-13T13:00:00', answer: 'A fresh answer.', sources: [], searched: 1 }), [savedTurn(1)])
    renderApp('/ask')
    await screen.findByText('saved question 1')
    await user.type(screen.getByLabelText('Your question'), 'a follow up{Enter}')
    await screen.findByText('A fresh answer.')
    expect(screen.getByText('saved question 1')).toBeInTheDocument()
    expect(calls.find((c) => c.url === '/api/v1/ask')!.body.history).toEqual([{ question: 'saved question 1', answer: 'saved answer 1 [M1]' }])
  })

  it('deleting asks first; cancelling keeps everything; confirming deletes this scope only', async () => {
    const user = userEvent.setup()
    const { calls } = setup(() => ({}), [savedTurn(1)])
    renderApp('/ask?meeting=m1')
    await screen.findByText('saved question 1')
    await user.click(screen.getByRole('button', { name: 'Clear' }))
    expect(await screen.findByText('Delete this conversation?')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(calls.some((c) => c.method === 'DELETE')).toBe(false)
    expect(screen.getByText('saved question 1')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Clear' }))
    await user.click(await screen.findByRole('button', { name: 'Delete conversation' }))
    await waitFor(() => expect(screen.queryByText('saved question 1')).not.toBeInTheDocument())
    expect(calls.find((c) => c.method === 'DELETE')!.url).toBe('/api/v1/ask/history?meeting_id=m1')
    expect(await screen.findByText('Conversation deleted.')).toBeInTheDocument()
  })

  it('shows why the conversation could not be loaded, and still lets you ask', async () => {
    const user = userEvent.setup()
    mockServer({
      ...emptyApp,
      'GET /auth/me': meHandler,
      '/api/v1/meetings': () => ({ count: 0, total: 0, meetings: [] }),
      'GET /api/v1/ask/history': () => jsonResponse({ detail: 'Meeting not found' }, 404),
      'POST /api/v1/ask': () => ({ answer: 'Still works.', sources: [], searched: 1 }),
    })
    renderApp('/ask')
    expect(await screen.findByRole('alert')).toHaveTextContent('Meeting not found')
    await user.type(screen.getByLabelText('Your question'), 'can I still ask?{Enter}')
    expect(await screen.findByText('Still works.')).toBeInTheDocument()
  })
})
