import { useCallback, useEffect, useRef, useState } from 'react'
import { fmtBytes, fmtClock } from '../lib/format'
import {
  buildMixer,
  captureSupport,
  describeCaptureError,
  NO_AUDIO_HELP,
  SILENCE_WARN_MS,
  SOUND_LEVEL,
  surfaceAdvice,
  surfaceOf,
  type CaptureInfo,
  type Mixer,
  type Surface,
} from '../lib/capture'
import { pickRecordingType } from './Recorder'
import { IconMic } from './icons'
import { Button, Check } from './ui'

type Phase = 'idle' | 'asking' | 'recording' | 'done'

const AUDIO_BITRATE = 64_000 // plenty for speech, and keeps a long meeting well under the upload limit

/**
 * Record an online meeting (Google Meet, Zoom or Teams in a browser tab): the tab's audio, mixed with
 * your microphone. It checks that meeting audio really arrives, and warns if it goes silent.
 */
export default function OnlineRecorder({ onUse }: { onUse: (file: File, info: CaptureInfo) => void }) {
  const support = captureSupport()
  const [phase, setPhase] = useState<Phase>('idle')
  const [consent, setConsent] = useState(false)
  const [useMic, setUseMic] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notes, setNotes] = useState<string[]>([]) // warnings that do not stop the recording
  const [elapsed, setElapsed] = useState(0)
  const [levels, setLevels] = useState({ tab: 0, mic: 0 })
  const [silent, setSilent] = useState({ tab: false, mic: false })
  const [result, setResult] = useState<{ file: File; url: string; info: CaptureInfo } | null>(null)

  const recorder = useRef<MediaRecorder | null>(null)
  const display = useRef<MediaStream | null>(null)
  const micStream = useRef<MediaStream | null>(null)
  const mixer = useRef<Mixer | null>(null)
  const chunks = useRef<Blob[]>([])
  const info = useRef<CaptureInfo>({ source: 'online_tab', tab: true, mic: false, surface: 'unknown' })
  const startedAt = useRef(0)
  const heard = useRef({ tab: 0, mic: 0 }) // last time each source made a sound
  const poll = useRef<ReturnType<typeof setInterval> | null>(null)
  const resultUrl = useRef<string | null>(null)
  const type = useRef(pickRecordingType())

  const release = useCallback(() => {
    if (poll.current) clearInterval(poll.current)
    poll.current = null
    mixer.current?.close()
    mixer.current = null
    for (const s of [display.current, micStream.current]) s?.getTracks().forEach((t) => t.stop())
    display.current = null
    micStream.current = null
  }, [])

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

  useEffect(() => {
    if (phase !== 'recording') return
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [phase])

  function fail(message: string) {
    release()
    setError(message)
    setPhase('idle')
  }

  async function start() {
    setError(null)
    setNotes([])
    if (!consent) return setError('Confirm that everyone in the meeting knows it is being recorded.')
    setPhase('asking')

    let shared: MediaStream
    try {
      // Video is required to share a tab, but only a tiny, slow picture is asked for; it is never recorded.
      shared = await navigator.mediaDevices.getDisplayMedia({
        video: { frameRate: { max: 2 }, width: { max: 640 }, height: { max: 360 } },
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
        systemAudio: 'include',
        selfBrowserSurface: 'exclude',
        surfaceSwitching: 'include',
      } as DisplayMediaStreamOptions)
    } catch (e) {
      return fail(describeCaptureError(e))
    }
    display.current = shared

    // 1. Did meeting audio actually come with the share?
    const audioTracks = shared.getAudioTracks()
    if (audioTracks.length === 0) return fail(NO_AUDIO_HELP)
    // 2. What was shared: a tab (best), or something else (warn)?
    const surface: Surface = surfaceOf(shared.getVideoTracks()[0]?.getSettings())
    const advice = surfaceAdvice(surface)
    const extra: string[] = advice ? [advice] : []

    // 3. Your own voice is not in the tab's audio, so bring the microphone in too.
    let mic: MediaStream | null = null
    if (useMic) {
      try {
        mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })
        micStream.current = mic
      } catch {
        extra.push('Your microphone could not be used, so only the other people in the meeting will be recorded, not your own voice.')
      }
    } else {
      extra.push('Your microphone is off, so your own voice will not be recorded.')
    }

    let mix: Mixer
    try {
      mix = buildMixer(new MediaStream(audioTracks), mic)
    } catch (e) {
      return fail(e instanceof Error ? e.message : 'Could not start mixing the audio.')
    }
    mixer.current = mix

    try {
      const opts: MediaRecorderOptions = { audioBitsPerSecond: AUDIO_BITRATE }
      if (type.current.mime) opts.mimeType = type.current.mime
      const rec = new MediaRecorder(mix.stream, opts)
      chunks.current = []
      rec.ondataavailable = (ev) => ev.data.size > 0 && chunks.current.push(ev.data)
      rec.onerror = () => fail('Recording stopped unexpectedly.')
      rec.onstop = () => {
        release()
        const blob = new Blob(chunks.current, { type: rec.mimeType || type.current.mime || 'audio/webm' })
        if (blob.size < 1000) return fail('Nothing was recorded. Check that the meeting is playing sound, then try again.')
        const stamp = new Date().toLocaleString([], { day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit' }).replace(/[:,]/g, '.')
        const file = new File([blob], `Online meeting ${stamp}${type.current.ext}`, { type: blob.type })
        if (resultUrl.current) URL.revokeObjectURL(resultUrl.current)
        resultUrl.current = URL.createObjectURL(blob)
        setResult({ file, url: resultUrl.current, info: { ...info.current } })
        setPhase('done')
      }
      info.current = { source: 'online_tab', tab: true, mic: !!mic, surface }
      recorder.current = rec

      // Stopping the share (the browser's own "Stop sharing" button) ends the recording cleanly.
      const ended = () => recorder.current && recorder.current.state !== 'inactive' && recorder.current.stop()
      audioTracks[0].addEventListener('ended', ended)
      shared.getVideoTracks()[0]?.addEventListener('ended', ended)

      startedAt.current = Date.now()
      heard.current = { tab: 0, mic: 0 }
      rec.start(1000)
      setNotes(extra)
      setElapsed(0)
      setSilent({ tab: false, mic: false })
      poll.current = setInterval(() => {
        const now = Date.now()
        const tab = mix.tabLevel()
        const m = mix.micLevel()
        if (tab > SOUND_LEVEL) heard.current.tab = now
        if (m > SOUND_LEVEL) heard.current.mic = now
        setLevels({ tab, mic: m })
        setElapsed((now - startedAt.current) / 1000)
        // Silence for a while: probably the audio box was not ticked, or nobody is talking yet.
        setSilent({
          tab: now - Math.max(heard.current.tab, startedAt.current) > SILENCE_WARN_MS,
          mic: !!mic && now - Math.max(heard.current.mic, startedAt.current) > SILENCE_WARN_MS,
        })
      }, 150)
      setPhase('recording')
    } catch (e) {
      fail(describeCaptureError(e))
    }
  }

  function stop() {
    if (recorder.current && recorder.current.state !== 'inactive') recorder.current.stop()
  }

  function again() {
    if (resultUrl.current) URL.revokeObjectURL(resultUrl.current)
    resultUrl.current = null
    setResult(null)
    setElapsed(0)
    setPhase('idle')
  }

  if (!support.ok) {
    return (
      <div className="recorder" role="status">
        <p style={{ margin: 0 }}>{support.reason}</p>
        <p className="muted small" style={{ marginBottom: 0 }}>
          You can still upload a recording of the meeting instead (Zoom and Teams can save one; Google Meet saves it to Drive).
        </p>
      </div>
    )
  }

  return (
    <div className="recorder">
      {error && (
        <div className="alert danger" role="alert">
          {error}
        </div>
      )}

      {phase === 'idle' && (
        <div className="stack">
          <div className="stack tight" style={{ alignItems: 'center', textAlign: 'center' }}>
            <span style={{ color: 'var(--accent)' }}>
              <IconMic size={30} />
            </span>
            <strong>Record a Google Meet, Zoom or Teams meeting that is open in another tab</strong>
          </div>
          <ol className="small" style={{ margin: 0, paddingLeft: '1.2rem' }}>
            <li>Open the meeting in its own Chrome or Edge tab (Zoom and Teams: use “join from your browser”).</li>
            <li>Click <strong>Start recording</strong> here and choose the <strong>Chrome Tab</strong> with your meeting.</li>
            <li>Tick <strong>“Also share tab audio”</strong> at the bottom of the dialog, then press Share.</li>
          </ol>
          <p className="muted small" style={{ margin: 0 }}>
            Use headphones. Without them your microphone also hears the speakers and the other people are recorded twice. The Zoom and Teams desktop apps are not supported here; upload their recordings instead.
          </p>
          <Check checked={useMic} onChange={setUseMic}>
            Also record my microphone (so my own voice is included)
          </Check>
          <Check checked={consent} onChange={setConsent}>
            Everyone in this meeting knows it is being recorded
          </Check>
          <div>
            <Button variant="primary" type="button" onClick={() => void start()} disabled={!consent}>
              Start recording
            </Button>
          </div>
        </div>
      )}

      {phase === 'asking' && (
        <div className="stack tight" style={{ alignItems: 'center' }} role="status">
          <strong>Waiting for you to choose the meeting tab…</strong>
          <span className="muted small">In the browser’s dialog, pick the tab, tick “Also share tab audio”, and press Share.</span>
        </div>
      )}

      {phase === 'recording' && (
        <div className="stack" style={{ alignItems: 'stretch' }}>
          <div className="row" role="timer" aria-label="Recording time" style={{ justifyContent: 'center' }}>
            <span className="rec-dot" aria-hidden="true" />
            <strong style={{ fontSize: '1.6rem', fontVariantNumeric: 'tabular-nums' }}>{fmtClock(elapsed)}</strong>
            <span className="muted small">Recording the meeting</span>
          </div>

          <div className="stack tight">
            <div className="meter-row">
              <span id="lbl-tab" className="small">Meeting audio</span>
              <div className="meter" role="meter" aria-labelledby="lbl-tab" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(levels.tab * 100)}>
                <span style={{ width: `${Math.round(levels.tab * 100)}%` }} />
              </div>
            </div>
            {info.current.mic && (
              <div className="meter-row">
                <span id="lbl-mic" className="small">Your microphone</span>
                <div className="meter" role="meter" aria-labelledby="lbl-mic" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(levels.mic * 100)}>
                  <span style={{ width: `${Math.round(levels.mic * 100)}%` }} />
                </div>
              </div>
            )}
          </div>

          {silent.tab && (
            <div className="alert warn" role="alert">
              No meeting audio has been heard for a while. Is someone speaking? If people are talking, press Stop and start again, making sure to tick “Also share tab audio”.
            </div>
          )}
          {silent.mic && !silent.tab && (
            <div className="alert warn" role="alert">
              Your microphone is silent. If you are speaking, check that it is not muted or that the right microphone is selected.
            </div>
          )}
          {notes.map((n) => (
            <div key={n} className="alert warn" role="status">
              {n}
            </div>
          ))}
          <div className="row" style={{ justifyContent: 'center' }}>
            <Button variant="danger" type="button" onClick={stop}>
              Stop
            </Button>
          </div>
          <p className="muted small" style={{ margin: 0, textAlign: 'center' }}>
            Keep this tab open while recording. You can switch to the meeting tab.
          </p>
        </div>
      )}

      {phase === 'done' && result && (
        <div className="stack" style={{ alignItems: 'stretch' }}>
          <strong>Recorded {fmtClock(elapsed)}</strong>
          <audio controls src={result.url} data-testid="recording-preview" style={{ width: '100%' }} />
          <span className="muted small">
            {result.file.name} · {fmtBytes(result.file.size)} · {result.info.mic ? 'meeting audio and your microphone' : 'meeting audio only'}
          </span>
          <div className="row">
            <Button variant="primary" type="button" onClick={() => onUse(result.file, result.info)}>
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
