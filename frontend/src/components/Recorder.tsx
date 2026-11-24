import { useCallback, useEffect, useRef, useState } from 'react'
import { fmtBytes, fmtClock } from '../lib/format'
import { IconMic } from './icons'
import { Button } from './ui'

type Phase = 'idle' | 'asking' | 'recording' | 'paused' | 'done'

/** The first container this browser can record: Chrome/Firefox give WebM/Ogg, Safari MP4. */
export function pickRecordingType(): { mime: string; ext: string } {
  const candidates: [string, string][] = [
    ['audio/webm;codecs=opus', '.webm'],
    ['audio/webm', '.webm'],
    ['audio/ogg;codecs=opus', '.ogg'],
    ['audio/mp4', '.m4a'],
  ]
  for (const [mime, ext] of candidates) {
    if (typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported?.(mime)) return { mime, ext }
  }
  return { mime: '', ext: '.webm' } // let the browser choose; WebM is what Chromium/Firefox produce
}

export function recordingSupported(): boolean {
  return typeof MediaRecorder !== 'undefined' && !!navigator.mediaDevices?.getUserMedia
}

function micError(e: unknown): string {
  const name = e instanceof DOMException ? e.name : ''
  if (name === 'NotAllowedError' || name === 'SecurityError')
    return 'Microphone access was blocked. Allow it for this site in your browser’s address bar, then try again.'
  if (name === 'NotFoundError' || name === 'OverconstrainedError') return 'No microphone was found. Plug one in and try again.'
  if (name === 'NotReadableError') return 'The microphone is in use by another app. Close it and try again.'
  return e instanceof Error ? e.message : 'Could not start recording.'
}

