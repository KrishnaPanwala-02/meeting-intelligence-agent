import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { foldersApi } from '../api/endpoints'
import { IconFolder, IconSparkle } from '../components/icons'
import { Button, Card, EmptyState, ErrorBanner, Loading, useToast } from '../components/ui'
import { fmtDate, fromWallClock, plural } from '../lib/format'
import { useAsync } from '../lib/useAsync'

/** Your folders: groups of meetings that remember each other. */
export default function FoldersPage() {
  const nav = useNavigate()
  const toast = useToast()
  const folders = useAsync(() => foldersApi.list(), [])
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)

  async function create(e: FormEvent) {
    e.preventDefault()
    if (!name.trim()) return
    setBusy(true)
    try {
      const f = await foldersApi.create(name.trim())
      toast.ok(`Folder “${f.name}” created. Upload its first meeting.`)
      nav(`/folders/${f.id}`)
    } catch (err) {
      toast.error(err)
      setBusy(false)
    }
  }

  const list = folders.data?.folders ?? []

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Folders</h1>
          <p>
            Group the meetings of a project. In a folder, each new meeting is summarised knowing what the earlier ones
            decided and left open, and you can ask questions across all of them.
          </p>
        </div>
      </div>

      <Card className="card-pad">
        <form className="row wrap" onSubmit={create} style={{ gap: '0.7rem' }}>
          <input
            className="input"
            aria-label="New folder name"
            placeholder="Name a new folder, e.g. Zomato project"
            maxLength={80}
            value={name}
            onChange={(e) => setName(e.target.value)}
            style={{ flex: '1 1 18rem' }}
          />
          <Button type="submit" variant="primary" busy={busy} disabled={!name.trim()}>
            Create folder
          </Button>
        </form>
      </Card>

      <div style={{ marginTop: '1.2rem' }}>
        {folders.error && !folders.data ? (
          <ErrorBanner error={folders.error} onRetry={folders.reload} />
        ) : !folders.data ? (
          <Loading />
        ) : list.length === 0 ? (
          <Card>
            <EmptyState icon={<IconFolder size={28} />} title="No folders yet">
              Make one above, or choose “New folder…” when you upload a meeting.
            </EmptyState>
          </Card>
        ) : (
          <div className="grid two">
            {list.map((f) => (
              <Card key={f.id} className="card-pad">
                <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
                  <div>
                    <h3 style={{ margin: 0 }}>
                      <Link to={`/folders/${f.id}`} style={{ display: 'inline-flex', gap: '0.45rem', alignItems: 'center' }}>
                        <IconFolder size={18} /> {f.name}
                      </Link>
                    </h3>
                    <div className="muted small" style={{ marginTop: '0.3rem' }}>
                      {plural(f.meeting_count, 'meeting')}
                      {f.last_meeting ? ` · latest ${fmtDate(fromWallClock(f.last_meeting))}` : ''}
                    </div>
                  </div>
                  {f.meeting_count > 0 && (
                    <Link className="btn sm" to={`/ask?folder=${f.id}`}>
                      <IconSparkle size={14} /> Ask
                    </Link>
                  )}
                </div>
              </Card>
            ))}
          </div>
        )}
      </div>
    </>
  )
}
