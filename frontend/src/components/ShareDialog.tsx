import { useMemo, useState } from 'react'
import { exportApi } from '../api/endpoints'
import type { ActionItem, Meeting, Summary } from '../api/types'
import { useAsync } from '../lib/useAsync'
import { summaryToPlainText } from '../lib/export'
import { Button, Check, Modal, Spinner, useToast } from './ui'

const looksLikeEmail = (s: string) => /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(s)
const MAILTO_BODY_LIMIT = 1800 // mail apps and browsers refuse very long mailto: links

/** E-mail a meeting's summary (from the server when it can send mail), or open it in the user's mail app. */
export default function ShareDialog({
  meeting,
  summary,
  actions,
  onClose,
}: {
  meeting: Meeting
  summary: Summary
  actions: ActionItem[]
  onClose: () => void
}) {
  const toast = useToast()
  const features = useAsync(() => exportApi.features().catch(() => ({ email_enabled: false })), [])
  const people = useMemo(() => meeting.participants.filter(looksLikeEmail), [meeting.participants])
  const [picked, setPicked] = useState<string[]>(people)
  const [copy, setCopy] = useState(false)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)

  const emailOn = features.data?.email_enabled === true
  const recipients = [...picked, ...(copy && meeting.owner_email ? [meeting.owner_email] : [])]
  const text = summaryToPlainText(meeting, summary, actions)
  const body = (note.trim() ? `${note.trim()}\n\n` : '') + text
  const mailto =
    `mailto:${recipients.map(encodeURIComponent).join(',')}` +
    `?subject=${encodeURIComponent(`Summary: ${meeting.title}`)}` +
    `&body=${encodeURIComponent(body.length > MAILTO_BODY_LIMIT ? `${body.slice(0, MAILTO_BODY_LIMIT)}…\n(shortened)` : body)}`

  async function send() {
    setBusy(true)
    try {
      const r = await exportApi.sendSummary(meeting.id, { recipients, note: note.trim() })
      toast.ok(`Sent to ${r.sent.join(', ')}.${r.failed.length ? ` Could not reach ${r.failed.join(', ')}.` : ''}`)
      onClose()
    } catch (e) {
      toast.error(e)
      setBusy(false)
    }
  }

  async function copyText() {
    try {
      await navigator.clipboard.writeText(body)
      toast.ok('Summary copied.')
    } catch {
      toast.error(new Error('Could not copy. Select the text in the Markdown export instead.'))
    }
  }

  return (
    <Modal
      title="E-mail the summary"
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Close</Button>
          <a className="btn" href={mailto} aria-disabled={recipients.length === 0} onClick={(e) => recipients.length === 0 && e.preventDefault()}>
            Open in mail app
          </a>
          <Button variant="primary" busy={busy} disabled={!emailOn || recipients.length === 0} onClick={() => void send()}>
            Send
          </Button>
        </>
      }
    >
      <div className="stack">
        {features.loading && !features.data ? (
          <Spinner label="Checking e-mail…" />
        ) : !emailOn ? (
          <div className="alert info" role="status">
            This computer is not set up to send e-mail. Use “Open in mail app” to send from your own mail program, or copy the text. (To send from here, an administrator sets EMAIL_ENABLED and the SMTP settings in .env.)
          </div>
        ) : null}

        <fieldset className="stack tight" style={{ border: 0, padding: 0, margin: 0 }}>
          <legend className="muted small">Send to people in this meeting</legend>
          {people.length === 0 && <p className="muted small">No participant has an e-mail address yet. Add participants to the meeting first.</p>}
          {people.map((p) => (
            <Check key={p} checked={picked.includes(p)} onChange={(on) => setPicked((cur) => (on ? [...cur, p] : cur.filter((x) => x !== p)))}>
              {p}
            </Check>
          ))}
          {meeting.owner_email && (
            <Check checked={copy} onChange={setCopy}>
              Send me a copy ({meeting.owner_email})
            </Check>
          )}
        </fieldset>

        <div className="field">
          <label htmlFor="note">Message (optional)</label>
          <textarea id="note" className="input" rows={3} maxLength={500} value={note} onChange={(e) => setNote(e.target.value)} placeholder="e.g. Please check your tasks before Friday." />
        </div>
        <p className="muted small">Each person gets the summary with their own tasks first.</p>
        <div>
          <Button size="sm" type="button" onClick={() => void copyText()}>
            Copy summary text
          </Button>
        </div>
      </div>
    </Modal>
  )
}
