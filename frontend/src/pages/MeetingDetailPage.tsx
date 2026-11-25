import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { meetingsApi } from '../api/endpoints'
import type { ActionItem, ConflictInfo, FolderChoice, Job, Meeting, Summary, Transcript } from '../api/types'
import ExportMenu from '../components/ExportMenu'
import FolderPicker, { folderChoiceError, folderFields } from '../components/FolderPicker'
import FollowUpCard from '../components/FollowUpCard'
import TaskThreadDialog from '../components/TaskThreadDialog'
import SpeakersCard from '../components/Speakers'
import { JobProgress, MeetingBadges, meetingWhen, whoCanTick } from '../components/domain'
import RenameMeetingDialog from '../components/RenameMeetingDialog'
import { IconComment, IconDownload, IconEdit, IconFolder, IconMic, IconPlus, IconSparkle, IconTrash } from '../components/icons'
import {
  Badge,
  Button,
  Card,
  CardHead,
  Check,
  ChipInput,
  ConfirmDialog,
  EmptyState,
  ErrorBanner,
  LinkButton,
  Loading,
  Modal,
  PriorityBadge,
  Spinner,
  Tabs,
  TextField,
  useToast,
} from '../components/ui'
import {
  downloadText,
  highlightParts,
  safeFilename,
  speakerClass,
  speakerOrder,
  transcriptToText,
} from '../lib/export'
import { fmtClock, fmtDateTime, fmtLength, fromServerUtc, fromWallClock, toApiWallClock, toLocalInput } from '../lib/format'
import { REPEAT_LABEL } from '../lib/recurrence'
import { countMatches } from '../lib/text'
import { allZones } from '../lib/zone'
import { useAsync, usePoll } from '../lib/useAsync'
import { useMembers } from '../lib/useMembers'

type TabId = 'summary' | 'transcript' | 'actions' | 'details'

const ACTIVE = new Set(['queued', 'running', 'retrying'])

/** Treat a 404 as "does not exist yet" instead of an error. */
async function orNull<T>(p: Promise<T>): Promise<T | null> {
  try {
    return await p
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null
    throw e
  }
}

