import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { onSessionLost, tokens } from '../src/api/client'
import Recorder, { pickRecordingType } from '../src/components/Recorder'
import { emptyApp, jsonResponse, meHandler, mockServer, pair, renderApp } from './helpers'

/** A recorder that "captures" 5 000 bytes of audio when stopped. */
class FakeRecorder {
  static instances: FakeRecorder[] = []
  static supported = new Set(['audio/webm;codecs=opus', 'audio/webm'])
  static isTypeSupported = (m: string) => FakeRecorder.supported.has(m)
  static emptyOnStop = false
  state: 'inactive' | 'recording' | 'paused' = 'inactive'
  mimeType: string
  ondataavailable: ((e: { data: Blob }) => void) | null = null
  onstop: (() => void) | null = null
  onerror: (() => void) | null = null
  constructor(public stream: MediaStream, opts?: { mimeType?: string }) {
    this.mimeType = opts?.mimeType ?? 'audio/webm'
    FakeRecorder.instances.push(this)
  }
  start() { this.state = 'recording' }
  pause() { this.state = 'paused' }
  resume() { this.state = 'recording' }
  stop() {
    this.state = 'inactive'
    this.ondataavailable?.({ data: new Blob([new Uint8Array(FakeRecorder.emptyOnStop ? 10 : 5000)], { type: this.mimeType }) })
    this.onstop?.()
  }
}

const track = { stop: vi.fn() }
const getUserMedia = vi.fn()

beforeEach(() => {
  tokens.clear()
  onSessionLost(null)
  FakeRecorder.instances = []
  FakeRecorder.emptyOnStop = false
  track.stop.mockClear()
  getUserMedia.mockReset().mockResolvedValue({ getTracks: () => [track] })
  vi.stubGlobal('MediaRecorder', FakeRecorder)
  Object.defineProperty(navigator, 'mediaDevices', { value: { getUserMedia }, configurable: true })
  URL.createObjectURL = vi.fn(() => 'blob:preview')
  URL.revokeObjectURL = vi.fn()
})
afterEach(() => {
  vi.unstubAllGlobals()
})

describe('pickRecordingType', () => {
  it('prefers WebM/Opus, and falls back to MP4 (Safari)', () => {
    expect(pickRecordingType()).toEqual({ mime: 'audio/webm;codecs=opus', ext: '.webm' })
    FakeRecorder.supported = new Set(['audio/mp4'])
    expect(pickRecordingType()).toEqual({ mime: 'audio/mp4', ext: '.m4a' })
    FakeRecorder.supported = new Set(['audio/webm;codecs=opus', 'audio/webm'])
  })
})

