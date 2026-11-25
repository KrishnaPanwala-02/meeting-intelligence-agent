import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { onSessionLost, tokens } from '../src/api/client'
import OnlineRecorder from '../src/components/OnlineRecorder'
import { ToastProvider } from '../src/components/ui'
import { captureSupport, describeCaptureError, peakLevel, surfaceAdvice, surfaceOf } from '../src/lib/capture'
import { emptyApp, meHandler, mockServer, pair, renderApp } from './helpers'

// ---------------------------------------------------------------- a fake browser

class FakeTrack {
  stop = vi.fn()
  listeners: Record<string, (() => void)[]> = {}
  constructor(public kind: 'audio' | 'video', private settings: Record<string, unknown> = {}) {}
  addEventListener(ev: string, fn: () => void) {
    ;(this.listeners[ev] ??= []).push(fn)
  }
  getSettings() {
    return this.settings
  }
  end() {
    ;(this.listeners.ended ?? []).forEach((f) => f())
  }
}
class FakeStream {
  constructor(public tracks: FakeTrack[] = []) {}
  getTracks() { return this.tracks }
  getAudioTracks() { return this.tracks.filter((t) => t.kind === 'audio') }
  getVideoTracks() { return this.tracks.filter((t) => t.kind === 'video') }
}
const levels = { tab: 0, mic: 0 }
const analysers: { fftSize: number; kind: 'tab' | 'mic'; getByteTimeDomainData: (a: Uint8Array) => void }[] = []
const connections: { from: string; to: string; input?: number }[] = []
const mixed = new FakeStream([new FakeTrack('audio')])
class FakeCtx {
  destination = { name: 'speakers' }
  n = 0
  createMediaStreamSource(s: FakeStream) {
    const name = `source${this.n++}`
    return {
      name, stream: s,
      connect: (to: { name?: string }, _out?: number, input?: number) =>
        connections.push({ from: name, to: to.name ?? 'analyser', ...(input === undefined ? {} : { input }) }),
    }
  }
  createChannelMerger() {
    return { name: 'merger', connect: (to: { name?: string }) => connections.push({ from: 'merger', to: to.name ?? 'analyser' }) }
  }
  createAnalyser() {
    const kind: 'tab' | 'mic' = analysers.length === 0 ? 'tab' : 'mic'
    const a = { fftSize: 2048, kind, name: 'analyser', getByteTimeDomainData: (arr: Uint8Array) => { arr.fill(128); arr[0] = 128 + Math.round(levels[kind] * 128) } }
    analysers.push(a)
    return a
  }
  createMediaStreamDestination() { return { name: 'recording', stream: mixed } }
  resume() { return Promise.resolve() }
  close = vi.fn(() => Promise.resolve())
}
class FakeRecorder {
  static instances: FakeRecorder[] = []
  static isTypeSupported = (m: string) => m === 'audio/webm;codecs=opus'
  static tiny = false
  state = 'inactive'
  mimeType = 'audio/webm;codecs=opus'
  ondataavailable: ((e: { data: Blob }) => void) | null = null
  onstop: (() => void) | null = null
  onerror: (() => void) | null = null
  started?: number
  constructor(public stream: unknown, public opts: Record<string, unknown>) { FakeRecorder.instances.push(this) }
  start(ms: number) { this.state = 'recording'; this.started = ms }
  stop() {
    this.state = 'inactive'
    this.ondataavailable?.({ data: new Blob([new Uint8Array(FakeRecorder.tiny ? 10 : 6000)], { type: this.mimeType }) })
    this.onstop?.()
  }
}

let display: FakeStream
let mic: FakeStream
const getDisplayMedia = vi.fn()
const getUserMedia = vi.fn()

function shared(opts: { audio?: boolean; surface?: string } = {}) {
  const tracks = [new FakeTrack('video', { displaySurface: opts.surface ?? 'browser' })]
  if (opts.audio !== false) tracks.push(new FakeTrack('audio'))
  display = new FakeStream(tracks)
  getDisplayMedia.mockResolvedValue(display)
}