export default function MeetingDetailPage() {
  const { id = '' } = useParams()
  const nav = useNavigate()
  const toast = useToast()

  const meeting = useAsync(() => meetingsApi.get(id), [id])
  const m = meeting.data

  // ── processing job ──────────────────────────────────────────────
  const [job, setJob] = useState<Job | null>(null)
  useEffect(() => {
    let live = true
    setJob(null)
    meetingsApi
      .latestJob(id)
      .then((j) => live && setJob(j))
      .catch(() => undefined)
    return () => {
      live = false
    }
  }, [id])

  const jobActive = !!job && ACTIVE.has(job.state)
  const [resultsKey, setResultsKey] = useState(0)
  // Set when a line of the transcript was corrected by hand: the summary still has the old wording.
  const [summaryStale, setSummaryStale] = useState(false)
  useEffect(() => setSummaryStale(false), [id])

  usePoll(
    async () => {
      if (!job) return
      try {
        const j = await meetingsApi.job(job.job_id)
        setJob(j)
        if (!ACTIVE.has(j.state)) {
          meeting.reload()
          setResultsKey((k) => k + 1)
          if (j.state === 'succeeded') toast.ok('Processing finished.')
          else toast.error(new Error(j.error ?? 'Processing failed.'))
        }
      } catch {
        /* transient: try again on the next tick */
      }
    },
    2000,
    jobActive,
  )

  // ── tab ─────────────────────────────────────────────────────────
  // A search result can open a meeting on a given tab, at a given transcript line (?tab=transcript&seg=3).
  const [urlParams] = useSearchParams()
  const urlTab = urlParams.get('tab')
  const [tab, setTab] = useState<TabId | null>(
    urlTab === 'summary' || urlTab === 'transcript' || urlTab === 'actions' || urlTab === 'details' ? urlTab : null,
  )
  const focusSegment = urlParams.get('seg') !== null && Number.isInteger(Number(urlParams.get('seg'))) ? Number(urlParams.get('seg')) : undefined
  const effectiveTab: TabId = tab ?? (m?.has_summary ? 'summary' : m?.has_transcript ? 'transcript' : 'details')

  // ── results (loaded when they exist; reloaded after a job finishes) ─
  const transcript = useAsync(
    () => (m?.has_transcript ? orNull(meetingsApi.transcript(id)) : Promise.resolve(null)),
    [id, m?.has_transcript, resultsKey],
  )
  const summary = useAsync(
    () => (m?.has_summary ? orNull(meetingsApi.summary(id)) : Promise.resolve(null)),
    [id, m?.has_summary, resultsKey],
  )
  const actions = useAsync(() => meetingsApi.actions(id).then((r) => r.action_items), [id, resultsKey])

  // ── dialogs ─────────────────────────────────────────────────────
  const [dialog, setDialog] = useState<'process' | 'cancel' | 'delete' | 'edit' | 'series' | 'folder' | 'rename' | null>(null)
  const [cancelScope, setCancelScope] = useState<'this' | 'following'>('this')
  const [busy, setBusy] = useState(false)

  async function startProcessing(opts: { language: string; speakers: boolean; regenerate: boolean; steps?: string[] }) {
    setBusy(true)
    try {
      const r = await meetingsApi.process(id, {
        steps: opts.steps,
        language: opts.language,
        identify_speakers: opts.speakers,
        regenerate_summary: opts.regenerate,
      })
      setJob(await meetingsApi.job(r.job_id))
      setDialog(null)
      toast.info('Processing started.')
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) toast.info('This meeting is already being processed.')
      else toast.error(e)
      setDialog(null)
      void meetingsApi.latestJob(id).then(setJob).catch(() => undefined)
    } finally {
      setBusy(false)
    }
  }

  async function startAnalyze() {
    try {
      const r = await meetingsApi.analyzeVoices(id)
      setJob(await meetingsApi.job(r.job_id))
      toast.info('Analysing voices…')
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) toast.info('This meeting is already being processed.')
      else toast.error(e)
    }
  }

  async function doCancel() {
    setBusy(true)
    try {
      const r = await meetingsApi.cancel(id, m?.series_id ? cancelScope : 'this')
      meeting.reload()
      toast.ok(r.cancelled_count && r.cancelled_count > 1 ? `${r.cancelled_count} meetings cancelled.` : 'Meeting cancelled.')
    } catch (e) {
      toast.error(e)
      meeting.reload()
    } finally {
      setBusy(false)
      setDialog(null)
    }
  }

  async function doDelete() {
    setBusy(true)
    try {
      await meetingsApi.remove(id)
      toast.ok('Meeting deleted.')
      nav('/meetings', { replace: true })
    } catch (e) {
      toast.error(e)
      setBusy(false)
      setDialog(null)
    }
  }

  if (meeting.loading && !m) return <Loading />
  if (meeting.error && !m) {
    const notFound = meeting.error instanceof ApiError && meeting.error.status === 404
    return notFound ? (
      <Card>
        <EmptyState icon={<IconMic />} title="Meeting not found" action={<Link className="btn" to="/meetings">Back to meetings</Link>}>
          It may have been deleted, or you do not have access to it.
        </EmptyState>
      </Card>
    ) : (
      <ErrorBanner error={meeting.error} onRetry={meeting.reload} />
    )
  }
  if (!m) return <Loading />

  const canWrite = m.can_write === true
  const processed = !!m.has_transcript
  const showJob = job && (ACTIVE.has(job.state) || job.state === 'failed')

  return (
    <>
      <div className="page-head">
        <div className="grow">
          <h1>{m.title}</h1>
          <div className="row wrap" style={{ marginTop: '0.5rem' }}>
            <MeetingBadges m={m} />
            <span className="muted small">{meetingWhen(m)}</span>
            {m.duration_minutes && m.status === 'scheduled' && <span className="muted small">· {m.duration_minutes} min</span>}
            {m.recurrence && (
              <>
                <Badge>
                  Repeats {REPEAT_LABEL[m.recurrence] ?? m.recurrence}
                  {m.series?.index ? ` · ${m.series.index} of ${m.series.count}` : ''}
                </Badge>
                <button type="button" className="link-btn small" onClick={() => setDialog('series')}>
                  See all dates
                </button>
              </>
            )}
          </div>
        </div>
        <div className="row wrap">
          {canWrite && m.has_audio && (
            <Button variant={processed ? 'default' : 'primary'} disabled={jobActive} onClick={() => setDialog('process')}>
              <IconSparkle /> {processed ? 'Re-process' : 'Process recording'}
            </Button>
          )}
          {(m.has_transcript || m.has_summary) && (
            <LinkButton to={`/ask?meeting=${m.id}`}>
              <IconSparkle /> Ask about this
            </LinkButton>
          )}
          {canWrite && (
            <Button onClick={() => setDialog('rename')}>
              <IconEdit /> Rename
            </Button>
          )}
          {canWrite && m.is_owner !== false && (
            <Button onClick={() => setDialog('folder')}>
              <IconFolder /> {m.folder ? 'Change folder' : 'Add to folder'}
            </Button>
          )}
          {canWrite && m.status === 'scheduled' && <Button onClick={() => setDialog('edit')}>Reschedule</Button>}
          {canWrite && m.status === 'scheduled' && <Button onClick={() => setDialog('cancel')}>Cancel meeting</Button>}
          {(processed || summary.data || m.scheduled_time) && (
            <ExportMenu meeting={m} summary={summary.data} actions={actions.data} transcript={transcript.data} />
          )}
          {canWrite && (
            <Button variant="danger" aria-label="Delete meeting" onClick={() => setDialog('delete')}>
              <IconTrash />
            </Button>
          )}
        </div>
      </div>

      {m.can_write === false && (
        <div className="alert info" style={{ marginBottom: '1rem' }}>
          You are a participant in this meeting. You can read everything, but only {m.owner_email ?? 'the owner'} can make changes.
        </div>
      )}

      {showJob && (
        <Card className="card-pad" >
          <h2 style={{ marginBottom: '0.7rem' }}>{jobActive ? 'Processing your recording…' : 'Processing did not finish'}</h2>
          <JobProgress job={job} />
          {jobActive && <p className="muted small" style={{ marginTop: '0.6rem' }}>This can take a few minutes. You can leave this page; processing continues in the background.</p>}
          {job.state === 'failed' && canWrite && (
            <div style={{ marginTop: '0.7rem' }}>
              <Button onClick={() => setDialog('process')}>Try again</Button>
            </div>
          )}
        </Card>
      )}

      <div style={{ marginTop: showJob ? '1.2rem' : 0 }}>
        <Tabs<TabId>
          tabs={[
            { id: 'summary', label: 'Summary' },
            { id: 'transcript', label: 'Transcript' },
            { id: 'actions', label: <>Action items {(m.open_action_count ?? 0) > 0 && <Badge tone="accent">{m.open_action_count}</Badge>}</> },
            { id: 'details', label: 'Details' },
          ]}
          value={effectiveTab}
          onChange={setTab}
        />

        {effectiveTab === 'summary' && (
          <SummaryPanel
            meeting={m}
            summary={summary.data}
            loading={summary.loading && !summary.data}
            error={summary.error}
            canProcess={canWrite && m.has_audio && !jobActive}
            onRegenerate={() => void startProcessing({ language: 'en', speakers: true, regenerate: true, steps: ['summarize'] })}
            onProcess={() => setDialog('process')}
          />
        )}
        {effectiveTab === 'transcript' && m.has_transcript && (
          <div style={{ margin: '1rem 0' }}>
            <SpeakersCard
              meetingId={id}
              canWrite={canWrite}
              hasAudio={!!m.has_audio}
              analyzing={jobActive}
              refreshKey={resultsKey}
              onChanged={() => setResultsKey((k) => k + 1)}
              onAnalyze={() => void startAnalyze()}
            />
          </div>
        )}
        {effectiveTab === 'transcript' && (
          <TranscriptPanel
            meeting={m}
            transcript={transcript.data}
            loading={transcript.loading && !transcript.data}
            error={transcript.error}
            onProcess={() => setDialog('process')}
            canProcess={canWrite && m.has_audio && !jobActive}
            canEdit={canWrite}
            focusSegment={focusSegment}
            summaryStale={summaryStale && !!m.has_summary}
            onChanged={(kind) => {
              setResultsKey((k) => k + 1)
              if (kind === 'segment') setSummaryStale(true)
            }}
            onRegenerate={() => {
              setSummaryStale(false)
              void startProcessing({ language: 'en', speakers: true, regenerate: true, steps: ['summarize'] })
            }}
          />
        )}
        {effectiveTab === 'actions' && <ActionsPanel meetingId={id} canWrite={canWrite} state={actions} openTask={urlParams.get('task')} />}
        {effectiveTab === 'details' && <DetailsPanel meeting={m} />}
      </div>

      {dialog === 'process' && <ProcessDialog processed={processed} busy={busy} onStart={startProcessing} onClose={() => setDialog(null)} />}
      {dialog === 'cancel' && (
        <ConfirmDialog
          title="Cancel this meeting?"
          message={
            m.series_id ? (
              <div className="stack">
                <p style={{ margin: 0 }}>The meeting stays in your history marked as cancelled, and its time slot is freed.</p>
                <fieldset className="stack tight" style={{ border: 0, padding: 0, margin: 0 }}>
                  <legend className="sr-only">Which meetings</legend>
                  <label className="check">
                    <input type="radio" name="scope" checked={cancelScope === 'this'} onChange={() => setCancelScope('this')} />
                    <span>Only this meeting</span>
                  </label>
                  <label className="check">
                    <input type="radio" name="scope" checked={cancelScope === 'following'} onChange={() => setCancelScope('following')} />
                    <span>This and all following meetings</span>
                  </label>
                </fieldset>
              </div>
            ) : (
              'The meeting stays in your history marked as cancelled, and its time slot is freed.'
            )
          }
          confirmLabel={m.series_id && cancelScope === 'following' ? 'Cancel these meetings' : 'Cancel meeting'}
          danger
          busy={busy}
          onConfirm={() => void doCancel()}
          onCancel={() => setDialog(null)}
        />
      )}
      {dialog === 'folder' && <FolderDialog meeting={m} onClose={() => setDialog(null)} onSaved={() => { setDialog(null); meeting.reload() }} />}
      {dialog === 'rename' && <RenameMeetingDialog meetingId={m.id} title={m.title} onClose={() => setDialog(null)} onSaved={() => { setDialog(null); meeting.reload() }} />}
      {dialog === 'series' && <SeriesDialog meetingId={id} onClose={() => setDialog(null)} />}
      {dialog === 'delete' && (
        <ConfirmDialog
          title="Delete this meeting permanently?"
          message={
            <>
              This removes <strong>{m.title}</strong> together with its recording, transcript, summary and all action items. This cannot be undone.
            </>
          }
          confirmLabel="Delete forever"
          danger
          busy={busy}
          onConfirm={() => void doDelete()}
          onCancel={() => setDialog(null)}
        />
      )}
      {dialog === 'edit' && <EditDialog meeting={m} onClose={() => setDialog(null)} onSaved={() => { setDialog(null); meeting.reload() }} />}
    </>
  )
}

