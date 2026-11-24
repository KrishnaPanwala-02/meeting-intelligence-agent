import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { onSessionLost, tokens } from '../src/api/client'
import { ToastProvider } from '../src/components/ui'
import { emptyApp, jsonResponse, me, meHandler, mockServer, pair, renderApp } from './helpers'

class FakeRecorder {
  static supported = new Set(['audio/webm;codecs=opus', 'audio/webm'])
  static isTypeSupported = (m: string) => FakeRecorder.supported.has(m)
  state = 'inactive'
  mimeType: string
  ondataavailable: ((e: { data: Blob }) => void) | null = null
  onstop: (() => void) | null = null
  onerror: (() => void) | null = null
  constructor(public stream: MediaStream, opts?: { mimeType?: string }) {
    this.mimeType = opts?.mimeType ?? 'audio/webm'
  }
  start() { this.state = 'recording' }
  pause() { this.state = 'paused' }
  resume() { this.state = 'recording' }
  stop() {
    this.state = 'inactive'
    this.ondataavailable?.({ data: new Blob([new Uint8Array(5000)], { type: this.mimeType }) })
    this.onstop?.()
  }
}

beforeEach(() => {
  tokens.clear()
  onSessionLost(null)
  tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
  Element.prototype.scrollIntoView = vi.fn()
  vi.stubGlobal('MediaRecorder', FakeRecorder)
  Object.defineProperty(navigator, 'mediaDevices', { value: { getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [{ stop: vi.fn() }] }) }, configurable: true })
  URL.createObjectURL = vi.fn(() => 'blob:preview')
  URL.revokeObjectURL = vi.fn()
})
afterEach(() => vi.unstubAllGlobals())

const speaker = (over: Record<string, unknown> = {}) => ({
  label: 'Speaker 1', segments: 1, seconds: 6, words: 13, named: false, has_voice: true, suggestion: null,
  voice_match: { name: 'Rahul Patel', score: 0.62, strong: false }, ...over,
})

async function card(speakers: unknown[], props: { canWrite?: boolean; hasAudio?: boolean } = {}, routes: Record<string, unknown> = {}) {
  const { default: SpeakersCard } = await import('../src/components/Speakers')
  const { AuthProvider } = await import('../src/auth/AuthContext')
  const server = mockServer({
    'GET /auth/me': meHandler,
    '/api/v1/voice-profiles': () => ({ count: 0, profiles: [] }),
    'GET /api/v1/meetings/m1/speakers': () => ({ meeting_id: 'm1', voices_available: true, speakers }),
    'PUT /api/v1/meetings/m1/speakers': () => ({ renamed: true, voice_saved: true, label: 'Rahul Patel' }),
    ...routes,
  } as never)
  const onChanged = vi.fn()
  render(
    <MemoryRouter><ToastProvider><AuthProvider>
      <SpeakersCard meetingId="m1" canWrite={props.canWrite ?? true} hasAudio={props.hasAudio ?? true} analyzing={false} refreshKey={0} onChanged={onChanged} onAnalyze={() => undefined} />
    </AuthProvider></ToastProvider></MemoryRouter>,
  )
  return { ...server, onChanged }
}