beforeEach(() => {
  tokens.clear()
  onSessionLost(null)
  tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
  Element.prototype.scrollIntoView = vi.fn()
  levels.tab = 0
  levels.mic = 0
  analysers.length = 0
  connections.length = 0
  FakeRecorder.instances = []
  FakeRecorder.tiny = false
  mic = new FakeStream([new FakeTrack('audio')])
  getUserMedia.mockReset().mockResolvedValue(mic)
  getDisplayMedia.mockReset()
  shared()
  vi.stubGlobal('MediaRecorder', FakeRecorder)
  vi.stubGlobal('MediaStream', FakeStream)
  vi.stubGlobal('AudioContext', FakeCtx)
  Object.defineProperty(navigator, 'mediaDevices', { value: { getDisplayMedia, getUserMedia }, configurable: true })
  Object.defineProperty(navigator, 'userAgentData', { value: { brands: [{ brand: 'Chromium' }, { brand: 'Google Chrome' }] }, configurable: true })
  URL.createObjectURL = vi.fn(() => 'blob:preview')
  URL.revokeObjectURL = vi.fn()
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

function setup(onUse = vi.fn()) {
  render(<ToastProvider><OnlineRecorder onUse={onUse} /></ToastProvider>)
  return onUse
}

async function startRecording(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('checkbox', { name: /I have told everyone in this meeting/ }))
  await user.click(screen.getByRole('button', { name: 'Start recording' }))
  await screen.findByRole('timer')
}

// -------------------------------------------------------------- pure helpers

describe('capture helpers', () => {
  it('reads what was shared', () => {
    expect(surfaceOf({ displaySurface: 'browser' } as MediaTrackSettings)).toBe('tab')
    expect(surfaceOf({ displaySurface: 'window' } as MediaTrackSettings)).toBe('window')
    expect(surfaceOf({ displaySurface: 'monitor' } as MediaTrackSettings)).toBe('screen')
    expect(surfaceOf({} as MediaTrackSettings)).toBe('unknown')
    expect(surfaceOf(undefined)).toBe('unknown')
  })
  it('only a tab needs no warning', () => {
    expect(surfaceAdvice('tab')).toBeNull()
    expect(surfaceAdvice('screen')).toMatch(/whole screen.*everything your computer plays/)
    expect(surfaceAdvice('window')).toMatch(/cannot capture sound from a window/)
    expect(surfaceAdvice('unknown')).toMatch(/Meeting audio.*meter/)
  })
  it('measures loudness from silence to full', () => {
    const at = (peak: number) => ({ fftSize: 8, getByteTimeDomainData: (a: Uint8Array) => { a.fill(128); a[3] = 128 + peak } })
    expect(peakLevel(at(0))).toBe(0)
    expect(peakLevel(at(64))).toBe(0.5)
    expect(peakLevel(at(127))).toBeCloseTo(0.99, 1)
    expect(peakLevel({ fftSize: 4, getByteTimeDomainData: (a) => a.fill(0) })).toBe(1)     // the loudest negative swing
  })
  it('explains the common ways sharing fails', () => {
    expect(describeCaptureError(new DOMException('x', 'NotAllowedError'))).toMatch(/cancelled or blocked/)
    expect(describeCaptureError(new DOMException('x', 'AbortError'))).toMatch(/cancelled or blocked/)
    expect(describeCaptureError(new DOMException('x', 'NotFoundError'))).toMatch(/another tab/)
    expect(describeCaptureError(new DOMException('x', 'NotReadableError'))).toMatch(/could not read/)
    expect(describeCaptureError(new Error('odd'))).toBe('odd')
    expect(describeCaptureError('???')).toMatch(/Could not start/)
  })
  it('only Chromium browsers with the sharing API are supported', () => {
    expect(captureSupport().ok).toBe(true)
    Object.defineProperty(navigator, 'userAgentData', { value: { brands: [{ brand: 'Safari' }] }, configurable: true })
    expect(captureSupport()).toMatchObject({ ok: false, reason: expect.stringMatching(/Only Chrome and Edge/) })
    Object.defineProperty(navigator, 'userAgentData', { value: undefined, configurable: true })
    vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Mozilla/5.0 Firefox/130.0')
    expect(captureSupport().ok).toBe(false)
    vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Mozilla/5.0 Chrome/130.0 Safari/537.36')
    expect(captureSupport().ok).toBe(true)
    Object.defineProperty(navigator, 'mediaDevices', { value: {}, configurable: true })
    expect(captureSupport().reason).toMatch(/cannot share a tab/)
  })
})

// -------------------------------------------------------------- before recording

describe('before recording', () => {
  it('explains the steps, advises headphones, and needs consent', async () => {
    const user = userEvent.setup()
    setup()
    expect(screen.getByText(/Open the meeting in its own Chrome or Edge tab/)).toBeInTheDocument()
    expect(screen.getByText(/Also share tab audio/, { selector: 'strong' })).toBeInTheDocument()
    expect(screen.getByText(/Use headphones/)).toBeInTheDocument()
    expect(screen.getByText(/desktop apps are not supported here/)).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: /Also record my microphone/ })).toBeChecked()
    const start = screen.getByRole('button', { name: 'Start recording' })
    expect(start).toBeDisabled()
    await user.click(screen.getByRole('checkbox', { name: /I have told everyone in this meeting/ }))
    expect(start).toBeEnabled()
    expect(getDisplayMedia).not.toHaveBeenCalled()
  })

  it('in an unsupported browser it says what to use instead, and offers uploading', () => {
    Object.defineProperty(navigator, 'userAgentData', { value: { brands: [{ brand: 'Safari' }] }, configurable: true })
    setup()
    expect(screen.getByRole('status')).toHaveTextContent(/Only Chrome and Edge can share a tab’s sound/)
    expect(screen.getByText(/upload a recording of the meeting instead/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Start recording' })).not.toBeInTheDocument()
  })
})

