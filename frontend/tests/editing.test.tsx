import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { onSessionLost, tokens } from '../src/api/client'
import { ToastProvider } from '../src/components/ui'
import { countMatches } from '../src/lib/text'
import { emptyApp, jsonResponse, meHandler, mockServer, pair, renderApp } from './helpers'

beforeEach(() => {
  tokens.clear()
  onSessionLost(null)
})
afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const signIn = () => tokens.set(pair(1) as Parameters<typeof tokens.set>[0])

describe('countMatches', () => {
  const texts = ['My name is Megapanvala and I will test.', 'Thanks Megapanvalas team, megapanvala!']
  it('counts whole words, ignoring case by default', () => {
    expect(countMatches(texts, 'Megapanvala', false, true)).toBe(2)
    expect(countMatches(texts, 'megapanvala', true, true)).toBe(1)
  })
  it('counts inside words when whole-word is off', () => expect(countMatches(texts, 'Megapanvala', false, false)).toBe(3))
  it('treats the text literally, not as a pattern', () => {
    expect(countMatches(['C++ is fun. c++ too', 'a.b a-b'], 'C++', false, false)).toBe(2)
    expect(countMatches(['a.b a-b'], 'a.b', false, true)).toBe(1)
    expect(countMatches(['x'], '(', false, false)).toBe(0)
  })
  it('collapses extra spaces in what you type, and ignores empty input', () => {
    expect(countMatches(['Megha Panwala'], '  Megha    Panwala ', false, true)).toBe(1)
    expect(countMatches(['anything'], '   ', false, true)).toBe(0)
  })
})

describe('a name said in the recording is filled in', () => {
  const speaker = (over: Record<string, unknown>) => ({
    label: 'Speaker 1', segments: 1, seconds: 6, words: 13, named: false, has_voice: true,
    suggestion: { name: 'Megha Panwala', heard: 'Megapanvala', quote: 'My name is Megapanvala and I will do the validation and testing part.', known: true },
    ...over,
  })

  async function card(speakers: unknown[], extra: Record<string, unknown> = {}) {
    signIn()
    const { default: SpeakersCard } = await import('../src/components/Speakers')
    const { AuthProvider } = await import('../src/auth/AuthContext')
    const server = mockServer({
      'GET /auth/me': meHandler,
      '/api/v1/voice-profiles': () => ({ count: 0, profiles: [] }),
      'GET /api/v1/meetings/m1/speakers': () => ({ meeting_id: 'm1', voices_available: true, speakers }),
      'PUT /api/v1/meetings/m1/speakers': () => ({ renamed: true, voice_saved: true, label: 'Megha Panwala', text_fixed: 6 }),
      ...extra,
    })
    render(
      <MemoryRouter><ToastProvider><AuthProvider>
        <SpeakersCard meetingId="m1" canWrite hasAudio analyzing={false} refreshKey={0} onChanged={() => undefined} onAnalyze={() => undefined} />
      </AuthProvider></ToastProvider></MemoryRouter>,
    )
    return server
  }

  it('pre-fills the suggested name, shows what was heard, and offers to fix the spelling', async () => {
    await card([speaker({})])
    const input = await screen.findByLabelText('Name for Speaker 1')
    expect(input).toHaveValue('Megha Panwala')
    expect(screen.getByText(/Heard: “My name is Megapanvala/)).toBeInTheDocument()
    expect(screen.getByText(/matches Megha Panwala/)).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: /Also change “Megapanvala” to “Megha Panwala”/ })).toBeChecked()
    expect(screen.getAllByRole('button', { name: 'Save' })[0]).toBeEnabled()
  })

  it('saves the corrected name and fixes the spelling everywhere in one go', async () => {
    const user = userEvent.setup()
    const { calls } = await card([speaker({})])
    await screen.findByLabelText('Name for Speaker 1')
    await user.click(screen.getAllByRole('button', { name: 'Save' })[0])
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    expect(calls.find((c) => c.method === 'PUT')!.body).toEqual({
      label: 'Speaker 1', name: 'Megha Panwala', remember_voice: true, replace_heard: 'Megapanvala',
    })
    expect(await screen.findByText(/Fixed “Megapanvala” in 6 places/)).toBeInTheDocument()
  })

  it('lets you edit the suggestion, and untick the spelling fix', async () => {
    const user = userEvent.setup()
    const { calls } = await card([speaker({})])
    const input = await screen.findByLabelText('Name for Speaker 1')
    await user.clear(input)
    await user.type(input, 'Megha P')
    await user.click(screen.getByRole('checkbox', { name: /Also change/ }))
    await user.click(screen.getAllByRole('button', { name: 'Save' })[0])
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    expect(calls.find((c) => c.method === 'PUT')!.body).toEqual({ label: 'Speaker 1', name: 'Megha P', remember_voice: true })
  })

  it('does not offer a spelling fix when you keep the spelling that was heard', async () => {
    const user = userEvent.setup()
    await card([speaker({ suggestion: { name: 'Megapanvala', heard: 'Megapanvala', quote: 'My name is Megapanvala.', known: false } })])
    expect(await screen.findByLabelText('Name for Speaker 1')).toHaveValue('Megapanvala')
    expect(screen.getByText(/check the spelling/)).toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: /Also change/ })).not.toBeInTheDocument()
    const input = screen.getByLabelText('Name for Speaker 1')
    await user.clear(input)
    await user.type(input, 'Megha Panwala')
    expect(screen.getByRole('checkbox', { name: /Also change “Megapanvala” to “Megha Panwala”/ })).toBeInTheDocument()
  })

  it('leaves the box empty when nobody introduced themselves, and for speakers already named', async () => {
    await card([speaker({ suggestion: null }), speaker({ label: 'Rahul Patel', named: true, suggestion: null })])
    expect(await screen.findByLabelText('Name for Speaker 1')).toHaveValue('')
    expect(screen.getByLabelText('Name for Rahul Patel')).toHaveValue('Rahul Patel')
    expect(screen.queryByText(/Heard:/)).not.toBeInTheDocument()
  })
})

