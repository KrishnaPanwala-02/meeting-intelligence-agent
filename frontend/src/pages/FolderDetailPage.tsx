import { useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { foldersApi } from '../api/endpoints'
import type { FolderMeeting } from '../api/types'
import { parseFollowUp } from '../components/FollowUpCard'
import { IconPlus, IconSparkle, IconTrash } from '../components/icons'
import { Badge, Button, Card, ConfirmDialog, EmptyState, ErrorBanner, LinkButton, Loading, MeetingStatusBadge, Modal, TextField, useToast } from '../components/ui'
import { fmtDate, fromWallClock, plural } from '../lib/format'
import { useAsync } from '../lib/useAsync'

function Step({ m }: { m: FolderMeeting }) {
  const rows = m.follow_up ? parseFollowUp(m.follow_up) : []
  return (
    <Card className="card-pad">
      <div className="row wrap" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
        <h3 style={{ margin: 0 }}>
          <span className="muted">Meeting {m.number} · </span>
          <Link to={`/meetings/${m.id}`}>{m.title}</Link>
        </h3>
        <span className="row wrap" style={{ gap: '0.4rem' }}>
          {m.when && <span className="muted small">{fmtDate(fromWallClock(m.when))}</span>}
          <MeetingStatusBadge status={m.status} />
          {m.open_tasks > 0 && <Badge tone="accent">{m.open_tasks} open</Badge>}
          {m.done_tasks > 0 && <Badge tone="ok">{m.done_tasks} done</Badge>}
        </span>
      </div>
      <p style={{ margin: '0.6rem 0 0' }}>
        {m.summary ?? <span className="muted">{m.status === 'scheduled' ? 'Not held yet.' : 'Not summarised yet.'}</span>}
      </p>
      {rows.length > 0 && (
        <div className="alert info" style={{ marginTop: '0.8rem', display: 'block' }}>
          <strong>Continues from earlier meetings</strong>
          {rows.map((r, i) => (
            <div key={i} style={{ marginTop: '0.3rem' }}>
              {r.label && <strong style={{ marginRight: '0.4rem' }}>{r.label}</strong>}
              {r.body}
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}

/** One folder: its meetings in order, each with its summary and how it continues the earlier ones. */
export default function FolderDetailPage() {
  const { id = '' } = useParams()
  const nav = useNavigate()
  const toast = useToast()
  const folder = useAsync(() => foldersApi.get(id), [id])
  const [dialog, setDialog] = useState<'rename' | 'delete' | null>(null)
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)

  async function rename(e?: FormEvent) {
    e?.preventDefault()
    if (!name.trim()) return
    setBusy(true)
    try {
      await foldersApi.rename(id, name.trim())
      setDialog(null)
      folder.reload()
    } catch (err) {
      toast.error(err)
    } finally {
      setBusy(false)
    }
  }

  async function remove() {
    setBusy(true)
    try {
      const r = await foldersApi.remove(id)
      toast.ok(`Folder deleted. ${plural(r.meetings_unfiled, 'meeting')} kept.`)
      nav('/folders', { replace: true })
    } catch (err) {
      toast.error(err)
      setBusy(false)
    }
  }

  if (folder.error && !folder.data) return <ErrorBanner error={folder.error} onRetry={folder.reload} />
  const f = folder.data
  if (!f) return <Loading />

  return (
    <>
      <div className="page-head">
        <div className="grow">
          <h1>{f.name}</h1>
          <p>{plural(f.meeting_count, 'meeting')}. Each new meeting here is summarised knowing the ones before it.</p>
        </div>
        <div className="row wrap">
          <LinkButton to={`/meetings/new?folder=${f.id}`} variant="primary">
            <IconPlus /> Upload a meeting here
          </LinkButton>
          {f.meeting_count > 0 && (
            <LinkButton to={`/ask?folder=${f.id}`}>
              <IconSparkle /> Ask this folder
            </LinkButton>
          )}
          <Button onClick={() => { setName(f.name); setDialog('rename') }}>Rename</Button>
          <Button variant="danger" aria-label="Delete folder" onClick={() => setDialog('delete')}>
            <IconTrash />
          </Button>
        </div>
      </div>

      {f.meetings.length === 0 ? (
        <Card>
          <EmptyState title="No meetings in this folder yet">
            Upload the first one. When you add the second, the system will already know what the first one said.
          </EmptyState>
        </Card>
      ) : (
        <div className="stack">
          {f.meetings.map((m) => (
            <Step key={m.id} m={m} />
          ))}
        </div>
      )}

      {dialog === 'rename' && (
        <Modal
          title="Rename folder"
          onClose={() => setDialog(null)}
          footer={
            <>
              <Button onClick={() => setDialog(null)}>Cancel</Button>
              <Button variant="primary" busy={busy} disabled={!name.trim()} onClick={() => void rename()}>
                Save
              </Button>
            </>
          }
        >
          <form onSubmit={rename}>
            <TextField label="Folder name" maxLength={80} value={name} autoFocus onChange={(e) => setName(e.target.value)} />
          </form>
        </Modal>
      )}
      {dialog === 'delete' && (
        <ConfirmDialog
          title="Delete this folder?"
          message={`“${f.name}” will be removed. Its ${plural(f.meeting_count, 'meeting')}, summaries and tasks are kept; they just stop being grouped, so later meetings no longer remember each other.`}
          confirmLabel="Delete folder"
          danger
          busy={busy}
          onConfirm={() => void remove()}
          onCancel={() => setDialog(null)}
        />
      )}
    </>
  )
}