/* ───────────────────────── Dialogs ───────────────────────── */

function SeriesDialog({ meetingId, onClose }: { meetingId: string; onClose: () => void }) {
  const series = useAsync(() => meetingsApi.series(meetingId), [meetingId])
  return (
    <Modal title="All dates in this series" onClose={onClose} footer={<Button onClick={onClose}>Close</Button>}>
      {series.loading && !series.data ? (
        <Loading />
      ) : series.error && !series.data ? (
        <ErrorBanner error={series.error} onRetry={series.reload} />
      ) : (
        <ul className="list" aria-label="Meetings in this series">
          {series.data?.meetings.map((s) => (
            <li key={s.id} className="row between" style={{ padding: '0.35rem 0' }}>
              <span>
                {s.is_current ? <strong>{s.scheduled_time ? fmtDateTime(fromWallClock(s.scheduled_time)) : '—'} (this one)</strong> : <Link to={`/meetings/${s.id}`} onClick={onClose}>{s.scheduled_time ? fmtDateTime(fromWallClock(s.scheduled_time)) : '—'}</Link>}
              </span>
              <Badge tone={s.status === 'cancelled' ? 'neutral' : 'info'}>{s.status}</Badge>
            </li>
          ))}
        </ul>
      )}
    </Modal>
  )
}

function ProcessDialog({
  processed,
  busy,
  onStart,
  onClose,
}: {
  processed: boolean
  busy: boolean
  onStart: (o: { language: string; speakers: boolean; regenerate: boolean }) => void
  onClose: () => void
}) {
  const [language, setLanguage] = useState('en')
  const [speakers, setSpeakers] = useState(true)
  const [regenerate, setRegenerate] = useState(processed)
  return (
    <Modal
      title={processed ? 'Re-process this recording' : 'Process this recording'}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" busy={busy} onClick={() => onStart({ language, speakers, regenerate })}>
            Start processing
          </Button>
        </>
      }
    >
      <div className="stack">
        <p>Transcribes the audio, writes a summary and extracts action items. Existing action items are kept; duplicates are skipped.</p>
        <div className="field">
          <label htmlFor="pl">Spoken language</label>
          <select id="pl" className="input" value={language} onChange={(e) => setLanguage(e.target.value)}>
            <option value="en">English</option>
            <option value="auto">Detect automatically</option>
            <option value="hi">Hindi</option>
            <option value="es">Spanish</option>
            <option value="fr">French</option>
            <option value="de">German</option>
          </select>
        </div>
        <Check checked={speakers} onChange={setSpeakers}>Identify who is speaking</Check>
        {processed && <Check checked={regenerate} onChange={setRegenerate}>Write a fresh summary</Check>}
      </div>
    </Modal>
  )
}