describe('correcting the transcript', () => {
  const segments = [
    { id: 0, start_time: 0, end_time: 4, text: 'My name is Megapanvala and I will do the validation.', speaker: 'Speaker 1', confidence: 0.9 },
    { id: 1, start_time: 4, end_time: 7, text: 'Thanks Megapanvala.', speaker: 'Speaker 2', confidence: 0.9 },
  ]
  const meeting = (over: Record<string, unknown> = {}) => ({
    id: 'm1', title: 'Validation', status: 'uploaded', scheduled_time: null, end_time: null, duration_minutes: 1, participants: [],
    owner_email: 'x@y.com', owner_name: null, is_owner: true, started_at: null, ended_at: null, has_audio: false, created_at: '2026-11-13T12:00:00',
    can_write: true, has_transcript: true, has_summary: true, action_count: 0, open_action_count: 0, ...over,
  })

  async function open(routes: Record<string, unknown> = {}, m = meeting()) {
    signIn()
    let current = segments.map((x) => ({ ...x })) // what the "server" holds
    const server = mockServer({
      ...emptyApp,
      'GET /auth/me': meHandler,
      '/api/v1/meetings/m1': () => m,
      '/api/v1/meetings/m1/jobs/latest': () => jsonResponse({ detail: 'none' }, 404),
      '/api/v1/meetings/m1/transcript': () => ({ meeting_id: 'm1', transcript_id: 't', language: 'en', transcript: '', word_count: 20, speaker_count: 2, created_at: null, segments: current }),
      '/api/v1/meetings/m1/summary': () => ({ meeting_id: 'm1', executive_summary: 'Megapanvala owns it.', key_points: [], decisions: [], next_steps: [], key_quotes: [], created_at: null }),
      '/api/v1/meetings/m1/actions': () => ({ meeting_id: 'm1', action_items: [], count: 0 }),
      '/api/v1/meetings/m1/speakers': () => ({ meeting_id: 'm1', voices_available: false, speakers: [] }),
      '/api/v1/voice-profiles': () => ({ count: 0, profiles: [] }),
      'PATCH /api/v1/meetings/m1/transcript/segments/0': ({ body }: { body: { text: string } }) => {
        current = current.map((x) => (x.id === 0 ? { ...x, text: body.text, edited: true } : x))
        return { ...current[0], changed: true }
      },
      ...routes,
    } as never)
    renderApp('/meetings/m1')
    await userEvent.setup().click(await screen.findByRole('tab', { name: 'Transcript' }))
    await screen.findByText(/My name is Megapanvala/)
    return server
  }

  it('shows an edit button on every line to the owner only', async () => {
    await open()
    expect(screen.getAllByRole('button', { name: /^Edit 0:0/ })).toHaveLength(2)
    expect(screen.getByRole('button', { name: /Find & replace/ })).toBeInTheDocument()
  })

  it('shows no edit controls to someone who can only read', async () => {
    await open({}, meeting({ can_write: false, is_owner: false }))
    expect(screen.queryByRole('button', { name: /^Edit / })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Find & replace/ })).not.toBeInTheDocument()
  })

  it('fixes one line, marks it edited, and offers to regenerate the summary', async () => {
    const user = userEvent.setup()
    const { calls } = await open({
      'POST /api/v1/meetings/m1/process': () => ({ job_id: 'j1', state: 'queued', steps: ['summarize'], status_url: '' }),
      '/api/v1/jobs/j1': () => ({ job_id: 'j1', meeting_id: 'm1', state: 'queued', progress_percent: 0, attempts: 0, error: null, steps: [{ name: 'summarize', state: 'pending', error: null, result: null }], created_at: '', updated_at: '' }),
    })
    await user.click(screen.getAllByRole('button', { name: /^Edit 0:00/ })[0])
    const box = screen.getByLabelText('Transcript text')
    expect(box).toHaveValue('My name is Megapanvala and I will do the validation.')
    await user.clear(box)
    await user.type(box, 'My name is Megha Panwala and I will do the validation.')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByText('My name is Megha Panwala and I will do the validation.')).toBeInTheDocument()
    expect(screen.getByText('edited')).toBeInTheDocument()
    expect(calls.find((c) => c.method === 'PATCH')!.body).toEqual({ text: 'My name is Megha Panwala and I will do the validation.' })
    expect(screen.queryByLabelText('Transcript text')).not.toBeInTheDocument()

    expect(await screen.findByText(/summary still uses the old wording/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Regenerate summary' }))
    await waitFor(() => expect(calls.some((c) => c.url.endsWith('/process'))).toBe(true))
    expect(calls.find((c) => c.url.endsWith('/process'))!.body).toMatchObject({ steps: ['summarize'], regenerate_summary: true })
    await waitFor(() => expect(screen.queryByText(/summary still uses the old wording/)).not.toBeInTheDocument())
  })

  it('Escape cancels without saving, and an empty line is refused', async () => {
    const user = userEvent.setup()
    const { calls } = await open()
    await user.click(screen.getAllByRole('button', { name: /^Edit 0:00/ })[0])
    await user.type(screen.getByLabelText('Transcript text'), ' extra{Escape}')
    expect(screen.queryByLabelText('Transcript text')).not.toBeInTheDocument()
    expect(screen.getByText('My name is Megapanvala and I will do the validation.')).toBeInTheDocument()

    await user.click(screen.getAllByRole('button', { name: /^Edit 0:00/ })[0])
    await user.clear(screen.getByLabelText('Transcript text'))
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('The text cannot be empty.')).toBeInTheDocument()
    expect(calls.some((c) => c.method === 'PATCH')).toBe(false)
  })

  it('saving unchanged text does not claim the summary is out of date', async () => {
    const user = userEvent.setup()
    await open({ 'PATCH /api/v1/meetings/m1/transcript/segments/0': () => ({ ...segments[0], changed: false }) })
    await user.click(screen.getAllByRole('button', { name: /^Edit 0:00/ })[0])
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.queryByLabelText('Transcript text')).not.toBeInTheDocument())
    expect(screen.queryByText(/summary still uses the old wording/)).not.toBeInTheDocument()
  })

  it('shows the server’s reason when saving fails, and keeps the editor open', async () => {
    const user = userEvent.setup()
    await open({ 'PATCH /api/v1/meetings/m1/transcript/segments/0': () => jsonResponse({ detail: 'Only the meeting owner can modify this meeting' }, 403) })
    await user.click(screen.getAllByRole('button', { name: /^Edit 0:00/ })[0])
    await user.type(screen.getByLabelText('Transcript text'), ' more')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('Only the meeting owner can modify this meeting')).toBeInTheDocument()
    expect(screen.getByLabelText('Transcript text')).toBeInTheDocument()
  })

  it('find & replace: starts from the selected word, shows the count, replaces everywhere', async () => {
    const user = userEvent.setup()
    vi.spyOn(window, 'getSelection').mockReturnValue({ toString: () => 'Megapanvala' } as Selection)
    const { calls } = await open({
      'POST /api/v1/meetings/m1/transcript/replace': () => ({ transcript: 2, summary: 1, action_items: 0, total: 3 }),
    })
    await user.click(screen.getByRole('button', { name: /Find & replace/ }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByLabelText('Find')).toHaveValue('Megapanvala')
    expect(within(dialog).getByText('2 matches in the transcript')).toBeInTheDocument()
    const replaceAll = within(dialog).getByRole('button', { name: 'Replace all' })
    expect(replaceAll).toBeDisabled() // nothing to replace with yet
    await user.type(within(dialog).getByLabelText('Replace with'), 'Megha Panwala')
    await user.click(replaceAll)
    await waitFor(() => expect(calls.some((c) => c.url.endsWith('/transcript/replace'))).toBe(true))
    expect(calls.find((c) => c.url.endsWith('/transcript/replace'))!.body).toEqual({
      find: 'Megapanvala', replace: 'Megha Panwala', match_case: false, whole_word: true, include_summary: true,
    })
    expect(await screen.findByText(/Replaced 3 times: 2 in the transcript, 1 in the summary, 0 in tasks/)).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('find & replace: the count follows the options, and identical text cannot be submitted', async () => {
    const user = userEvent.setup()
    vi.spyOn(window, 'getSelection').mockReturnValue({ toString: () => '' } as Selection)
    await open()
    await user.click(screen.getByRole('button', { name: /Find & replace/ }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/select a word in the transcript first/)).toBeInTheDocument()
    await user.type(within(dialog).getByLabelText('Find'), 'megapanvala')
    expect(within(dialog).getByText('2 matches in the transcript')).toBeInTheDocument()
    await user.click(within(dialog).getByLabelText('Match upper/lower case'))
    expect(within(dialog).getByText('0 matches in the transcript')).toBeInTheDocument()
    await user.type(within(dialog).getByLabelText('Replace with'), 'megapanvala')
    expect(within(dialog).getByRole('button', { name: 'Replace all' })).toBeDisabled()
  })
})