// ------------------------------------------------------- is it really meeting audio

describe('checking that meeting audio is really being captured', () => {
  it('asks the browser for the tab with its audio, on a tiny picture that is never recorded', async () => {
    const user = userEvent.setup()
    setup()
    await startRecording(user)
    const asked = getDisplayMedia.mock.calls[0][0]
    expect(asked.audio).toMatchObject({ echoCancellation: false, noiseSuppression: false, autoGainControl: false })
    expect(asked.systemAudio).toBe('include')
    expect(asked.selfBrowserSurface).toBe('exclude')
    expect(asked.video.frameRate.max).toBeLessThanOrEqual(5)
    const rec = FakeRecorder.instances[0]
    expect(rec.stream).toBe(mixed)                                                   // only the mixed audio is recorded: no video
    expect(rec.opts).toMatchObject({ audioBitsPerSecond: 64000, mimeType: 'audio/webm;codecs=opus' })
    expect(rec.started).toBe(1000)
  })

  it('refuses to record when no audio came with the share, and lets go of everything', async () => {
    shared({ audio: false })
    const user = userEvent.setup()
    setup()
    await user.click(screen.getByRole('checkbox', { name: /I have told everyone in this meeting/ }))
    await user.click(screen.getByRole('button', { name: 'Start recording' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/No audio was shared, so only your microphone would be recorded/)
    expect(screen.getByRole('alert')).toHaveTextContent(/Also share tab audio/)
    expect(FakeRecorder.instances).toHaveLength(0)
    expect(display.getTracks().every((t) => t.stop.mock.calls.length === 1)).toBe(true)   // the share was ended
    expect(getUserMedia).not.toHaveBeenCalled()                                        // never even asked for the microphone
    expect(screen.getByRole('button', { name: 'Start recording' })).toBeInTheDocument()
  })

  it('says so if the share dialog was cancelled', async () => {
    getDisplayMedia.mockRejectedValue(new DOMException('no', 'NotAllowedError'))
    const user = userEvent.setup()
    setup()
    await user.click(screen.getByRole('checkbox', { name: /I have told everyone in this meeting/ }))
    await user.click(screen.getByRole('button', { name: 'Start recording' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/cancelled or blocked/)
    expect(FakeRecorder.instances).toHaveLength(0)
  })

  it('shows two live meters, one for the meeting and one for the microphone', async () => {
    const user = userEvent.setup()
    setup()
    await startRecording(user)
    const tab = screen.getByRole('meter', { name: 'Meeting audio' })
    const mic = screen.getByRole('meter', { name: 'Your microphone' })
    levels.tab = 0.5
    levels.mic = 0.25
    await waitFor(() => expect(tab).toHaveAttribute('aria-valuenow', '50'))
    expect(mic).toHaveAttribute('aria-valuenow', '25')
    levels.tab = 0
    await waitFor(() => expect(tab).toHaveAttribute('aria-valuenow', '0'))
  })

  it('records the microphone and the tab on separate channels and never plays the tab aloud again', async () => {
    const user = userEvent.setup()
    setup()
    await startRecording(user)
    const toMerger = connections.filter((c) => c.to === 'merger')
    expect(toMerger).toEqual([{ from: 'source0', to: 'merger', input: 1 }, { from: 'source1', to: 'merger', input: 0 }]) // tab right, microphone left
    expect(connections.filter((c) => c.to === 'recording').map((c) => c.from)).toEqual(['merger'])
    expect(connections.some((c) => c.to === 'speakers')).toBe(false)                   // nothing is sent to the speakers (no echo)
    expect(analysers).toHaveLength(2)
  })

  it('without a microphone the tab plays on both channels as before', async () => {
    const user = userEvent.setup()
    setup()
    await user.click(screen.getByRole('checkbox', { name: /Also record my microphone/ }))
    await startRecording(user)
    expect(connections.some((c) => c.to === 'merger')).toBe(false)
    expect(connections.filter((c) => c.to === 'recording').map((c) => c.from)).toEqual(['source0'])
  })

  it('warns when no meeting audio is heard for a while, and clears the warning when it is', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] })
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    setup()
    await startRecording(user)
    levels.mic = 0.4                                                                    // the user is talking, the meeting is silent
    await act(async () => { await vi.advanceTimersByTimeAsync(9_000) })
    expect(screen.queryByText(/No meeting audio has been heard/)).not.toBeInTheDocument()
    await act(async () => { await vi.advanceTimersByTimeAsync(2_500) })
    expect(screen.getByRole('alert')).toHaveTextContent(/No meeting audio has been heard for a while.*Also share tab audio/)
    levels.tab = 0.3                                                                    // someone speaks
    await act(async () => { await vi.advanceTimersByTimeAsync(400) })
    expect(screen.queryByText(/No meeting audio has been heard/)).not.toBeInTheDocument()
  })

  it('warns separately when only the microphone is silent', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] })
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    setup()
    await startRecording(user)
    levels.tab = 0.3
    await act(async () => { await vi.advanceTimersByTimeAsync(11_000) })
    expect(screen.queryByText(/No meeting audio/)).not.toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent(/Your microphone is silent/)
  })

  it('a whole-screen or window share still records, but with a clear warning', async () => {
    for (const [surface, text] of [['monitor', /whole screen/], ['window', /cannot capture sound from a window/]] as const) {
      shared({ surface })
      const user = userEvent.setup()
      const { unmount } = render(<ToastProvider><OnlineRecorder onUse={vi.fn()} /></ToastProvider>)
      await user.click(screen.getByRole('checkbox', { name: /I have told everyone in this meeting/ }))
      await user.click(screen.getByRole('button', { name: 'Start recording' }))
      await screen.findByRole('timer')
      expect(screen.getByText(text)).toBeInTheDocument()
      unmount()
    }
  })

  it('sharing a tab gives no surface warning', async () => {
    const user = userEvent.setup()
    setup()
    await startRecording(user)
    expect(screen.queryByText(/whole screen|window|did not say/)).not.toBeInTheDocument()
  })
})

