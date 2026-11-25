import { useMemo, useRef, useState, type DragEvent, type FormEvent } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { calendarApi, meetingsApi } from '../api/endpoints'
import type { ConflictInfo, OptimalTime, ScheduleResult } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { IconSparkle, IconUpload } from '../components/icons'
import OnlineRecorder from '../components/OnlineRecorder'
import Recorder from '../components/Recorder'
import type { CaptureInfo } from '../lib/capture'
import { Button, Card, ChipInput, Check, ErrorBanner, Modal, Tabs, TextField, useToast } from '../components/ui'
import { dayKey, fmtBytes, fmtDateTime, fromWallClock, toApiWallClock, toLocalInput } from '../lib/format'
import { MAX_OCCURRENCES, occurrences, REPEAT_OPTIONS, type Repeat } from '../lib/recurrence'
import { allZones, browserZone } from '../lib/zone'
import { useMembers } from '../lib/useMembers'
import FolderPicker, { folderChoiceError, folderFields } from '../components/FolderPicker'
import type { FolderChoice } from '../api/types'

const AUDIO_EXT = ['.wav', '.mp3', '.m4a', '.ogg', '.flac', '.webm', '.mp4']

const LANGUAGES = [
  { v: 'en', l: 'English' },
  { v: 'auto', l: 'Detect automatically' },
  { v: 'hi', l: 'Hindi' },
  { v: 'es', l: 'Spanish' },
  { v: 'fr', l: 'French' },
  { v: 'de', l: 'German' },
  { v: 'pt', l: 'Portuguese' },
  { v: 'it', l: 'Italian' },
  { v: 'ja', l: 'Japanese' },
  { v: 'zh', l: 'Chinese' },
]

const DURATIONS = [15, 30, 45, 60, 90, 120, 180]

type Mode = 'upload' | 'schedule'

export default function NewMeetingPage() {
  const [params, setParams] = useSearchParams()
  const mode: Mode = params.get('mode') === 'schedule' ? 'schedule' : 'upload'
  return (
    <>
      <div className="page-head">
        <div>
          <h1>New meeting</h1>
          <p>Upload a recording to get a transcript, summary and tasks — or schedule a future meeting.</p>
        </div>
      </div>
      <Tabs<Mode>
        tabs={[
          { id: 'upload', label: 'Upload a recording' },
          { id: 'schedule', label: 'Schedule a meeting' },
        ]}
        value={mode}
        onChange={(m) => setParams(m === 'upload' ? {} : { mode: m }, { replace: true })}
      />
      {mode === 'upload' ? <UploadForm initialFolder={params.get('folder')} /> : <ScheduleForm initialDate={params.get('date')} initialFolder={params.get('folder')} />}
    </>
  )
}

/* ───────────────────────── Upload ───────────────────────── */