function FolderDialog({ meeting, onClose, onSaved }: { meeting: Meeting; onClose: () => void; onSaved: () => void }) {
  const toast = useToast()
  const [choice, setChoice] = useState<FolderChoice>({ folder_id: meeting.folder?.id ?? null })
  const [busy, setBusy] = useState(false)
  const problem = folderChoiceError(choice)

  async function save() {
    if (problem) return
    setBusy(true)
    try {
      const fields = folderFields(choice)
      const r = await meetingsApi.moveToFolder(meeting.id, 'new_folder' in fields || 'folder_id' in fields ? fields : { folder_id: null })
      toast.ok(r.folder ? `Added to “${r.folder.name}”.` : 'Taken out of its folder.')
      onSaved()
    } catch (err) {
      toast.error(err)
      setBusy(false)
    }
  }

  return (
    <Modal
      title="Folder"
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" busy={busy} disabled={!!problem} onClick={() => void save()}>
            Save
          </Button>
        </>
      }
    >
      <div className="stack">
        <FolderPicker value={choice} onChange={setChoice} />
        <p className="muted small" style={{ margin: 0 }}>
          Meetings are numbered by date. Moving one in changes what the later ones remember; summaries already written are not rewritten (use Regenerate to update one).
        </p>
        {problem && <span className="field-error">{problem}</span>}
      </div>
    </Modal>
  )
}

function EditDialog({ meeting, onClose, onSaved }: { meeting: Meeting; onClose: () => void; onSaved: () => void }) {
  const toast = useToast()
  const [title, setTitle] = useState(meeting.title)
  const [people, setPeople] = useState(meeting.participants)
  const members = useMembers()
  const [start, setStart] = useState(meeting.scheduled_time ? toLocalInput(fromWallClock(meeting.scheduled_time)) : '')
  const [duration, setDuration] = useState(meeting.duration_minutes ?? 60)
  const [tz, setTz] = useState(meeting.timezone ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [conflicts, setConflicts] = useState<ConflictInfo[] | null>(null)

  async function save(allow: boolean) {
    setBusy(true)
    setError(null)
    try {
      await meetingsApi.update(meeting.id, {
        title: title.trim(),
        participants: people,
        scheduled_time: start ? toApiWallClock(start) : undefined,
        duration_minutes: duration,
        allow_conflicts: allow || undefined,
        timezone: tz !== (meeting.timezone ?? '') ? tz : undefined,
      })
      toast.ok('Meeting updated.')
      onSaved()
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && typeof e.detail === 'object' && e.detail) {
        setConflicts(((e.detail as { conflicts?: ConflictInfo[] }).conflicts ?? []) as ConflictInfo[])
      } else setError(e instanceof Error ? e.message : 'Could not save')
    } finally {
      setBusy(false)
    }
  }

  const changed = () => setConflicts(null)

  return (
    <Modal
      title="Reschedule meeting"
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Close</Button>
          {conflicts && (
            <Button variant="danger" busy={busy} onClick={() => void save(true)}>
              Save anyway
            </Button>
          )}
          <Button variant="primary" busy={busy} onClick={() => void save(false)}>
            Save
          </Button>
        </>
      }
    >
      <div className="stack">
        <ErrorBanner error={error} />
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
              <span className="small">Pick another time, or save anyway to keep the overlap.</span>
            </div>
          </div>
        )}
        <TextField label="Title" value={title} onChange={(e) => setTitle(e.target.value)} />
        <TextField label="Starts" type="datetime-local" value={start} onChange={(e) => { setStart(e.target.value); changed() }} />
        <TextField label="Duration (minutes)" type="number" min={5} max={1440} value={duration} onChange={(e) => { setDuration(Number(e.target.value)); changed() }} />
        <div className="field">
          <label htmlFor="edit-tz">Time zone</label>
          <select id="edit-tz" className="input" value={tz} onChange={(e) => { setTz(e.target.value); changed() }}>
            <option value="">Not set (the computer’s clock)</option>
            {allZones(meeting.timezone).map((z) => (
              <option key={z} value={z}>
                {z.replace(/_/g, ' ')}
              </option>
            ))}
          </select>
        </div>
        <ChipInput label="Participants" values={people} suggestions={members} onChange={(v) => { setPeople(v); changed() }} />
      </div>
    </Modal>
  )
}

/* ───────────────────────── Summary ───────────────────────── */

function SummaryPanel({
  meeting,
  summary,
  loading,
  error,
  canProcess,
  onRegenerate,
  onProcess,
}: {
  meeting: Meeting
  summary: Summary | null | undefined
  loading: boolean
  error: Error | undefined
  canProcess: boolean
  onRegenerate: () => void
  onProcess: () => void
}) {
  if (loading) return <Loading />
  if (error) return <ErrorBanner error={error} />
  if (!summary) {
    return (
      <Card>
        <EmptyState
          icon={<IconSparkle />}
          title="No summary yet"
          action={canProcess ? <Button variant="primary" onClick={onProcess}>Process recording</Button> : undefined}
        >
          {meeting.has_audio
            ? 'Process the recording to get a transcript, summary and action items.'
            : 'This meeting has no recording to summarize.'}
        </EmptyState>
      </Card>
    )
  }
  return (
    <div className="stack" style={{ gap: '1.1rem' }}>
      <Card>
        <CardHead title="Overview">
          {canProcess && (
            <Button size="sm" onClick={onRegenerate}>
              <IconSparkle /> Regenerate
            </Button>
          )}
        </CardHead>
        <div className="card-body">
          <p style={{ fontSize: '1.02rem' }}>{summary.executive_summary}</p>
          {summary.created_at && <p className="muted small" style={{ marginTop: '0.6rem' }}>Generated {fmtDateTime(fromServerUtc(summary.created_at))}</p>}
        </div>
      </Card>

      {summary.follow_up && <FollowUpCard text={summary.follow_up} folderName={meeting.folder?.name} number={meeting.folder?.number} />}

      <div className="grid two" style={{ alignItems: 'start' }}>
        {summary.key_points.length > 0 && (
          <Card>
            <CardHead title="Key points" />
            <div className="card-body">
              {summary.key_points.map((k, i) => (
                <div key={i} className="kp">
                  <strong>{k.topic}</strong>
                  <p className="sub">{k.description}</p>
                </div>
              ))}
            </div>
          </Card>
        )}
        <div className="stack" style={{ gap: '1.1rem' }}>
          {summary.decisions.length > 0 && (
            <Card>
              <CardHead title="Decisions" />
              <div className="card-body">
                {summary.decisions.map((d, i) => (
                  <div key={i} className="kp">
                    <strong>{d.decision}</strong>
                    <div className="row wrap small muted">
                      {d.owner && d.owner !== 'TBD' && <Badge>{d.owner}</Badge>}
                      {d.context && <span>{d.context}</span>}
                    </div>
                  </div>
                ))}
              </div>
            </Card>
          )}
          {summary.next_steps.length > 0 && (
            <Card>
              <CardHead title="Next steps" />
              <ul className="card-body" style={{ margin: 0, paddingLeft: '2.2rem' }}>
                {summary.next_steps.map((n, i) => (
                  <li key={i}>{n}</li>
                ))}
              </ul>
            </Card>
          )}
          {summary.key_quotes.length > 0 && (
            <Card>
              <CardHead title="Notable quotes" />
              <div className="card-body stack">
                {summary.key_quotes.map((q, i) => (
                  <div key={i} className="quote">“{q}”</div>
                ))}
              </div>
            </Card>
          )}
        </div>
      </div>
    </div>
  )
}

