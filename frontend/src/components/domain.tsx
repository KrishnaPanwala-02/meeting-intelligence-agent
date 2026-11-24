import { useState } from 'react'
import { Link } from 'react-router-dom'
import { meetingsApi } from '../api/endpoints'
import type { Job, JobStep, Meeting, TaskRow } from '../api/types'
import { daysFromToday, fmtDue, fromServerUtc, fromWallClock, fmtDateTime, relativeDue } from '../lib/format'
import { browserZone, instantOf, offsetMinutes, zoneAbbr } from '../lib/zone'
import { FolderBadge } from './FolderPicker'
import { IconComment } from './icons'
import TaskThreadDialog from './TaskThreadDialog'
import { Badge, MeetingStatusBadge, PriorityBadge, ProgressBar, useToast } from './ui'

/* ───────── Tasks ───────── */

export function whoCanTick(assignee: string | null): string {
  return assignee && assignee !== 'TBD' && !/^speaker \d+$/i.test(assignee)
    ? `Only ${assignee} can complete this task`
    : 'Only the meeting owner can change this task'
}

export function TaskRowItem({
  task,
  onUpdated,
  showMeeting = true,
}: {
  task: TaskRow
  onUpdated: (t: TaskRow) => void
  showMeeting?: boolean
}) {
  const toast = useToast()
  const [busy, setBusy] = useState(false)
  const [thread, setThread] = useState(false)
  const done = task.status === 'completed'
  const due = task.due_date?.slice(0, 10)
  const overdue = !done && due !== undefined && daysFromToday(due) < 0

  async function toggle() {
    const next = done ? 'pending' : 'completed'
    setBusy(true)
    onUpdated({ ...task, status: next }) // optimistic
    try {
      const saved = await meetingsApi.updateAction(task.meeting_id, task.id, { status: next })
      onUpdated({ ...task, ...saved })
    } catch (e) {
      onUpdated(task) // roll back
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={`task ${done ? 'done' : ''}`}>
      <input
        type="checkbox"
        aria-label={`${done ? 'Reopen' : 'Complete'}: ${task.task}`}
        checked={done}
        disabled={busy || !task.can_update_status}
        onChange={toggle}
        style={{ width: '1.15rem', height: '1.15rem', accentColor: 'var(--accent)' }}
        title={task.can_update_status ? undefined : whoCanTick(task.assignee)}
      />
      <div className="grow">
        <div className="task-title">{task.task}</div>
        <div className="row wrap small muted" style={{ marginTop: 2 }}>
          {showMeeting && <Link to={`/meetings/${task.meeting_id}`}>{task.meeting_title}</Link>}
          {task.mine && <Badge tone="accent">You</Badge>}
          {task.assignee && task.assignee !== 'TBD' && !task.mine && <Badge>{task.assignee}</Badge>}
          <PriorityBadge priority={task.priority} />
        </div>
      </div>
      <div className="small" style={{ textAlign: 'right', color: overdue ? 'var(--danger)' : undefined, fontWeight: overdue ? 650 : undefined }}>
        <div>{fmtDue(task.due_date)}</div>
        {!done && task.due_date && <div className="muted" style={overdue ? { color: 'var(--danger)' } : undefined}>{relativeDue(task.due_date)}</div>}
      </div>
      <button
        type="button"
        className="discuss-btn"
        onClick={() => setThread(true)}
        aria-label={`Discuss: ${task.task}${task.comment_count ? `, ${task.comment_count} comment${task.comment_count === 1 ? '' : 's'}` : ''}${task.pending_extension ? ', more time asked' : ''}`}
        title="Comments and requests"
      >
        <IconComment size={16} />
        {(task.comment_count ?? 0) > 0 && <span>{task.comment_count}</span>}
        {task.pending_extension && <span aria-hidden="true">⏳</span>}
      </button>
      {thread && (
        <TaskThreadDialog
          meetingId={task.meeting_id}
          task={task}
          onClose={() => setThread(false)}
          onChanged={() => void meetingsApi.action(task.meeting_id, task.id).then((fresh) => onUpdated({ ...task, ...fresh })).catch(() => undefined)}
        />
      )}
    </div>
  )
}

/* ───────── Meetings ───────── */

export function meetingWhen(m: Meeting): string {
  if (m.scheduled_time) {
    // The clock time is shown in the meeting's own zone; if you are elsewhere, your local time follows.
    const base = fmtDateTime(fromWallClock(m.scheduled_time))
    if (!m.timezone) return base
    const at = instantOf(m.scheduled_time, m.timezone)
    const label = zoneAbbr(m.timezone, at)
    const mine = browserZone()
    if (mine === m.timezone || offsetMinutes(at, mine) === offsetMinutes(at, m.timezone)) return `${base} ${label}`
    return `${base} ${label} (${at.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })} your time)`
  }
  if (m.created_at) return fmtDateTime(fromServerUtc(m.created_at))
  return ''
}

/** Calendar meetings that have not finished yet (in progress counts as upcoming). */
export function upcomingOnly<T extends { start: string; end: string; timezone?: string | null }>(events: T[], now: Date = new Date()): T[] {
  return events.filter((e) => instantOf(e.end, e.timezone).getTime() > now.getTime())
}

/** A scheduled meeting whose planned end time has passed. */
export function hasEnded(
  m: Pick<Meeting, 'status' | 'scheduled_time' | 'end_time' | 'duration_minutes'> & { timezone?: string | null },
  now: Date = new Date(),
): boolean {
  if (m.status !== 'scheduled' || !m.scheduled_time) return false
  const end = m.end_time
    ? instantOf(m.end_time, m.timezone)
    : new Date(instantOf(m.scheduled_time, m.timezone).getTime() + (m.duration_minutes ?? 60) * 60_000)
  return end.getTime() <= now.getTime()
}

export function MeetingBadges({ m }: { m: Meeting }) {
  return (
    <span className="row wrap" style={{ gap: '0.35rem' }}>
      {hasEnded(m) ? <Badge tone="warn">ended</Badge> : <MeetingStatusBadge status={m.status} />}
      {m.folder && <FolderBadge folder={m.folder} />}
      {m.is_owner === false && <Badge tone="info">Shared by {m.owner_name || m.owner_email || 'someone'}</Badge>}
      {m.has_audio && !m.has_transcript && m.status === 'uploaded' && <Badge tone="warn">not processed</Badge>}
      {m.has_summary && <Badge tone="ok">summary</Badge>}
      {(m.open_action_count ?? 0) > 0 && <Badge tone="accent">{m.open_action_count} open</Badge>}
    </span>
  )
}

/* ───────── Background job ───────── */

const STEP_LABEL: Record<JobStep['name'], string> = {
  transcribe: 'Transcribe & identify speakers',
  summarize: 'Write summary',
  extract_actions: 'Extract action items',
  voices: 'Analyse voices',
}

const STEP_STATE: Record<string, string> = {
  pending: 'Waiting',
  running: 'Working…',
  succeeded: 'Done',
  failed: 'Failed',
  skipped: 'Skipped',
  skipped_due_to_failure: 'Skipped',
}

function stepDetail(s: JobStep): string | null {
  const r = s.result
  if (s.state !== 'succeeded' || !r) return null
  if (s.name === 'transcribe') return `${r.speaker_count ?? 0} speakers · ${r.word_count ?? 0} words`
  if (s.name === 'voices') return `${r.voices ?? 0} voices${Number(r.recognised ?? 0) ? ` · ${r.recognised} recognised` : ''}`
  if (s.name === 'summarize') return r.already_existed ? 'Already up to date' : 'Generated'
  if (s.name === 'extract_actions') {
    const saved = Number(r.saved ?? 0)
    const review = Number(r.unresolved_deadlines ?? 0)
    const dup = Number(r.skipped_duplicates ?? 0)
    const parts = [`${saved} new`]
    if (dup) parts.push(`${dup} already known`)
    if (review) parts.push(`${review} deadline${review === 1 ? '' : 's'} to review`)
    return parts.join(' · ')
  }
  return null
}

export function JobProgress({ job }: { job: Job }) {
  const active = job.state === 'queued' || job.state === 'running' || job.state === 'retrying'
  const tone = job.state === 'succeeded' ? 'ok' : job.state === 'failed' ? 'danger' : job.state === 'retrying' ? 'warn' : 'info'
  const label =
    job.state === 'queued'
      ? 'Queued'
      : job.state === 'running'
        ? 'Processing'
        : job.state === 'retrying'
          ? 'Retrying'
          : job.state === 'succeeded'
            ? 'Finished'
            : 'Failed'
  return (
    <div className="stack tight" aria-live="polite">
      <div className="row between">
        <span className="row">
          <Badge tone={tone}>{label}</Badge>
          {active && <span className="muted small">{job.progress_percent}%</span>}
          {job.attempts > 1 && <span className="muted small">attempt {job.attempts}</span>}
        </span>
      </div>
      <ProgressBar value={job.state === 'succeeded' ? 100 : job.progress_percent} />
      <div className="steps" style={{ marginTop: '0.4rem' }}>
        {job.steps.map((s) => (
          <div key={s.name} className={`step ${s.state}`}>
            <strong className="small">{STEP_LABEL[s.name]}</strong>
            <span className="small muted">{STEP_STATE[s.state] ?? s.state}</span>
            {stepDetail(s) && <span className="small">{stepDetail(s)}</span>}
            {s.error && <span className="small" style={{ color: 'var(--danger)' }}>{s.error}</span>}
          </div>
        ))}
      </div>
      {job.error && job.state !== 'succeeded' && (
        <div className="alert danger small" role="alert">
          {job.error}
        </div>
      )}
    </div>
  )
}