function UploadForm({ initialFolder }: { initialFolder: string | null }) {
  const nav = useNavigate()
  const toast = useToast()
  const input = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File | null>(null)
  const [source, setSource] = useState<'file' | 'record' | 'online'>('file')
  // How the chosen audio was made: sent with the upload, with the consent for an online meeting.
  const [origin, setOrigin] = useState<'upload' | 'browser_mic' | 'online_tab'>('upload')
  const [over, setOver] = useState(false)
  const [title, setTitle] = useState('')
  const [people, setPeople] = useState<string[]>([])
  const members = useMembers()
  const [folder, setFolder] = useState<FolderChoice>(initialFolder ? { folder_id: initialFolder } : {})
  const [language, setLanguage] = useState('en')
  const [process, setProcess] = useState(true)
  const [speakers, setSpeakers] = useState(true)
  const [busy, setBusy] = useState<'upload' | 'process' | null>(null)
  const [error, setError] = useState<string | null>(null)

  function choose(f: File | undefined | null) {
    setError(null)
    if (!f) return
    const ext = f.name.slice(f.name.lastIndexOf('.')).toLowerCase()
    if (!AUDIO_EXT.includes(ext)) {
      setError(`Unsupported file type “${ext || f.name}”. Use ${AUDIO_EXT.join(', ')}.`)
      return
    }
    setFile(f)
    setOrigin('upload')
    if (!title.trim()) setTitle(f.name.replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' ').trim())
  }

  function useRecording(f: File, info?: CaptureInfo) {
    setError(null)
    setFile(f)
    setOrigin(info ? 'online_tab' : 'browser_mic')
    setSource('file')
    if (!title.trim()) setTitle(`Meeting ${f.name.replace(/^Recording\s*/, '').replace(/\.[^.]+$/, '')}`.trim())
  }

  const onDrop = (e: DragEvent) => {
    e.preventDefault()
    setOver(false)
    choose(e.dataTransfer.files?.[0])
  }

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (!file) return setError('Choose an audio file first.')
    if (!title.trim()) return setError('Give the meeting a title.')
    const folderProblem = folderChoiceError(folder)
    if (folderProblem) return setError(folderProblem)
    setError(null)
    setBusy('upload')
    let meetingId: string
    try {
      const form = new FormData()
      form.append('audio_file', file)
      form.append('title', title.trim())
      form.append('participants', people.join(','))
      form.append('source', origin)
      for (const [k, v] of Object.entries(folderFields(folder))) form.append(k, v)
      if (origin === 'online_tab') form.append('recording_consent', 'true') // the recorder ticked the box before recording
      meetingId = (await meetingsApi.upload(form)).meeting_id
    } catch (err) {
      setBusy(null)
      setError(err instanceof Error ? err.message : 'Upload failed')
      return
    }

    if (process) {
      setBusy('process')
      try {
        await meetingsApi.process(meetingId, { language, identify_speakers: speakers })
      } catch (err) {
        // The recording is safe; the user can start processing from the meeting page.
        toast.error(err)
      }
    }
    toast.ok(process ? 'Uploaded. Processing started.' : 'Recording uploaded.')
    nav(`/meetings/${meetingId}`)
  }

  return (
    <form onSubmit={submit}>
      <div className="grid two" style={{ alignItems: 'start' }}>
        <Card className="card-pad">
          <div className="stack">
            <ErrorBanner error={error} />
            <div className="seg-control full" role="group" aria-label="Audio source">
              <button type="button" aria-pressed={source === 'file'} onClick={() => setSource('file')}>
                Upload a file
              </button>
              <button type="button" aria-pressed={source === 'record'} onClick={() => setSource('record')}>
                Record now
              </button>
              <button type="button" aria-pressed={source === 'online'} onClick={() => setSource('online')}>
                Online meeting
              </button>
            </div>
            {source === 'record' ? (
              <Recorder onUse={(f) => useRecording(f)} />
            ) : source === 'online' ? (
              <OnlineRecorder members={members} onUse={(f, info) => useRecording(f, info)} />
            ) : (
            <div
              className={`drop ${over ? 'over' : ''} ${file ? 'has-file' : ''}`}
              onDragOver={(e) => {
                e.preventDefault()
                setOver(true)
              }}
              onDragLeave={() => setOver(false)}
              onDrop={onDrop}
              onClick={() => input.current?.click()}
              onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && input.current?.click()}
              role="button"
              tabIndex={0}
              aria-label="Choose an audio file"
            >
              <input
                ref={input}
                type="file"
                hidden
                accept={`audio/*,${AUDIO_EXT.join(',')}`}
                onChange={(e) => choose(e.target.files?.[0])}
                data-testid="audio-input"
              />
              {file ? (
                <div className="stack tight">
                  <strong>{file.name}</strong>
                  <span className="muted small">
                    {fmtBytes(file.size)}
                    {origin === 'online_tab' ? ' · online meeting, recording confirmed with everyone' : ''} · click to choose a different file, or use “Record now”
                  </span>
                </div>
              ) : (
                <div className="stack tight">
                  <span style={{ color: 'var(--accent)' }}>
                    <IconUpload size={28} />
                  </span>
                  <strong>Drop an audio file here, or click to browse</strong>
                  <span className="muted small">WAV, MP3, M4A, OGG, FLAC, WebM · up to 200 MB</span>
                </div>
              )}
            </div>
            )}
            <TextField label="Title" required value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} />
            <ChipInput
              label="Participants"
              values={people}
              onChange={setPeople}
              suggestions={members}
              placeholder="Pick a member, or type an e-mail and press Enter"
              help="People listed here can view the meeting, transcript and tasks."
            />
            <FolderPicker value={folder} onChange={setFolder} />
          </div>
        </Card>

        <Card className="card-pad">
          <div className="stack">
            <h2>Processing</h2>
            <Check checked={process} onChange={setProcess}>
              Start processing right away
            </Check>
            <p className="muted small" style={{ marginTop: '-0.5rem' }}>
              Transcribes the audio, writes a summary and extracts action items with deadlines. Runs in the background; you can leave the page.
            </p>
            <div className="field">
              <label htmlFor="lang">Spoken language</label>
              <select id="lang" className="input" value={language} onChange={(e) => setLanguage(e.target.value)} disabled={!process}>
                {LANGUAGES.map((l) => (
                  <option key={l.v} value={l.v}>
                    {l.l}
                  </option>
                ))}
              </select>
            </div>
            <Check checked={speakers} onChange={setSpeakers} disabled={!process}>
              Identify who is speaking
            </Check>
            <p className="muted small" style={{ marginTop: '-0.5rem' }}>
              Labels the transcript as Speaker 1, Speaker 2… Takes a little longer.
            </p>
            <hr className="divider" />
            <Button variant="primary" type="submit" busy={busy !== null} disabled={!file}>
              {busy === 'upload' ? 'Uploading…' : busy === 'process' ? 'Starting…' : process ? 'Upload and process' : 'Upload'}
            </Button>
          </div>
        </Card>
      </div>
    </form>
  )
}