/* ───────────────────────── Transcript ───────────────────────── */

function TranscriptPanel({
  meeting,
  transcript,
  loading,
  error,
  onProcess,
  canProcess,
  canEdit,
  focusSegment,
  summaryStale,
  onChanged,
  onRegenerate,
}: {
  meeting: Meeting
  transcript: Transcript | null | undefined
  loading: boolean
  error: Error | undefined
  onProcess: () => void
  canProcess: boolean
  canEdit: boolean
  /** A transcript line to scroll to and highlight (from a search result). */
  focusSegment?: number
  summaryStale: boolean
  onChanged: (kind: 'segment' | 'replace') => void
  onRegenerate: () => void
}) {
  const toast = useToast()
  const [editingId, setEditingId] = useState<number | null>(null)
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const [overrides, setOverrides] = useState<Record<number, string>>({})
  const [replacing, setReplacing] = useState<string | null>(null) // text to start the dialog with
  useEffect(() => setOverrides({}), [transcript])
  const [flash, setFlash] = useState<number | null>(null)
  useEffect(() => {
    if (focusSegment === undefined || !transcript) return
    const el = document.querySelector<HTMLElement>(`[data-seg="${focusSegment}"]`)
    el?.scrollIntoView?.({ block: 'center', behavior: 'smooth' })
    setFlash(focusSegment)
    const t = setTimeout(() => setFlash(null), 3000)
    return () => clearTimeout(t)
  }, [focusSegment, transcript])

  const audioRef = useRef<HTMLAudioElement>(null)
  const [audioUrl, setAudioUrl] = useState<string | null>(null)
  const [audioError, setAudioError] = useState<string | null>(null)
  const [now, setNow] = useState(0)
  const [term, setTerm] = useState('')

  // The audio endpoint needs the bearer token, so fetch it as a blob.
  useEffect(() => {
    if (!meeting.has_audio) return
    let url: string | null = null
    let live = true
    meetingsApi
      .audio(meeting.id)
      .then((blob) => {
        if (!live) return
        url = URL.createObjectURL(blob)
        setAudioUrl(url)
      })
      .catch((e: unknown) => live && setAudioError(e instanceof Error ? e.message : 'Audio unavailable'))
    return () => {
      live = false
      if (url) URL.revokeObjectURL(url)
    }
  }, [meeting.id, meeting.has_audio])

  const order = useMemo(() => speakerOrder(transcript ?? undefined), [transcript])
  const seek = useCallback((t: number) => {
    const a = audioRef.current
    if (!a) return
    a.currentTime = t
    void a.play().catch(() => undefined)
  }, [])

  const segments = transcript?.segments ?? []
  const q = term.trim().toLowerCase()
  const textOf = (s: { id: number; text: string }) => overrides[s.id] ?? s.text
  const visible = q ? segments.filter((s) => textOf(s).toLowerCase().includes(q) || (s.speaker ?? '').toLowerCase().includes(q)) : segments

  function startEdit(id: number, text: string) {
    setEditingId(id)
    setDraft(text)
  }

  async function saveEdit(id: number) {
    if (!draft.trim()) return toast.error(new Error('The text cannot be empty.'))
    setSaving(true)
    try {
      const saved = await meetingsApi.editSegment(meeting.id, id, draft)
      setOverrides((o) => ({ ...o, [id]: saved.text }))
      setEditingId(null)
      if (saved.changed) {
        toast.ok('Saved.')
        onChanged('segment')
      }
    } catch (e) {
      toast.error(e)
    } finally {
      setSaving(false)
    }
  }

  function openReplace() {
    const picked = window.getSelection()?.toString().trim() ?? ''
    setReplacing(picked && picked.length <= 100 && !picked.includes('\n') ? picked : '')
  }
  const currentId = segments.find((s) => now >= s.start_time && now < s.end_time)?.id

  if (loading) return <Loading />
  if (error) return <ErrorBanner error={error} />
  if (!transcript) {
    return (
      <Card>
        <EmptyState
          icon={<IconMic />}
          title="No transcript yet"
          action={canProcess ? <Button variant="primary" onClick={onProcess}>Process recording</Button> : undefined}
        >
          {meeting.has_audio ? 'Process the recording to see who said what.' : 'This meeting has no recording.'}
        </EmptyState>
      </Card>
    )
  }

  return (
    <Card>
      {summaryStale && (
        <div className="alert warn" role="status" style={{ margin: '1rem 1.25rem 0' }}>
          <div className="grow">You corrected the transcript. The summary still uses the old wording.</div>
          <Button size="sm" onClick={onRegenerate}>
            Regenerate summary
          </Button>
        </div>
      )}
      {meeting.has_audio && (
        <div className="player">
          {audioUrl ? (
            <audio ref={audioRef} controls src={audioUrl} onTimeUpdate={(e) => setNow(e.currentTarget.currentTime)} data-testid="player" />
          ) : audioError ? (
            <span className="muted small">Audio unavailable: {audioError}</span>
          ) : (
            <Spinner label="Loading audio…" />
          )}
        </div>
      )}
      <div className="card-head" style={{ flexWrap: 'wrap' }}>
        <div className="row wrap">
          {order.map((s) => (
            <span key={s} className={`speaker ${speakerClass(order, s)}`}>{s}</span>
          ))}
          <span className="muted small">
            {transcript.word_count} words · {transcript.language.toUpperCase()}
          </span>
        </div>
        <div className="row">
          <input className="input sm" type="search" placeholder="Search transcript" aria-label="Search transcript" value={term} onChange={(e) => setTerm(e.target.value)} style={{ width: 200 }} />
          {canEdit && (
            <Button size="sm" onClick={openReplace}>
              <IconEdit /> Find &amp; replace
            </Button>
          )}
          <Button size="sm" onClick={() => downloadText(`${safeFilename(meeting.title)}-transcript.txt`, transcriptToText(transcript))}>
            <IconDownload /> .txt
          </Button>
        </div>
      </div>
      {visible.length === 0 ? (
        <EmptyState title="No matches">Nothing in the transcript matches “{term}”.</EmptyState>
      ) : (
        visible.map((s) => (
          <div
            key={s.id}
            data-seg={s.id}
            className={`seg ${s.id === currentId ? 'now' : ''} ${s.id === flash ? 'flash' : ''}`}
            role="button"
            tabIndex={0}
            onClick={() => seek(s.start_time)}
            onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), seek(s.start_time))}
            aria-label={`${fmtClock(s.start_time)} ${s.speaker ?? ''}: ${s.text}`}
          >
            <span className="time">{fmtClock(s.start_time)}</span>
            <div>
              <div className="row between">
                {s.speaker ? <span className={`speaker ${speakerClass(order, s.speaker)}`}>{s.speaker}</span> : <span />}
                <span className="row" style={{ gap: '0.4rem' }}>
                  {(s.edited || overrides[s.id] !== undefined) && <span className="muted small">edited</span>}
                  {canEdit && editingId !== s.id && (
                    <button
                      type="button"
                      className="icon-btn"
                      aria-label={`Edit ${fmtClock(s.start_time)} ${s.speaker ?? ''}`.trim()}
                      title="Fix the words"
                      onClick={(e) => {
                        e.stopPropagation()
                        startEdit(s.id, textOf(s))
                      }}
                    >
                      <IconEdit size={15} />
                    </button>
                  )}
                </span>
              </div>
              {editingId === s.id ? (
                <div className="stack tight" onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
                  <textarea
                    className="input"
                    aria-label="Transcript text"
                    rows={3}
                    autoFocus
                    maxLength={5000}
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Escape') setEditingId(null)
                      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') void saveEdit(s.id)
                    }}
                  />
                  <div className="row">
                    <Button size="sm" variant="primary" busy={saving} onClick={() => void saveEdit(s.id)}>
                      Save
                    </Button>
                    <Button size="sm" onClick={() => setEditingId(null)} disabled={saving}>
                      Cancel
                    </Button>
                    <span className="muted small">Ctrl/⌘ + Enter to save · Esc to cancel</span>
                  </div>
                </div>
              ) : (
                <div>
                  {highlightParts(textOf(s), term).map((p, i) => (p.match ? <mark key={i}>{p.text}</mark> : <span key={i}>{p.text}</span>))}
                </div>
              )}
            </div>
          </div>
        ))
      )}
      {replacing !== null && (
        <ReplaceDialog
          meetingId={meeting.id}
          texts={segments.map(textOf)}
          initialFind={replacing}
          hasSummary={!!meeting.has_summary}
          onClose={() => setReplacing(null)}
          onDone={() => {
            setReplacing(null)
            onChanged('replace')
          }}
        />
      )}
    </Card>
  )
}