describe('"Is this them?" for a voice that sounds like someone saved', () => {
  it('offers the likely person with how sure it is, and says to check when not certain', async () => {
    await card([speaker()])
    expect(await screen.findByText(/Sounds like/)).toBeInTheDocument()
    expect(screen.getByText('Rahul Patel', { selector: 'strong' })).toBeInTheDocument()
    expect(screen.getByText(/62% match, so check it/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Yes, that is Rahul' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'No' })).toBeInTheDocument()
  })

  it('a strong match does not ask to check it', async () => {
    await card([speaker({ voice_match: { name: 'Rahul Patel', score: 0.91, strong: true } })])
    expect(await screen.findByText(/91% match\)/)).toBeInTheDocument()
    expect(screen.queryByText(/so check it/)).not.toBeInTheDocument()
  })

  it('"Yes" names the speaker and remembers the voice', async () => {
    const user = userEvent.setup()
    const { calls, onChanged } = await card([speaker()])
    await user.click(await screen.findByRole('button', { name: 'Yes, that is Rahul' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    expect(calls.find((c) => c.method === 'PUT')!.body).toEqual({ label: 'Speaker 1', name: 'Rahul Patel', remember_voice: true })
    expect(await screen.findByText(/Saved as Rahul Patel\. Their voice will be recognised even better next time/)).toBeInTheDocument()
    expect(onChanged).toHaveBeenCalled()
  })

  it('"No" hides the suggestion and changes nothing', async () => {
    const user = userEvent.setup()
    const { calls } = await card([speaker()])
    await user.click(await screen.findByRole('button', { name: 'No' }))
    expect(screen.queryByText(/Sounds like/)).not.toBeInTheDocument()
    expect(calls.some((c) => c.method === 'PUT')).toBe(false)
    expect(screen.getByLabelText('Name for Speaker 1')).toBeInTheDocument()          // they can still type a name
  })

  it('shows why it failed and keeps the question', async () => {
    const user = userEvent.setup()
    await card([speaker()], {}, { 'PUT /api/v1/meetings/m1/speakers': () => jsonResponse({ detail: 'Only the meeting owner can modify this meeting' }, 403) })
    await user.click(await screen.findByRole('button', { name: 'Yes, that is Rahul' }))
    expect(await screen.findByText('Only the meeting owner can modify this meeting')).toBeInTheDocument()
  })

  it('is not shown for a speaker who is already named, has no match, or to a reader', async () => {
    await card([speaker({ label: 'Anna Larsen', named: true }), speaker({ label: 'Speaker 2', voice_match: null })])
    await screen.findByLabelText('Name for Speaker 2')
    expect(screen.queryByText(/Sounds like/)).not.toBeInTheDocument()
  })

  it('a read-only person sees no question to answer', async () => {
    await card([speaker()], { canWrite: false })
    await screen.findByText('Speaker 1')
    expect(screen.queryByText(/Sounds like/)).not.toBeInTheDocument()
  })
})

const CLIPS = [
  { start: 12, end: 18.5, text: 'Good morning everyone, I am ready.' },
  { start: 40, end: 47, text: 'I will finish the report by Friday.' },
]

describe('hearing who an unnamed speaker is', () => {
  let played: Array<{ el: HTMLMediaElement; at: number }>
  let paused: number
  beforeEach(() => {
    played = []
    paused = 0
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(function (this: HTMLMediaElement) {
      played.push({ el: this, at: this.currentTime })
      return Promise.resolve()
    })
    vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => { paused += 1 })
  })
  // not restored: the card pauses on unmount, after this test body, and jsdom cannot play audio

  const audioRoute = () => ({ 'GET /api/v1/meetings/m1/audio': () => new Response(new Blob(['x'], { type: 'audio/wav' }), { status: 200 }) })
  const unnamed = () => speaker({ clips: CLIPS, voice_match: null })

  it('offers a play button for each clip, and shows what is said', async () => {
    await card([unnamed()], {}, audioRoute())
    expect(await screen.findByRole('button', { name: 'Play Speaker 1 at 0:12' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Play Speaker 1 at 0:40' })).toBeInTheDocument()
    expect(screen.getByText(/Good morning everyone, I am ready\./)).toBeInTheDocument()
  })

  it('plays from the clip start, fetching the recording only once', async () => {
    const user = userEvent.setup()
    const { calls } = await card([unnamed()], {}, audioRoute())
    await user.click(await screen.findByRole('button', { name: 'Play Speaker 1 at 0:12' }))
    await waitFor(() => expect(played).toHaveLength(1))
    expect(played[0].at).toBe(12)
    await user.click(screen.getByRole('button', { name: 'Play Speaker 1 at 0:40' }))
    await waitFor(() => expect(played).toHaveLength(2))
    expect(played[1].at).toBe(40)
    expect(calls.filter((c) => c.url.endsWith('/audio'))).toHaveLength(1)
    expect(screen.getByText(/I will finish the report by Friday\./)).toBeInTheDocument()   // shows the clip being played
  })

  it('the button turns into stop while playing, and stops when pressed again', async () => {
    const user = userEvent.setup()
    await card([unnamed()], {}, audioRoute())
    await user.click(await screen.findByRole('button', { name: 'Play Speaker 1 at 0:12' }))
    const stop = await screen.findByRole('button', { name: 'Stop Speaker 1 at 0:12' })
    expect(stop).toHaveAttribute('aria-pressed', 'true')
    await user.click(stop)
    expect(paused).toBeGreaterThan(0)
    expect(await screen.findByRole('button', { name: 'Play Speaker 1 at 0:12' })).toBeInTheDocument()
  })

  it('stops by itself at the end of the clip', async () => {
    const user = userEvent.setup()
    await card([unnamed()], {}, audioRoute())
    await user.click(await screen.findByRole('button', { name: 'Play Speaker 1 at 0:12' }))
    await screen.findByRole('button', { name: 'Stop Speaker 1 at 0:12' })
    const el = played[0].el
    Object.defineProperty(el, 'currentTime', { value: 18.6, configurable: true, writable: true })
    el.dispatchEvent(new Event('timeupdate'))
    expect(await screen.findByRole('button', { name: 'Play Speaker 1 at 0:12' })).toBeInTheDocument()
    expect(paused).toBeGreaterThan(0)
  })

  it('says so when the recording cannot be loaded', async () => {
    const user = userEvent.setup()
    await card([unnamed()], {}, { 'GET /api/v1/meetings/m1/audio': () => jsonResponse({ detail: 'No stored audio for this meeting.' }, 404) })
    await user.click(await screen.findByRole('button', { name: 'Play Speaker 1 at 0:12' }))
    expect(await screen.findByText(/Could not play the recording: No stored audio for this meeting\./)).toBeInTheDocument()
    expect(played).toHaveLength(0)
  })

  it('also works for a speaker who is already named (to check a match), and for a read-only viewer', async () => {
    await card([speaker({ label: 'Jai Jain', named: true, voice_match: null, clips: CLIPS })], { canWrite: false }, audioRoute())
    expect(await screen.findByText(/Listen to Jai Jain/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Play Jai Jain at 0:12' })).toBeInTheDocument()
  })

  it('shows nothing to play without a recording or without clips', async () => {
    await card([unnamed()], { hasAudio: false })
    await screen.findByLabelText('Name for Speaker 1')
    expect(screen.queryByText(/Listen to/)).not.toBeInTheDocument()
  })

  it('a speaker with no clips has no listen row', async () => {
    await card([speaker({ clips: [], voice_match: null })], {}, audioRoute())
    await screen.findByLabelText('Name for Speaker 1')
    expect(screen.queryByText(/Listen to/)).not.toBeInTheDocument()
  })
})

const PEOPLE = {
  count: 3,
  people: [
    { name: 'Darshan Patel', email: 'darshanpatel@gmail.com' },
    { name: 'Jai Jain', email: 'jaijain@gmail.com' },
    { name: 'Meet Oza', email: 'meetoza@gmail.com' },
  ],
}

describe('correcting a voice that was matched to the wrong member', () => {
  const wrong = () => speaker({ label: 'Jai Jain', named: true, voice_match: null, email: 'jaijain@gmail.com' })

  it('shows who a named speaker is, and suggests every member in the name box', async () => {
    await card([wrong()], {}, { '/api/v1/people': () => PEOPLE })
    expect(await screen.findByText('jaijain@gmail.com')).toBeInTheDocument()
    await waitFor(() => expect(document.querySelectorAll('datalist option').length).toBeGreaterThanOrEqual(3))
    const names = Array.from(document.querySelectorAll('datalist option')).map((o) => (o as HTMLOptionElement).value)
    expect(names).toEqual(expect.arrayContaining(['Darshan Patel', 'Jai Jain', 'Meet Oza']))
  })

  it('changing the name shows who gets access, and offers to remove the wrong person', async () => {
    const user = userEvent.setup()
    await card([wrong()], {}, { '/api/v1/people': () => PEOPLE })
    const box = await screen.findByLabelText('Name for Jai Jain')
    await screen.findByText('jaijain@gmail.com')
    await user.clear(box)
    await user.type(box, 'Darshan Patel')
    expect(await screen.findByText(/Darshan Patel will be given access \(darshanpatel@gmail.com\)/)).toBeInTheDocument()
    expect(screen.getByLabelText(/Remove Jai Jain’s access \(jaijain@gmail.com\)/)).toBeChecked()
  })

  it('saves the new name and asks the server to remove the previous person', async () => {
    const user = userEvent.setup()
    const { calls } = await card([wrong()], {}, {
      '/api/v1/people': () => PEOPLE,
      'PUT /api/v1/meetings/m1/speakers': () => ({ renamed: true, voice_saved: true, label: 'Darshan Patel', invited: 'darshanpatel@gmail.com', revoked: 'jaijain@gmail.com' }),
    })
    const box = await screen.findByLabelText('Name for Jai Jain')
    await screen.findByText('jaijain@gmail.com')
    await user.clear(box)
    await user.type(box, 'Darshan Patel')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    expect(calls.find((c) => c.method === 'PUT')!.body).toMatchObject({ label: 'Jai Jain', name: 'Darshan Patel', revoke_previous: true })
    expect(await screen.findByText(/darshanpatel@gmail.com can now sign in/)).toBeInTheDocument()
    expect(screen.getByText(/jaijain@gmail.com no longer has access/)).toBeInTheDocument()
  })

  it('keeps the previous person\'s access when the box is unticked', async () => {
    const user = userEvent.setup()
    const { calls } = await card([wrong()], {}, { '/api/v1/people': () => PEOPLE })
    const box = await screen.findByLabelText('Name for Jai Jain')
    await screen.findByText('jaijain@gmail.com')
    await user.clear(box)
    await user.type(box, 'Meet Oza')
    await user.click(await screen.findByLabelText(/Remove Jai Jain’s access/))
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    expect(calls.find((c) => c.method === 'PUT')!.body).not.toHaveProperty('revoke_previous')
  })

  it('a name nobody is registered with asks for an e-mail instead', async () => {
    const user = userEvent.setup()
    await card([wrong()], {}, { '/api/v1/people': () => PEOPLE })
    const box = await screen.findByLabelText('Name for Jai Jain')
    await user.clear(box)
    await user.type(box, 'Visitor Person')
    expect(await screen.findByText(/Add their e-mail so they can see the meeting/)).toBeInTheDocument()
  })

  it('no correction prompt appears for an unnamed speaker', async () => {
    const user = userEvent.setup()
    await card([speaker({ voice_match: null })], {}, { '/api/v1/people': () => PEOPLE })
    await user.type(await screen.findByLabelText('Name for Speaker 1'), 'Darshan Patel')
    expect(screen.queryByText(/Remove .* access/)).not.toBeInTheDocument()
    expect(await screen.findByPlaceholderText('Saved: darshanpatel@gmail.com')).toBeInTheDocument()
  })

  it('still works when the directory cannot be loaded', async () => {
    await card([wrong()], {}, { '/api/v1/people': () => jsonResponse({ detail: 'nope' }, 500) })
    expect(await screen.findByLabelText('Name for Jai Jain')).toBeInTheDocument()
  })
})

describe('recording my own voice in Settings', () => {
  function settings(profiles: unknown[], routes: Record<string, unknown> = {}, user: Record<string, unknown> = {}) {
    return mockServer({
      ...emptyApp,
      'GET /auth/me': () => ({ ...me, ...user }),
      '/auth/sessions': () => ({ sessions: [] }),
      'GET /api/v1/features': () => ({ email_enabled: true }),
      'GET /api/v1/voice-profiles': () => ({ count: profiles.length, profiles }),
      ...routes,
    } as never)
  }

  it('asks for a name first when there is none', async () => {
    settings([], {}, { name: null })
    renderApp('/settings')
    expect(await screen.findByText(/Add your full name first \(above\)/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Start recording' })).not.toBeInTheDocument()
  })

  it('explains what to do, with a sentence to read in the person’s own name', async () => {
    settings([])
    renderApp('/settings')
    expect(await screen.findByText(/about 15 seconds, alone and somewhere quiet/)).toBeInTheDocument()
    expect(screen.getByText(/My name is Anna Larsen, and I am recording my voice/)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Start recording' }).length).toBeGreaterThan(0)
  })

  it('shows that a voice is already saved, matching it by the person’s e-mail', async () => {
    settings([
      { id: 'p1', name: 'Anna Larsen', samples: 2, created_at: null, email: 'anna@example.com' },
      { id: 'p2', name: 'Rahul Patel', samples: 5, created_at: null, email: 'rahul@example.com' },
    ])
    renderApp('/settings')
    expect(await screen.findByText(/Your voice is saved \(2 samples\)/)).toBeInTheDocument()
    expect(screen.getByText('Saved')).toBeInTheDocument()
  })

  async function record(user: ReturnType<typeof userEvent.setup>) {
    await user.click((await screen.findAllByRole('button', { name: 'Start recording' }))[0])
    await user.click(await screen.findByRole('button', { name: 'Stop' }))
    await user.click(await screen.findByRole('button', { name: 'Use this recording' }))
  }

  it('uploads the recording and confirms what was saved', async () => {
    const user = userEvent.setup()
    let saved = false
    const { calls } = settings([], {
      'POST /api/v1/voice-profiles/me': () => ((saved = true), { id: 'p', name: 'Anna Larsen', email: 'anna@example.com', samples: 1, speech_seconds: 12.4 }),
      'GET /api/v1/voice-profiles': () => ({ count: saved ? 1 : 0, profiles: saved ? [{ id: 'p', name: 'Anna Larsen', samples: 1, created_at: null, email: 'anna@example.com' }] : [] }),
    })
    renderApp('/settings')
    await record(user)
    expect(await screen.findByText(/Saved your voice \(12 seconds of speech\)/)).toBeInTheDocument()
    const sent = calls.find((c) => c.url === '/api/v1/voice-profiles/me')!.body as FormData
    expect((sent.get('audio_file') as File).name).toMatch(/^Recording .+\.webm$/)
    expect(await screen.findByText(/Your voice is saved \(1 sample\)/)).toBeInTheDocument()
    expect((await screen.findAllByRole('button', { name: 'Start recording' })).length).toBeGreaterThan(0)   // ready to record again
  })

  it('shows the server’s reason when the clip was not usable, and lets you try again', async () => {
    const user = userEvent.setup()
    settings([], { 'POST /api/v1/voice-profiles/me': () => jsonResponse({ detail: 'More than one voice was heard. Record again in a quiet place, on your own.' }, 422) })
    renderApp('/settings')
    await record(user)
    expect(await screen.findByText(/More than one voice was heard/)).toBeInTheDocument()
    expect(screen.queryByText(/Analysing your voice/)).not.toBeInTheDocument()
    expect((await screen.findAllByRole('button', { name: 'Start recording' })).length).toBeGreaterThan(0)
  })
})