/* ───────────────────────── Schedule ───────────────────────── */

function defaultStart(dateParam: string | null): string {
  const now = new Date()
  let d = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, 10, 0)
  if (dateParam && /^\d{4}-\d{2}-\d{2}$/.test(dateParam)) {
    const [y, m, day] = dateParam.split('-').map(Number)
    const picked = new Date(y, m - 1, day, 10, 0)
    if (picked.getTime() > now.getTime()) d = picked
  }
  return toLocalInput(d)
}

function ScheduleForm({ initialDate, initialFolder }: { initialDate: string | null; initialFolder: string | null }) {
  const nav = useNavigate()
  const toast = useToast()
  const { user } = useAuth()
  const [title, setTitle] = useState('')
  const [people, setPeople] = useState<string[]>([])
  const members = useMembers()
  const [folder, setFolder] = useState<FolderChoice>(initialFolder ? { folder_id: initialFolder } : {})
  const [start, setStart] = useState(defaultStart(initialDate))
  const [duration, setDuration] = useState(60)
  const [tz, setTz] = useState(user?.timezone || browserZone())
  const [repeat, setRepeat] = useState<Repeat | ''>('')
  const [endMode, setEndMode] = useState<'count' | 'until'>('count')
  const [count, setCount] = useState(4)
  const [until, setUntil] = useState('')
  const [skipped, setSkipped] = useState<ScheduleResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [conflicts, setConflicts] = useState<ConflictInfo[] | null>(null)
  const [suggest, setSuggest] = useState<{ loading: boolean; data?: OptimalTime; error?: string }>({ loading: false })

  // The dates a repeating meeting would get, so the person sees what they are about to create.
  const dates = useMemo(
    () => (repeat && start ? occurrences(fromWallClock(start), repeat, endMode === 'count' ? count : null, endMode === 'until' ? until : null) : []),
    [repeat, start, endMode, count, until],
  )
  const repeatReady = !repeat || dates.length >= 2

  async function save(allowConflicts: boolean) {
    const folderProblem = folderChoiceError(folder)
    if (folderProblem) return setError(folderProblem)
    setError(null)
    setBusy(true)
    try {
      const m = await meetingsApi.schedule({
        title: title.trim(),
        participants: people,
        scheduled_time: toApiWallClock(start),
        duration_minutes: duration,
        allow_conflicts: allowConflicts || undefined,
        timezone: tz || undefined,
        ...folderFields(folder),
        ...(repeat ? { repeat, repeat_count: endMode === 'count' ? count : undefined, repeat_until: endMode === 'until' ? until : undefined } : {}),
      })
      if (m.series && m.series.skipped.length > 0) {
        setSkipped(m) // say which dates could not be booked before moving on
        return
      }
      toast.ok(m.series ? `${m.series.created} meetings scheduled.` : 'Meeting scheduled.')
      nav(`/meetings/${m.id}`)
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && typeof e.detail === 'object' && e.detail) {
        setConflicts(((e.detail as { conflicts?: ConflictInfo[] }).conflicts ?? []) as ConflictInfo[])
      } else {
        setError(e instanceof Error ? e.message : 'Could not schedule the meeting')
      }
    } finally {
      setBusy(false)
    }
  }

  function submit(e: FormEvent) {
    e.preventDefault()
    setConflicts(null)
    if (!title.trim()) return setError('Give the meeting a title.')
    void save(false)
  }

  async function findTime() {
    setSuggest({ loading: true })
    try {
      const everyone = Array.from(new Set([...people, user!.email]))
      const data = await calendarApi.findTime({ participants: everyone, duration_minutes: duration })
      setSuggest({ loading: false, data })
    } catch (e) {
      setSuggest({ loading: false, error: e instanceof Error ? e.message : 'Could not find a time' })
    }
  }

  const pick = (iso: string) => {
    // slot times come back as naive wall-clock strings
    const d = fromWallClock(iso)
    setStart(toLocalInput(d))
    setConflicts(null)
  }

  const slots = suggest.data ? [{ start_time: suggest.data.optimal_time, end_time: suggest.data.end_time, confidence: suggest.data.confidence, reasoning: suggest.data.reasoning }, ...suggest.data.alternatives] : []
  const minLocal = toLocalInput(new Date())

  return (
    <form onSubmit={submit}>
      <div className="grid two" style={{ alignItems: 'start' }}>
        <Card className="card-pad">
          <div className="stack">
            <ErrorBanner error={error} />
            <TextField label="Title" required value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} autoFocus />
            <ChipInput
              label="Participants"
              values={people}
              onChange={setPeople}
              suggestions={members}
              placeholder="Pick a member, or type an e-mail and press Enter"
              help="You are included automatically. Conflicts are checked for everyone listed."
            />
            <FolderPicker value={folder} onChange={setFolder} />
            <div className="form-grid">
              <TextField label="Starts" type="datetime-local" required min={minLocal} value={start} onChange={(e) => { setStart(e.target.value); setConflicts(null) }} />
              <div className="field">
                <label htmlFor="dur">Duration</label>
                <select id="dur" className="input" value={duration} onChange={(e) => { setDuration(Number(e.target.value)); setConflicts(null) }}>
                  {DURATIONS.map((d) => (
                    <option key={d} value={d}>
                      {d >= 60 ? `${d / 60} hour${d === 60 ? '' : 's'}` : `${d} minutes`}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            <div className="field">
              <label htmlFor="tz">Time zone</label>
              <select id="tz" className="input" value={tz} onChange={(e) => { setTz(e.target.value); setConflicts(null) }}>
                {allZones(tz, user?.timezone).map((z) => (
                  <option key={z} value={z}>
                    {z.replace(/_/g, ' ')}
                  </option>
                ))}
              </select>
              <span className="help">The start time above is in this zone. Others see it in theirs.</span>
            </div>

            <div className="field">
              <label htmlFor="repeat">Repeat</label>
              <select id="repeat" className="input" value={repeat} onChange={(e) => { setRepeat(e.target.value as Repeat | ''); setConflicts(null) }}>
                {REPEAT_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </div>
            {repeat && (
              <div className="stack tight">
                <div className="row wrap">
                  <select className="input sm" aria-label="How the repeat ends" value={endMode} onChange={(e) => setEndMode(e.target.value as 'count' | 'until')} style={{ width: 'auto' }}>
                    <option value="count">Ends after</option>
                    <option value="until">Ends on</option>
                  </select>
                  {endMode === 'count' ? (
                    <>
                      <input className="input sm" type="number" aria-label="Number of meetings" min={2} max={MAX_OCCURRENCES} value={count} onChange={(e) => { setCount(Number(e.target.value)); setConflicts(null) }} style={{ width: '5rem' }} />
                      <span>meetings</span>
                    </>
                  ) : (
                    <input className="input sm" type="date" aria-label="Last day" min={start.slice(0, 10)} value={until} onChange={(e) => { setUntil(e.target.value); setConflicts(null) }} />
                  )}
                </div>
                <p className="muted small" style={{ margin: 0 }} role="status">
                  {dates.length >= 2
                    ? `${dates.length} meetings, from ${fmtDateTime(dates[0])} to ${fmtDateTime(dates[dates.length - 1])}. Each is checked for clashes; dates that clash are skipped.`
                    : endMode === 'count'
                      ? `Choose between 2 and ${MAX_OCCURRENCES} meetings.`
                      : 'Choose the last day (it must give at least two meetings).'}
                </p>
              </div>
            )}

            {conflicts && (
              <div className="alert warn" role="alert">
                <div className="grow stack tight">
                  <strong>This time clashes with {conflicts.length === 1 ? 'another meeting' : `${conflicts.length} other meetings`}:</strong>
                  <ul style={{ margin: 0, paddingLeft: '1.1rem' }}>
                    {conflicts.map((c, i) => (
                      <li key={c.id ?? i}>
                        {c.redacted ? 'Busy (private)' : c.title}
                        {c.scheduled_time && <> — {fmtDateTime(fromWallClock(c.scheduled_time))}</>}
                      </li>
                    ))}
                  </ul>
                  <div className="row wrap">
                    <Button size="sm" onClick={() => void findTime()} type="button">
                      Suggest another time
                    </Button>
                    <Button size="sm" variant="danger" type="button" busy={busy} onClick={() => void save(true)}>
                      Book anyway
                    </Button>
                  </div>
                </div>
              </div>
            )}
            <Button variant="primary" type="submit" busy={busy} disabled={!repeatReady}>
              {repeat && dates.length >= 2 ? `Schedule ${dates.length} meetings` : 'Schedule meeting'}
            </Button>
          </div>
        </Card>

        <Card className="card-pad">
          <div className="stack">
            <div className="row between">
              <h2>Find a free time</h2>
              <Button type="button" size="sm" onClick={() => void findTime()} busy={suggest.loading}>
                <IconSparkle /> Suggest times
              </Button>
            </div>
            <p className="muted small">
              Looks at everyone’s existing meetings in the next two weeks (working hours, 9–5) and ranks open slots.
            </p>
            {suggest.error && <div className="alert danger small" role="alert">{suggest.error}</div>}
            {slots.length > 0 && (
              <div className="stack tight">
                {slots.map((s, i) => (
                  <button type="button" key={s.start_time} className="slot" onClick={() => pick(s.start_time)}>
                    <span>
                      <strong>{fmtDateTime(fromWallClock(s.start_time))}</strong>
                      <span className="muted small" style={{ display: 'block' }}>{s.reasoning}</span>
                    </span>
                    {i === 0 && <span className="badge ok">Best</span>}
                  </button>
                ))}
                <p className="muted small">Click a time to use it.</p>
              </div>
            )}
            {!suggest.loading && !suggest.error && slots.length === 0 && (
              <p className="muted small">Pick participants and a duration, then ask for suggestions. Today is {dayKey(new Date())}.</p>
            )}
          </div>
        </Card>
      </div>
      {skipped?.series && (
        <Modal
          title="Some dates were skipped"
          onClose={() => nav(`/meetings/${skipped.id}`)}
          footer={
            <Button variant="primary" onClick={() => nav(`/meetings/${skipped.id}`)}>
              Open the first meeting
            </Button>
          }
        >
          <div className="stack">
            <p style={{ margin: 0 }}>
              {skipped.series.created} meeting{skipped.series.created === 1 ? ' was' : 's were'} scheduled. {skipped.series.skipped.length} date
              {skipped.series.skipped.length === 1 ? ' clashed' : 's clashed'} with other meetings and {skipped.series.skipped.length === 1 ? 'was' : 'were'} left out:
            </p>
            <ul style={{ margin: 0, paddingLeft: '1.1rem' }}>
              {skipped.series.skipped.map((s) => (
                <li key={s.scheduled_time}>
                  {fmtDateTime(fromWallClock(s.scheduled_time))} — {s.reason}
                </li>
              ))}
            </ul>
          </div>
        </Modal>
      )}
    </form>
  )
}