function ReplaceDialog({
  meetingId,
  texts,
  initialFind,
  hasSummary,
  onClose,
  onDone,
}: {
  meetingId: string
  texts: string[]
  initialFind: string
  hasSummary: boolean
  onClose: () => void
  onDone: () => void
}) {
  const toast = useToast()
  const [find, setFind] = useState(initialFind)
  const [replace, setReplace] = useState('')
  const [matchCase, setMatchCase] = useState(false)
  const [wholeWord, setWholeWord] = useState(true)
  const [alsoSummary, setAlsoSummary] = useState(true)
  const [busy, setBusy] = useState(false)
  const found = useMemo(() => countMatches(texts, find, matchCase, wholeWord), [texts, find, matchCase, wholeWord])
  const ready = find.trim() !== '' && replace.trim() !== '' && find.trim() !== replace.trim()

  async function run() {
    setBusy(true)
    try {
      const r = await meetingsApi.replaceText(meetingId, {
        find: find.trim(),
        replace: replace.trim(),
        match_case: matchCase,
        whole_word: wholeWord,
        include_summary: alsoSummary,
      })
      if (r.total === 0) toast.info('Nothing matched, so nothing changed.')
      else
        toast.ok(
          `Replaced ${r.total} time${r.total === 1 ? '' : 's'}: ${r.transcript} in the transcript` +
            (alsoSummary ? `, ${r.summary} in the summary, ${r.action_items} in tasks.` : '.'),
        )
      onDone()
    } catch (e) {
      toast.error(e)
      setBusy(false)
    }
  }

  return (
    <Modal
      title="Find and replace"
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" busy={busy} disabled={!ready} onClick={() => void run()}>
            Replace all
          </Button>
        </>
      }
    >
      <div className="stack">
        <TextField label="Find" value={find} maxLength={200} onChange={(e) => setFind(e.target.value)} help={find.trim() ? `${found} match${found === 1 ? '' : 'es'} in the transcript` : 'Tip: select a word in the transcript first.'} />
        <TextField label="Replace with" value={replace} maxLength={200} onChange={(e) => setReplace(e.target.value)} />
        <Check checked={wholeWord} onChange={setWholeWord}>Whole words only</Check>
        <Check checked={matchCase} onChange={setMatchCase}>Match upper/lower case</Check>
        {hasSummary && <Check checked={alsoSummary} onChange={setAlsoSummary}>Also fix the summary and action items</Check>}
      </div>
    </Modal>
  )
}

