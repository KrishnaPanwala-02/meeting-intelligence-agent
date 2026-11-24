// Capturing an online meeting from a browser tab: checks, advice and the audio mixer.
//
// The shared tab carries only what the OTHER participants say (your own microphone goes to the
// meeting service, not to the tab), so the recorder mixes the tab's audio with your microphone.

export type Surface = 'tab' | 'window' | 'screen' | 'unknown'

/** Is this a Chromium browser (Chrome, Edge, Brave, Opera)? Only those can share a tab's sound. */
export function isChromium(): boolean {
  const brands = (navigator as unknown as { userAgentData?: { brands?: { brand: string }[] } }).userAgentData?.brands
  if (brands?.length) return brands.some((b) => /Chromium|Google Chrome|Microsoft Edge/i.test(b.brand))
  const ua = navigator.userAgent ?? ''
  return /Chrome\//.test(ua) && !/Firefox\//.test(ua)
}

export interface Support {
  ok: boolean
  /** Why not, or a warning when it may not work. */
  reason?: string
}

export function captureSupport(): Support {
  if (typeof MediaRecorder === 'undefined' || !navigator.mediaDevices?.getDisplayMedia)
    return { ok: false, reason: 'This browser cannot share a tab or record audio. Use Google Chrome or Microsoft Edge on a computer.' }
  if (!isChromium())
    return { ok: false, reason: 'Only Chrome and Edge can share a tab’s sound. Firefox and Safari share the picture without the audio. Open this page in Chrome or Edge.' }
  return { ok: true }
}

/** What the browser says was shared: a tab ("browser"), a window, or the whole screen ("monitor"). */
export function surfaceOf(settings: MediaTrackSettings | undefined): Surface {
  const s = (settings as { displaySurface?: string } | undefined)?.displaySurface
  return s === 'browser' ? 'tab' : s === 'window' ? 'window' : s === 'monitor' ? 'screen' : 'unknown'
}

/** A note for anything other than a tab (null when a tab was shared, which is what we want). */
export function surfaceAdvice(surface: Surface): string | null {
  if (surface === 'tab') return null
  if (surface === 'screen')
    return 'You shared your whole screen. That records everything your computer plays (every app and notification), not just the meeting. Sharing only the meeting tab is safer and more accurate.'
  if (surface === 'window') return 'You shared a window. Chrome cannot capture sound from a window, so choose the meeting’s browser tab instead.'
  return 'The browser did not say what was shared. Check below that the “Meeting audio” meter moves when someone speaks.'
}

export const NO_AUDIO_HELP =
  'No audio was shared, so only your microphone would be recorded. Start again, choose the Chrome Tab that has your meeting, and tick “Also share tab audio” at the bottom of the dialog.'

export const SILENCE_WARN_MS = 10_000
export const SOUND_LEVEL = 0.02 // above this peak (0-1) counts as sound

/** Loudness of what an analyser hears right now, 0 (silence) to 1. */
export function peakLevel(analyser: { fftSize: number; getByteTimeDomainData: (a: Uint8Array) => void }): number {
  const data = new Uint8Array(analyser.fftSize)
  analyser.getByteTimeDomainData(data)
  let peak = 0
  for (const v of data) peak = Math.max(peak, Math.abs(v - 128))
  return Math.min(1, peak / 128)
}

export interface Mixer {
  /** The mixed audio to record. */
  stream: MediaStream
  tabLevel: () => number
  micLevel: () => number
  close: () => void
}

/** Mix the tab's audio and (optionally) the microphone into one stream; the tab is never played aloud again. */
export function buildMixer(tabAudio: MediaStream, mic: MediaStream | null): Mixer {
  const Ctx = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
  if (!Ctx) throw new Error('This browser cannot mix audio. Use a current Chrome or Edge.')
  const ctx = new Ctx()
  const out = ctx.createMediaStreamDestination()

  const tab = ctx.createMediaStreamSource(tabAudio)
  const tabAnalyser = ctx.createAnalyser()
  tabAnalyser.fftSize = 1024
  tab.connect(tabAnalyser)
  tab.connect(out)

  let micAnalyser: AnalyserNode | null = null
  if (mic) {
    const m = ctx.createMediaStreamSource(mic)
    micAnalyser = ctx.createAnalyser()
    micAnalyser.fftSize = 1024
    m.connect(micAnalyser)
    m.connect(out)
  }
  void ctx.resume?.().catch?.(() => undefined)
  return {
    stream: out.stream,
    tabLevel: () => peakLevel(tabAnalyser),
    micLevel: () => (micAnalyser ? peakLevel(micAnalyser) : 0),
    close: () => void ctx.close?.().catch?.(() => undefined),
  }
}

export interface CaptureInfo {
  source: 'online_tab'
  /** The meeting tab's audio was captured. */
  tab: boolean
  /** The user's microphone was mixed in. */
  mic: boolean
  surface: Surface
}

export function describeCaptureError(e: unknown): string {
  const name = e instanceof DOMException ? e.name : ''
  if (name === 'NotAllowedError' || name === 'AbortError')
    return 'Sharing was cancelled or blocked. Click “Start recording” again, then choose the meeting tab and press Share.'
  if (name === 'NotFoundError') return 'There is nothing to share. Open your meeting in another tab first.'
  if (name === 'NotReadableError') return 'The browser could not read the shared tab. Close other screen-sharing and try again.'
  return e instanceof Error ? e.message : 'Could not start capturing the meeting.'
}