/** Record from the microphone; hands back an audio File the upload form can send. */
export default function Recorder({ onUse }: { onUse: (file: File) => void }) {
  const [phase, setPhase] = useState<Phase>('idle')
  const [error, setError] = useState<string | null>(null)
  const [elapsed, setElapsed] = useState(0)
  const [level, setLevel] = useState(0)
  const [result, setResult] = useState<{ file: File; url: string } | null>(null)

  const recorder = useRef<MediaRecorder | null>(null)
  const stream = useRef<MediaStream | null>(null)
  const chunks = useRef<Blob[]>([])
  const type = useRef(pickRecordingType())
  const startedAt = useRef(0) // ms timestamp of the current running stretch
  const banked = useRef(0) // ms recorded before the last pause
  const audioCtx = useRef<AudioContext | null>(null)
  const raf = useRef(0)
  const timer = useRef<ReturnType<typeof setInterval> | null>(null)
  const resultUrl = useRef<string | null>(null)

  const stopMeter = useCallback(() => {
    cancelAnimationFrame(raf.current)
    void audioCtx.current?.close().catch(() => undefined)
    audioCtx.current = null
    setLevel(0)
  }, [])

  const release = useCallback(() => {
    if (timer.current) clearInterval(timer.current)
    timer.current = null
    stopMeter()
    stream.current?.getTracks().forEach((t) => t.stop())
    stream.current = null
  }, [stopMeter])

  // Leaving the page mid-recording must not leave the microphone open.
  useEffect(
    () => () => {
      const r = recorder.current
      if (r && r.state !== 'inactive') {
        r.onstop = null
        try {
          r.stop()
        } catch {
          /* already stopped */
        }
      }
      release()
      if (resultUrl.current) URL.revokeObjectURL(resultUrl.current)
    },
    [release],
  )

  // Warn before a reload/close would throw the recording away.
  useEffect(() => {
    if (phase !== 'recording' && phase !== 'paused') return
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [phase])

  function startMeter(s: MediaStream) {
    try {
      const Ctx = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
      if (!Ctx) return
      const ctx = new Ctx()
      const analyser = ctx.createAnalyser()
      analyser.fftSize = 512
      ctx.createMediaStreamSource(s).connect(analyser)
      audioCtx.current = ctx
      const data = new Uint8Array(analyser.fftSize)
      const tick = () => {
        analyser.getByteTimeDomainData(data)
        let peak = 0
        for (const v of data) peak = Math.max(peak, Math.abs(v - 128))
        setLevel(Math.min(1, peak / 90))
        raf.current = requestAnimationFrame(tick)
      }
      tick()
    } catch {
      /* the level meter is a nicety */
    }
  }

  const tickClock = () => setElapsed((banked.current + (Date.now() - startedAt.current)) / 1000)

  async function start() {
    setError(null)
    setPhase('asking')
    let s: MediaStream
    try {
      s = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })
    } catch (e) {
      setError(micError(e))
      setPhase('idle')
      return
    }
    try {
      const rec = type.current.mime ? new MediaRecorder(s, { mimeType: type.current.mime }) : new MediaRecorder(s)
      chunks.current = []
      rec.ondataavailable = (ev) => ev.data.size > 0 && chunks.current.push(ev.data)
      rec.onerror = () => {
        setError('Recording stopped unexpectedly.')
        release()
        setPhase('idle')
      }
      rec.onstop = () => {
        release()
        const blob = new Blob(chunks.current, { type: rec.mimeType || type.current.mime || 'audio/webm' })
        if (blob.size < 1000) {
          setError('Nothing was recorded. Check your microphone and try again.')
          setPhase('idle')
          return
        }
        const stamp = new Date().toLocaleString([], { day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit' }).replace(/[:,]/g, '.')
        const file = new File([blob], `Recording ${stamp}${type.current.ext}`, { type: blob.type })
        if (resultUrl.current) URL.revokeObjectURL(resultUrl.current)
        resultUrl.current = URL.createObjectURL(blob)
        setResult({ file, url: resultUrl.current })
        setPhase('done')
      }
      stream.current = s
      recorder.current = rec
      banked.current = 0
      startedAt.current = Date.now()
      setElapsed(0)
      rec.start(1000) // a chunk per second, so a crash loses at most a second
      timer.current = setInterval(tickClock, 250)
      startMeter(s)
      setPhase('recording')
    } catch (e) {
      s.getTracks().forEach((t) => t.stop())
      setError(micError(e))
      setPhase('idle')
    }
  }

  function pause() {
    recorder.current?.pause()
    banked.current += Date.now() - startedAt.current
    if (timer.current) clearInterval(timer.current)
    setElapsed(banked.current / 1000)
    setPhase('paused')
  }

  function resume() {
    recorder.current?.resume()
    startedAt.current = Date.now()
    timer.current = setInterval(tickClock, 250)
    setPhase('recording')
  }

  function stop() {
    if (phase === 'recording') banked.current += Date.now() - startedAt.current
    setElapsed(banked.current / 1000)
    recorder.current?.stop()
  }

  function again() {
    if (resultUrl.current) URL.revokeObjectURL(resultUrl.current)
    resultUrl.current = null
    setResult(null)
    setElapsed(0)
    setPhase('idle')
  }

  if (!recordingSupported()) {
    return (
      <div className="recorder" role="status">
        <p style={{ margin: 0 }}>
          This browser cannot record audio. Use a current Chrome, Edge, Firefox or Safari, open the app at <code>http://localhost</code>, or upload a file instead.
        </p>
      </div>
    )
  }

  const live = phase === 'recording' || phase === 'paused'

  return (
    <div className="recorder">
      {error && (
        <div className="alert danger" role="alert">
          {error}
        </div>
      )}

      {phase === 'idle' && (
        <div className="stack tight" style={{ alignItems: 'center', textAlign: 'center' }}>
          <span style={{ color: 'var(--accent)' }}>
            <IconMic size={30} />
          </span>
          <strong>Record the meeting from your microphone</strong>
          <span className="muted small">Your browser will ask for microphone access. The audio stays on this computer until you upload it.</span>
          <Button variant="primary" type="button" onClick={() => void start()}>
            Start recording
          </Button>
        </div>
      )}

      {phase === 'asking' && (
        <div className="stack tight" style={{ alignItems: 'center' }} role="status">
          <strong>Waiting for microphone permission…</strong>
          <span className="muted small">Choose “Allow” in the browser prompt.</span>
        </div>
      )}

      {live && (
        <div className="stack" style={{ alignItems: 'center' }}>
          <div className="row" role="timer" aria-label="Recording time">
            <span className={`rec-dot ${phase === 'paused' ? 'paused' : ''}`} aria-hidden="true" />
            <strong style={{ fontSize: '1.6rem', fontVariantNumeric: 'tabular-nums' }}>{fmtClock(elapsed)}</strong>
            <span className="muted small">{phase === 'paused' ? 'Paused' : 'Recording'}</span>
          </div>
          <div className="meter" aria-hidden="true">
            <span style={{ width: `${Math.round(level * 100)}%` }} />
          </div>
          <div className="row">
            {phase === 'recording' ? (
              <Button type="button" onClick={pause}>
                Pause
              </Button>
            ) : (
              <Button type="button" onClick={resume}>
                Resume
              </Button>
            )}
            <Button variant="danger" type="button" onClick={stop}>
              Stop
            </Button>
          </div>
        </div>
      )}

      {phase === 'done' && result && (
        <div className="stack" style={{ alignItems: 'stretch' }}>
          <strong>Recorded {fmtClock(elapsed)}</strong>
          <audio controls src={result.url} data-testid="recording-preview" style={{ width: '100%' }} />
          <span className="muted small">
            {result.file.name} · {fmtBytes(result.file.size)}
          </span>
          <div className="row">
            <Button variant="primary" type="button" onClick={() => onUse(result.file)}>
              Use this recording
            </Button>
            <Button type="button" onClick={again}>
              Record again
            </Button>
            <a className="btn" href={result.url} download={result.file.name}>
              Save a copy
            </a>
          </div>
        </div>
      )}
    </div>
  )
}