/* ───────────────────────── Action items ───────────────────────── */

function ActionsPanel({
  meetingId,
  canWrite,
  state,
  openTask,
}: {
  meetingId: string
  canWrite: boolean
  state: ReturnType<typeof useAsync<ActionItem[]>>
  /** A task id from a link (a notification): open its discussion straight away. */
  openTask?: string | null
}) {
  const toast = useToast()
  const [adding, setAdding] = useState(false)
  const [discussing, setDiscussing] = useState<string | null>(null)
  const opened = useRef(false)
  useEffect(() => {
    if (openTask && !opened.current && state.data?.some((a) => a.id === openTask)) {
      opened.current = true
      setDiscussing(openTask)
    }
  }, [openTask, state.data])
  // Tasks assigned to the signed-in user come first.
  const items = useMemo(
    () => (state.data ? [...state.data].sort((x, y) => Number(!!y.mine) - Number(!!x.mine)) : state.data),
    [state.data],
  )

  const patch = (id: string, next: Partial<ActionItem>) =>
    state.setData((list) => list?.map((a) => (a.id === id ? { ...a, ...next } : a)))

  async function save(a: ActionItem, body: Parameters<typeof meetingsApi.updateAction>[2], optimistic: Partial<ActionItem>) {
    patch(a.id, optimistic)
    try {
      patch(a.id, await meetingsApi.updateAction(meetingId, a.id, body))
    } catch (e) {
      patch(a.id, a)
      toast.error(e)
    }
  }

  async function remove(a: ActionItem) {
    try {
      await meetingsApi.deleteAction(meetingId, a.id)
      state.setData((list) => list?.filter((x) => x.id !== a.id))
    } catch (e) {
      toast.error(e)
    }
  }

  if (state.loading && !items) return <Loading />
  if (state.error && !items) return <ErrorBanner error={state.error} onRetry={state.reload} />

  return (
    <Card>
      <CardHead title={`Action items${items?.length ? ` (${items.length})` : ''}`}>
        {canWrite && (
          <Button size="sm" onClick={() => setAdding((v) => !v)}>
            <IconPlus /> Add
          </Button>
        )}
      </CardHead>
      {adding && (
        <AddAction
          meetingId={meetingId}
          onClose={() => setAdding(false)}
          onAdded={(a) => {
            state.setData((l) => [...(l ?? []), a])
            setAdding(false)
          }}
        />
      )}
      {!items?.length ? (
        <EmptyState title="No action items">
          {canWrite ? 'They are extracted automatically when the recording is processed, or add one yourself.' : 'Nothing has been assigned yet.'}
        </EmptyState>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th style={{ width: '1%' }} aria-label="Done" />
                <th>Task</th>
                <th>Owner</th>
                <th>Deadline</th>
                <th>Priority</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {items.map((a) => (
                <ActionRow key={a.id} a={a} canWrite={canWrite} onSave={save} onRemove={remove} onDiscuss={() => setDiscussing(a.id)} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      {discussing && items?.find((a) => a.id === discussing) && (
        <TaskThreadDialog
          meetingId={meetingId}
          task={items.find((a) => a.id === discussing)!}
          onClose={() => setDiscussing(null)}
          onChanged={() => state.reload()}
        />
      )}
    </Card>
  )
}

function DiscussButton({ a, onClick }: { a: ActionItem; onClick: () => void }) {
  const n = a.comment_count ?? 0
  return (
    <button
      type="button"
      className="discuss-btn"
      onClick={onClick}
      aria-label={`Discuss: ${a.task}${n ? `, ${n} comment${n === 1 ? '' : 's'}` : ''}${a.pending_extension ? ', more time asked' : ''}`}
      title={a.pending_extension ? `${a.pending_extension.by.name ?? 'Someone'} asked for more time` : 'Comments and requests'}
    >
      <IconComment size={16} />
      {n > 0 && <span>{n}</span>}
      {a.pending_extension && <span aria-hidden="true">⏳</span>}
    </button>
  )
}

function ActionRow({
  a,
  canWrite,
  onSave,
  onRemove,
  onDiscuss,
}: {
  a: ActionItem
  canWrite: boolean
  onDiscuss: () => void
  onSave: (a: ActionItem, body: Parameters<typeof meetingsApi.updateAction>[2], optimistic: Partial<ActionItem>) => Promise<void>
  onRemove: (a: ActionItem) => Promise<void>
}) {
  const [task, setTask] = useState(a.task)
  const [who, setWho] = useState(a.assignee ?? '')
  useEffect(() => setTask(a.task), [a.task])
  useEffect(() => setWho(a.assignee ?? ''), [a.assignee])
  const done = a.status === 'completed'

  const canTick = a.can_update_status ?? canWrite
  const tickBox = (
    <input
      type="checkbox"
      checked={done}
      disabled={!canTick}
      title={canTick ? undefined : whoCanTick(a.assignee)}
      aria-label={canTick ? `${done ? 'Reopen' : 'Complete'}: ${a.task}` : `${a.task} is ${a.status}`}
      onChange={() => void onSave(a, { status: done ? 'pending' : 'completed' }, { status: done ? 'pending' : 'completed' })}
      style={{ width: '1.1rem', height: '1.1rem', accentColor: 'var(--accent)' }}
    />
  )

  if (!canWrite) {
    return (
      <tr>
        <td>{tickBox}</td>
        <td style={{ textDecoration: done ? 'line-through' : undefined }}>{a.task}</td>
        <td>{a.assignee ?? '—'} {a.mine && <Badge tone="accent">You</Badge>}</td>
        <td>{a.due_date ? a.due_date.slice(0, 10) : '—'}</td>
        <td><PriorityBadge priority={a.priority} /></td>
        <td><DiscussButton a={a} onClick={onDiscuss} /></td>
      </tr>
    )
  }

  return (
    <tr>
      <td>{tickBox}</td>
      <td>
        <input
          className="input sm"
          value={task}
          aria-label="Task"
          style={{ textDecoration: done ? 'line-through' : undefined }}
          onChange={(e) => setTask(e.target.value)}
          onBlur={() => task.trim() && task.trim() !== a.task && void onSave(a, { task: task.trim() }, { task: task.trim() })}
        />
      </td>
      <td>
        <input
          className="input sm"
          value={who}
          aria-label="Owner"
          style={{ minWidth: 110 }}
          onChange={(e) => setWho(e.target.value)}
          onBlur={() => who.trim() !== (a.assignee ?? '') && void onSave(a, { assignee: who.trim() || 'TBD' }, { assignee: who.trim() || 'TBD' })}
        />
        {a.mine && <Badge tone="accent">You</Badge>}
      </td>
      <td>
        {a.needs_review && (
          <div className="small late" title="This deadline was said but not understood: choose a date">
            Said: “{a.due_phrase}”
          </div>
        )}
        <input
          className="input sm"
          type="date"
          aria-label="Deadline"
          value={a.due_date ? a.due_date.slice(0, 10) : ''}
          onChange={(e) => {
            const v = e.target.value
            void onSave(a, v ? { due_date: `${v}T00:00:00` } : { clear_due_date: true }, { due_date: v ? `${v}T00:00:00` : null })
          }}
        />
      </td>
      <td>
        <select
          className="input sm"
          aria-label="Priority"
          value={a.priority}
          onChange={(e) => void onSave(a, { priority: e.target.value }, { priority: e.target.value })}
        >
          <option value="low">Low</option>
          <option value="medium">Medium</option>
          <option value="high">High</option>
        </select>
      </td>
      <td>
        <div className="row" style={{ gap: '0.2rem', flexWrap: 'nowrap' }}>
          <DiscussButton a={a} onClick={onDiscuss} />
          <Button variant="ghost" size="sm" aria-label={`Delete ${a.task}`} onClick={() => void onRemove(a)}>
            <IconTrash />
          </Button>
        </div>
      </td>
    </tr>
  )
}

function AddAction({ meetingId, onAdded, onClose }: { meetingId: string; onAdded: (a: ActionItem) => void; onClose: () => void }) {
  const [task, setTask] = useState('')
  const [who, setWho] = useState('')
  const [due, setDue] = useState('')
  const [priority, setPriority] = useState('medium')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (!task.trim()) return
    setBusy(true)
    setError(null)
    try {
      onAdded(
        await meetingsApi.createAction(meetingId, {
          task: task.trim(),
          assignee: who.trim() || undefined,
          due_date: due ? `${due}T00:00:00` : null,
          priority,
        }),
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not add the task')
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="card-body stack" onSubmit={submit} style={{ borderBottom: '1px solid var(--border)', background: 'var(--surface-2)' }}>
      <ErrorBanner error={error} />
      <div className="form-grid">
        <TextField label="Task" required autoFocus value={task} onChange={(e) => setTask(e.target.value)} />
        <TextField label="Owner" value={who} onChange={(e) => setWho(e.target.value)} placeholder="TBD" />
        <TextField label="Deadline" type="date" value={due} onChange={(e) => setDue(e.target.value)} />
        <div className="field">
          <label htmlFor="np">Priority</label>
          <select id="np" className="input" value={priority} onChange={(e) => setPriority(e.target.value)}>
            <option value="low">Low</option>
            <option value="medium">Medium</option>
            <option value="high">High</option>
          </select>
        </div>
      </div>
      <div className="row">
        <Button variant="primary" type="submit" busy={busy}>Add task</Button>
        <Button type="button" onClick={onClose}>Cancel</Button>
      </div>
    </form>
  )
}

/* ───────────────────────── Details ───────────────────────── */

/** A recording that was not scheduled has no planned length: show how long it really is. */
function durationLabel(m: Meeting): string {
  if (m.has_audio && !m.scheduled_time && typeof m.audio_seconds === 'number') return fmtLength(m.audio_seconds)
  return m.duration_minutes ? `${m.duration_minutes} minutes` : '—'
}

function DetailsPanel({ meeting }: { meeting: Meeting }) {
  const rows: [string, React.ReactNode][] = [
    ['Status', <MeetingBadges key="s" m={meeting} />],
    ['Owner', meeting.owner_email ?? 'Not assigned (legacy meeting)'],
    ['Participants', meeting.participants.length ? meeting.participants.join(', ') : 'None listed'],
    ['Scheduled for', meeting.scheduled_time ? fmtDateTime(fromWallClock(meeting.scheduled_time)) : '—'],
    ['Duration', durationLabel(meeting)],
    ['Created', meeting.created_at ? fmtDateTime(fromServerUtc(meeting.created_at)) : '—'],
    ['Recording', meeting.has_audio ? 'Stored' : 'None'],
    ...(meeting.source === 'online_tab'
      ? ([['Recorded from', `An online meeting (browser tab audio${meeting.consent_at ? `, everyone’s consent confirmed ${fmtDateTime(fromServerUtc(meeting.consent_at))}` : ''})`]] as [string, React.ReactNode][])
      : meeting.source === 'browser_mic'
        ? ([['Recorded from', 'Recorded in this website with the microphone']] as [string, React.ReactNode][])
        : meeting.source === 'upload'
          ? ([['Recorded from', 'An audio file uploaded from this device']] as [string, React.ReactNode][])
          : []),
    ['Meeting ID', <span key="i" className="mono small">{meeting.id}</span>],
  ]
  return (
    <Card>
      <table className="table">
        <tbody>
          {rows.map(([k, v]) => (
            <tr key={k}>
              <td className="muted" style={{ width: '11rem' }}>{k}</td>
              <td>{v}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  )
}
