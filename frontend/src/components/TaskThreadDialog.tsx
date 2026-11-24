import { useState } from 'react'
import { threadApi } from '../api/endpoints'
import type { ThreadEntry } from '../api/types'
import { ago, dayKey, fmtDue, fromServerUtc } from '../lib/format'
import { useAsync } from '../lib/useAsync'
import { Badge, Button, ErrorBanner, Loading, Modal, useToast } from './ui'

const nameOf = (a: { name: string | null; email: string | null }) => a.name || a.email || 'Someone'
const dayText = (d: string | null) => (d ? fmtDue(`${d}T00:00:00`) : '')

function Entry({
  e,
  canDecide,
  busy,
  onDelete,
  onDecide,
}: {
  e: ThreadEntry
  canDecide: boolean
  busy: boolean
  onDelete: (e: ThreadEntry) => void
  onDecide: (e: ThreadEntry, approve: boolean, note: string) => void
}) {
  const [note, setNote] = useState('')
  const when = ago(e.created_at)

  if (e.kind === 'comment') {
    return (
      <li className="thread-entry">
        <div className="row between">
          <strong>{nameOf(e.author)}</strong>
          <span className="muted small">
            {when}
            {(e.mine || canDecide) && (
              <>
                {' · '}
                <button type="button" className="link-btn" onClick={() => onDelete(e)} aria-label={`Delete comment by ${nameOf(e.author)}`}>
                  Delete
                </button>
              </>
            )}
          </span>
        </div>
        <div style={{ whiteSpace: 'pre-wrap' }}>{e.body}</div>
      </li>
    )
  }

  return (
    <li className="thread-entry request">
      <div className="row between">
        <strong>{nameOf(e.author)} asked for more time</strong>
        <span className="muted small">{when}</span>
      </div>
      <div>
        New deadline asked: <strong>{dayText(e.requested_due)}</strong>{' '}
        {e.status === 'pending' && <Badge tone="warn">waiting for an answer</Badge>}
        {e.status === 'approved' && <Badge tone="ok">approved</Badge>}
        {e.status === 'declined' && <Badge>declined</Badge>}
      </div>
      {e.body && <div style={{ whiteSpace: 'pre-wrap' }}>“{e.body}”</div>}
      {e.status && e.status !== 'pending' && e.decided_by && (
        <div className="muted small">
          {e.status === 'approved' ? 'Approved' : 'Declined'} by {nameOf(e.decided_by)}
          {e.decision_note ? `: “${e.decision_note}”` : ''}
        </div>
      )}
      {e.status === 'pending' && canDecide && (
        <div className="stack tight" style={{ marginTop: '0.4rem' }}>
          <input className="input sm" aria-label="Note for your answer (optional)" placeholder="Note (optional)" maxLength={500} value={note} onChange={(x) => setNote(x.target.value)} />
          <div className="row">
            <Button size="sm" variant="primary" disabled={busy} onClick={() => onDecide(e, true, note)}>
              Approve
            </Button>
            <Button size="sm" disabled={busy} onClick={() => onDecide(e, false, note)}>
              Decline
            </Button>
          </div>
        </div>
      )}
    </li>
  )
}

/** The discussion on one task: comments, a request for more time, and the owner's answer. */
export default function TaskThreadDialog({
  meetingId,
  task,
  onClose,
  onChanged,
}: {
  meetingId: string
  task: { id: string; task: string; due_date: string | null }
  onClose: () => void
  onChanged: () => void
}) {
  const toast = useToast()
  const thread = useAsync(() => threadApi.get(meetingId, task.id), [meetingId, task.id])
  const [text, setText] = useState('')
  const [date, setDate] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const t = thread.data

  async function run(fn: () => Promise<unknown>, ok?: string) {
    setBusy(true)
    try {
      await fn()
      if (ok) toast.ok(ok)
      thread.reload()
      onChanged()
      return true
    } catch (e) {
      toast.error(e)
      return false
    } finally {
      setBusy(false)
    }
  }

  async function post() {
    if (!text.trim()) return
    if (await run(() => threadApi.comment(meetingId, task.id, text.trim()))) setText('')
  }

  async function ask() {
    if (!date) return
    const ok = await run(() => threadApi.requestMoreTime(meetingId, task.id, `${date}T00:00:00`, reason.trim() || undefined), 'Request sent.')
    if (ok) {
      setDate('')
      setReason('')
    }
  }

  // A request must be later than the current deadline (and never in the past).
  const earliest = (() => {
    const tomorrow = new Date()
    tomorrow.setDate(tomorrow.getDate() + 1)
    const base = task.due_date ? new Date(fromServerUtc(task.due_date.slice(0, 10) + 'T00:00:00').getTime() + 86_400_000) : tomorrow
    return dayKey(base > tomorrow ? base : tomorrow)
  })()

  return (
    <Modal title={`Discussion: ${task.task}`} onClose={onClose} footer={<Button onClick={onClose}>Close</Button>}>
      <div className="stack">
        {thread.loading && !t ? (
          <Loading />
        ) : thread.error && !t ? (
          <ErrorBanner error={thread.error} onRetry={thread.reload} />
        ) : t ? (
          <>
            {t.comments.length === 0 ? (
              <p className="muted small" style={{ margin: 0 }}>
                No comments yet.
              </p>
            ) : (
              <ul className="thread" aria-label="Comments">
                {t.comments.map((e) => (
                  <Entry
                    key={e.id}
                    e={e}
                    canDecide={t.can_decide}
                    busy={busy}
                    onDelete={(x) => void run(() => threadApi.removeComment(meetingId, task.id, x.id))}
                    onDecide={(x, approve, note) =>
                      void run(() => threadApi.decide(meetingId, task.id, x.id, approve, note.trim() || undefined), approve ? 'Deadline moved.' : 'Request declined.')
                    }
                  />
                ))}
              </ul>
            )}

            {t.can_comment && (
              <div className="stack tight">
                <textarea
                  className="input"
                  rows={2}
                  aria-label="Add a comment"
                  placeholder="Write a comment…"
                  maxLength={1000}
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                  onKeyDown={(e) => (e.metaKey || e.ctrlKey) && e.key === 'Enter' && void post()}
                />
                <div>
                  <Button size="sm" variant="primary" disabled={!text.trim()} busy={busy} onClick={() => void post()}>
                    Post comment
                  </Button>
                </div>
              </div>
            )}

            {t.can_request_extension && (
              <fieldset className="stack tight" style={{ border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)', padding: '0.7rem 0.9rem', margin: 0 }}>
                <legend className="small" style={{ fontWeight: 650 }}>
                  Need more time?
                </legend>
                <div className="row wrap">
                  <input className="input sm" type="date" aria-label="New deadline" min={earliest} value={date} onChange={(e) => setDate(e.target.value)} />
                  <input className="input sm grow" aria-label="Reason (optional)" placeholder="Why? (optional)" maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
                  <Button size="sm" disabled={!date} busy={busy} onClick={() => void ask()}>
                    Ask for more time
                  </Button>
                </div>
                <span className="muted small">The meeting owner will be told and can approve or decline.</span>
              </fieldset>
            )}
          </>
        ) : null}
      </div>
    </Modal>
  )
}