describe('Recorder', () => {
  it('records, pauses, resumes, stops, previews and hands back an audio file', async () => {
    const user = userEvent.setup()
    const onUse = vi.fn()
    render(<Recorder onUse={onUse} />)

    await user.click(screen.getByRole('button', { name: 'Start recording' }))
    expect(getUserMedia).toHaveBeenCalledWith({ audio: expect.objectContaining({ echoCancellation: true }) })
    expect(await screen.findByRole('timer')).toBeInTheDocument()
    expect(screen.getByText('Recording')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Pause' }))
    expect(screen.getByText('Paused')).toBeInTheDocument()
    expect(FakeRecorder.instances[0].state).toBe('paused')
    await user.click(screen.getByRole('button', { name: 'Resume' }))
    expect(FakeRecorder.instances[0].state).toBe('recording')

    await user.click(screen.getByRole('button', { name: 'Stop' }))
    expect(await screen.findByTestId('recording-preview')).toHaveAttribute('src', 'blob:preview')
    expect(track.stop).toHaveBeenCalled() // the microphone is released

    await user.click(screen.getByRole('button', { name: 'Use this recording' }))
    expect(onUse).toHaveBeenCalledOnce()
    const file = onUse.mock.calls[0][0] as File
    expect(file.name).toMatch(/^Recording .+\.webm$/)
    expect(file.size).toBe(5000)
    expect(file.type).toContain('audio/webm')
  })

  it('"Record again" discards the take and starts over', async () => {
    const user = userEvent.setup()
    render(<Recorder onUse={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: 'Start recording' }))
    await user.click(await screen.findByRole('button', { name: 'Stop' }))
    await user.click(await screen.findByRole('button', { name: 'Record again' }))
    expect(screen.getByRole('button', { name: 'Start recording' })).toBeInTheDocument()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:preview')
  })

  it('explains a blocked microphone and can be retried', async () => {
    const user = userEvent.setup()
    getUserMedia.mockRejectedValueOnce(new DOMException('denied', 'NotAllowedError'))
    render(<Recorder onUse={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: 'Start recording' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/blocked/i)
    expect(FakeRecorder.instances).toHaveLength(0)
    await user.click(screen.getByRole('button', { name: 'Start recording' }))
    expect(await screen.findByRole('timer')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('explains a missing or busy microphone', async () => {
    const user = userEvent.setup()
    getUserMedia.mockRejectedValueOnce(new DOMException('none', 'NotFoundError'))
    render(<Recorder onUse={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: 'Start recording' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/No microphone/)
    getUserMedia.mockRejectedValueOnce(new DOMException('busy', 'NotReadableError'))
    await user.click(screen.getByRole('button', { name: 'Start recording' }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/in use by another app/))
  })

  it('offers a copy of the take to save to disk, so a failed upload never loses it', async () => {
    const user = userEvent.setup()
    render(<Recorder onUse={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: 'Start recording' }))
    await user.click(await screen.findByRole('button', { name: 'Stop' }))
    const link = await screen.findByRole('link', { name: 'Save a copy' })
    expect(link).toHaveAttribute('href', 'blob:preview')
    expect(link.getAttribute('download')).toMatch(/^Recording .+\.webm$/)
  })

  it('refuses an empty recording', async () => {
    const user = userEvent.setup()
    FakeRecorder.emptyOnStop = true
    const onUse = vi.fn()
    render(<Recorder onUse={onUse} />)
    await user.click(screen.getByRole('button', { name: 'Start recording' }))
    await user.click(await screen.findByRole('button', { name: 'Stop' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/Nothing was recorded/)
    expect(screen.queryByRole('button', { name: 'Use this recording' })).not.toBeInTheDocument()
  })

  it('releases the microphone if the page is left mid-recording', async () => {
    const user = userEvent.setup()
    const { unmount } = render(<Recorder onUse={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: 'Start recording' }))
    await screen.findByRole('timer')
    unmount()
    expect(track.stop).toHaveBeenCalled()
    expect(FakeRecorder.instances[0].state).toBe('inactive')
  })

  it('warns before the page is closed while recording, but not otherwise', async () => {
    const user = userEvent.setup()
    render(<Recorder onUse={vi.fn()} />)
    const fire = () => {
      const e = new Event('beforeunload', { cancelable: true })
      window.dispatchEvent(e)
      return e.defaultPrevented
    }
    expect(fire()).toBe(false)
    await user.click(screen.getByRole('button', { name: 'Start recording' }))
    await screen.findByRole('timer')
    expect(fire()).toBe(true)
    await user.click(screen.getByRole('button', { name: 'Stop' }))
    await screen.findByTestId('recording-preview')
    expect(fire()).toBe(false)
  })

  it('says so when the browser cannot record', () => {
    vi.stubGlobal('MediaRecorder', undefined)
    render(<Recorder onUse={vi.fn()} />)
    expect(screen.getByRole('status')).toHaveTextContent(/cannot record audio/)
    expect(screen.queryByRole('button', { name: 'Start recording' })).not.toBeInTheDocument()
  })
})

describe('recording from the new-meeting page', () => {
  it('uses the recording as the file to upload', async () => {
    tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
    const user = userEvent.setup()
    const { calls } = mockServer({
      ...emptyApp,
      'GET /auth/me': meHandler,
      'POST /api/v1/meetings/upload': () => ({ meeting_id: 'm9' }),
      'POST /api/v1/meetings/m9/process': () => ({ job_id: 'j1', state: 'queued', steps: [], status_url: '' }),
      '/api/v1/meetings/m9': () => jsonResponse({ detail: 'stop here' }, 404),
    })
    renderApp('/meetings/new')
    const submit = await screen.findByRole('button', { name: /Upload and process/ })
    expect(submit).toBeDisabled() // nothing chosen yet

    await user.click(screen.getByRole('button', { name: 'Record now' }))
    await user.click(screen.getByRole('button', { name: 'Start recording' }))
    await user.click(await screen.findByRole('button', { name: 'Stop' }))
    await user.click(await screen.findByRole('button', { name: 'Use this recording' }))

    expect(await screen.findByText(/^Recording .+\.webm$/)).toBeInTheDocument()
    expect((screen.getByLabelText('Title') as HTMLInputElement).value).toMatch(/^Meeting /)
    expect(screen.getByRole('button', { name: /Upload and process/ })).toBeEnabled()

    await user.click(screen.getByRole('button', { name: /Upload and process/ }))
    await waitFor(() => expect(calls.some((c) => c.url === '/api/v1/meetings/upload')).toBe(true))
    const sent = calls.find((c) => c.url === '/api/v1/meetings/upload')!.body as FormData
    expect((sent.get('audio_file') as File).name).toMatch(/\.webm$/)
    expect(sent.get('title')).toMatch(/^Meeting /)
    await waitFor(() => expect(calls.some((c) => c.url === '/api/v1/meetings/m9/process')).toBe(true))
  })
})