// ------------------------------------------------------------ the microphone

describe('your own voice', () => {
  it('if the microphone is denied it still records the meeting, and says your voice is missing', async () => {
    getUserMedia.mockRejectedValue(new DOMException('no', 'NotAllowedError'))
    const user = userEvent.setup()
    setup()
    await startRecording(user)
    expect(screen.getByText(/microphone could not be used, so only the other people/)).toBeInTheDocument()
    expect(screen.queryByRole('meter', { name: 'Your microphone' })).not.toBeInTheDocument()
    expect(screen.getByRole('meter', { name: 'Meeting audio' })).toBeInTheDocument()
  })

  it('with the microphone switched off, it never asks for it', async () => {
    const user = userEvent.setup()
    setup()
    await user.click(screen.getByRole('checkbox', { name: /Also record my microphone/ }))
    await startRecording(user)
    expect(getUserMedia).not.toHaveBeenCalled()
    expect(screen.getByText(/Your microphone is off, so your own voice will not be recorded/)).toBeInTheDocument()
  })
})

// ------------------------------------------------------------- finishing

describe('finishing a recording', () => {
  it('Stop gives a preview and hands back the file with what was captured', async () => {
    const user = userEvent.setup()
    const onUse = setup()
    await startRecording(user)
    await user.click(screen.getByRole('button', { name: 'Stop' }))
    expect(await screen.findByTestId('recording-preview')).toHaveAttribute('src', 'blob:preview')
    expect(screen.getByText(/meeting audio and your microphone/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Save a copy' })).toHaveAttribute('href', 'blob:preview')
    expect(display.getTracks().every((t) => t.stop.mock.calls.length === 1)).toBe(true)  // the share ended
    expect(mic.getTracks()[0].stop).toHaveBeenCalled()                                    // the microphone was released

    await user.click(screen.getByRole('button', { name: 'Use this recording' }))
    expect(onUse).toHaveBeenCalledOnce()
    const [file, info] = onUse.mock.calls[0]
    expect(file.name).toMatch(/^Online meeting .+\.webm$/)
    expect(file.size).toBe(6000)
    expect(info).toEqual({ source: 'online_tab', tab: true, mic: true, surface: 'tab' })
  })

  it('without the microphone, the result says so', async () => {
    getUserMedia.mockRejectedValue(new DOMException('no', 'NotFoundError'))
    const user = userEvent.setup()
    const onUse = setup()
    await startRecording(user)
    await user.click(screen.getByRole('button', { name: 'Stop' }))
    expect(await screen.findByText(/· meeting audio only/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Use this recording' }))
    expect(onUse.mock.calls[0][1]).toMatchObject({ tab: true, mic: false })
  })

  it('the browser’s own "Stop sharing" button ends the recording cleanly', async () => {
    const user = userEvent.setup()
    setup()
    await startRecording(user)
    act(() => display.getVideoTracks()[0].end())
    expect(await screen.findByTestId('recording-preview')).toBeInTheDocument()
    expect(FakeRecorder.instances[0].state).toBe('inactive')
  })

  it('so does the tab’s audio ending (the meeting tab was closed)', async () => {
    const user = userEvent.setup()
    setup()
    await startRecording(user)
    act(() => display.getAudioTracks()[0].end())
    expect(await screen.findByTestId('recording-preview')).toBeInTheDocument()
  })

  it('an empty recording is refused with advice', async () => {
    FakeRecorder.tiny = true
    const user = userEvent.setup()
    const onUse = setup()
    await startRecording(user)
    await user.click(screen.getByRole('button', { name: 'Stop' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/Nothing was recorded/)
    expect(onUse).not.toHaveBeenCalled()
  })

  it('"Record again" starts over and releases the old preview', async () => {
    const user = userEvent.setup()
    setup()
    await startRecording(user)
    await user.click(screen.getByRole('button', { name: 'Stop' }))
    await user.click(await screen.findByRole('button', { name: 'Record again' }))
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:preview')
    expect(screen.getByRole('button', { name: 'Start recording' })).toBeInTheDocument()
  })

  it('warns before the page is closed while recording, but not otherwise', async () => {
    const user = userEvent.setup()
    setup()
    const fire = () => { const e = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(e); return e.defaultPrevented }
    expect(fire()).toBe(false)
    await startRecording(user)
    expect(fire()).toBe(true)
    await user.click(screen.getByRole('button', { name: 'Stop' }))
    await screen.findByTestId('recording-preview')
    expect(fire()).toBe(false)
  })

  it('leaving the page mid-recording stops everything', async () => {
    const user = userEvent.setup()
    const { unmount } = render(<ToastProvider><OnlineRecorder onUse={vi.fn()} /></ToastProvider>)
    await user.click(screen.getByRole('checkbox', { name: /I have told everyone in this meeting/ }))
    await user.click(screen.getByRole('button', { name: 'Start recording' }))
    await screen.findByRole('timer')
    unmount()
    expect(FakeRecorder.instances[0].state).toBe('inactive')
    expect(display.getTracks().every((t) => t.stop.mock.calls.length >= 1)).toBe(true)
    expect(mic.getTracks()[0].stop).toHaveBeenCalled()
  })
})

// ---------------------------------------------------- in the new-meeting form

describe('the Online meeting tab on the new-meeting page', () => {
  async function useOnline(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByRole('button', { name: 'Online meeting' }))
    await startRecording(user)
    await user.click(screen.getByRole('button', { name: 'Stop' }))
    await user.click(await screen.findByRole('button', { name: 'Use this recording' }))
  }
  const routes = () => mockServer({
    ...emptyApp,
    'GET /auth/me': meHandler,
    'POST /api/v1/meetings/upload': () => ({ meeting_id: 'm9' }),
    'POST /api/v1/meetings/m9/process': () => ({ job_id: 'j', state: 'queued', steps: [], status_url: '' }),
  })

  it('uploads it as an online meeting, with the consent that was given', async () => {
    const user = userEvent.setup()
    const { calls } = routes()
    renderApp('/meetings/new')
    await useOnline(user)
    expect(await screen.findByText(/online meeting, recording confirmed with everyone/)).toBeInTheDocument()
    expect((screen.getByLabelText('Title') as HTMLInputElement).value).toMatch(/^Meeting /)
    await user.click(screen.getByRole('button', { name: /Upload and process/ }))
    await waitFor(() => expect(calls.some((c) => c.url === '/api/v1/meetings/upload')).toBe(true))
    const sent = calls.find((c) => c.url === '/api/v1/meetings/upload')!.body as FormData
    expect(sent.get('source')).toBe('online_tab')
    expect(sent.get('recording_consent')).toBe('true')
    expect((sent.get('audio_file') as File).name).toMatch(/^Online meeting .+\.webm$/)
  })

  it('a microphone recording and a chosen file are not labelled as online meetings', async () => {
    const user = userEvent.setup()
    const { calls } = routes()
    renderApp('/meetings/new')
    await user.click(await screen.findByRole('button', { name: 'Online meeting' }))
    await user.click(screen.getByRole('button', { name: 'Upload a file' }))
    const file = new File([new Uint8Array(2000)], 'standup.wav', { type: 'audio/wav' })
    await user.upload(screen.getByTestId('audio-input'), file)
    await user.click(screen.getByRole('button', { name: /Upload and process/ }))
    await waitFor(() => expect(calls.some((c) => c.url === '/api/v1/meetings/upload')).toBe(true))
    const sent = calls.find((c) => c.url === '/api/v1/meetings/upload')!.body as FormData
    expect(sent.get('source')).toBe('upload')
    expect(sent.get('recording_consent')).toBeNull()
  })

  it('choosing a file after an online recording clears the online label and its consent', async () => {
    const user = userEvent.setup()
    const { calls } = routes()
    renderApp('/meetings/new')
    await useOnline(user)
    await screen.findByText(/online meeting, recording confirmed/)
    await user.upload(screen.getByTestId('audio-input'), new File([new Uint8Array(2000)], 'other.wav', { type: 'audio/wav' }))
    expect(screen.queryByText(/online meeting, recording confirmed/)).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /Upload and process/ }))
    await waitFor(() => expect(calls.some((c) => c.url === '/api/v1/meetings/upload')).toBe(true))
    const sent = calls.find((c) => c.url === '/api/v1/meetings/upload')!.body as FormData
    expect(sent.get('source')).toBe('upload')
    expect(sent.get('recording_consent')).toBeNull()
  })

  it('the meeting’s Details say it was an online meeting and when consent was confirmed', async () => {
    mockServer({
      ...emptyApp,
      'GET /auth/me': meHandler,
      '/api/v1/meetings/m1': () => ({
        id: 'm1', title: 'Weekly', status: 'uploaded', scheduled_time: null, end_time: null, duration_minutes: 1, participants: [], owner_email: 'x@y.com',
        owner_name: null, is_owner: true, started_at: null, ended_at: null, has_audio: false, created_at: '2026-11-13T12:00:00', can_write: true,
        has_transcript: false, has_summary: false, action_count: 0, open_action_count: 0, source: 'online_tab', consent_at: '2026-11-13T12:00:00',
      }),
      '/api/v1/meetings/m1/jobs/latest': () => new Response(JSON.stringify({ detail: 'none' }), { status: 404 }),
      '/api/v1/meetings/m1/actions': () => ({ meeting_id: 'm1', action_items: [], count: 0 }),
    })
    renderApp('/meetings/m1?tab=details')
    const row = (await screen.findByText('Recorded from')).closest('tr') as HTMLElement
    expect(within(row).getByText(/An online meeting \(browser tab audio, everyone’s consent confirmed/)).toBeInTheDocument()
  })
})

describe('telling the people in the meeting', () => {
  it('offers a message to paste into the meeting chat', () => {
    setup()
    expect(screen.getByText(/Heads-up: I am recording this meeting/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Copy message' })).toBeInTheDocument()
  })

  it('notifies the chosen members when recording starts, and says who has no account', async () => {
    const user = userEvent.setup()
    const { calls } = mockServer({
      'POST /api/v1/recording-notice': () => ({ notified: ['ann@example.com'], not_registered: ['friend@gmail.com'] }),
    })
    render(<ToastProvider><OnlineRecorder onUse={vi.fn()} members={[{ name: 'Ann', email: 'ann@example.com' }]} /></ToastProvider>)
    await user.type(screen.getByLabelText(/Who is in this meeting/), 'ann@example.com{enter}friend@gmail.com{enter}')
    await startRecording(user)
    expect(await screen.findByText(/Notified: ann@example.com/)).toBeInTheDocument()
    expect(screen.getByText(/No account here for friend@gmail.com/)).toBeInTheDocument()
    expect(calls.find((c) => c.url.endsWith('/recording-notice'))?.body).toEqual({ emails: ['ann@example.com', 'friend@gmail.com'] })
  })

  it('sends nothing when nobody was listed', async () => {
    const user = userEvent.setup()
    const { calls } = mockServer({})
    setup()
    await startRecording(user)
    expect(calls.some((c) => c.url.endsWith('/recording-notice'))).toBe(false)
  })
})
