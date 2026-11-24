import { useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { foldersApi, meetingsApi } from '../api/endpoints'
import type { Meeting } from '../api/types'
import { MeetingBadges, meetingWhen } from '../components/domain'
import { IconMic, IconPlus, IconTrash } from '../components/icons'
import { Button, Card, ConfirmDialog, EmptyState, ErrorBanner, LinkButton, Loading, useToast } from '../components/ui'
import { plural } from '../lib/format'
import { useAsync } from '../lib/useAsync'

const PAGE = 25

const STATUSES = [
  { value: '', label: 'All statuses' },
  { value: 'uploaded', label: 'Recorded' },
  { value: 'scheduled', label: 'Scheduled' },
  { value: 'in_progress', label: 'In progress' },
  { value: 'completed', label: 'Completed' },
  { value: 'cancelled', label: 'Cancelled' },
]

export default function MeetingsPage() {
  const nav = useNavigate()
  const [status, setStatus] = useState('')
  const [query, setQuery] = useState('')
  const [folder, setFolder] = useState('')
  const [limit, setLimit] = useState(PAGE)
  const toast = useToast()
  const [toDelete, setToDelete] = useState<Meeting | null>(null)
  const [deleting, setDeleting] = useState(false)

  const folders = useAsync(() => foldersApi.list().catch(() => ({ count: 0, folders: [] })), [])
  const list = useAsync(() => meetingsApi.list({ status: status || undefined, folder_id: folder || undefined, limit }), [status, folder, limit])

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase()
    const all = list.data?.meetings ?? []
    if (!q) return all
    return all.filter(
      (m) => m.title.toLowerCase().includes(q) || m.participants.some((p) => p.toLowerCase().includes(q)),
    )
  }, [list.data, query])

  async function doDelete() {
    if (!toDelete) return
    setDeleting(true)
    try {
      await meetingsApi.remove(toDelete.id)
      toast.ok(`“${toDelete.title}” deleted.`)
      list.reload()
    } catch (e) {
      toast.error(e)
    } finally {
      setDeleting(false)
      setToDelete(null)
    }
  }

  const hasMore = (list.data?.total ?? 0) > (list.data?.meetings.length ?? 0) && limit < 200

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Meetings</h1>
          <p>Every recording and scheduled meeting you own or take part in.</p>
        </div>
        <LinkButton variant="primary" to="/meetings/new">
          <IconPlus /> New meeting
        </LinkButton>
      </div>

      <Card>
        <div className="card-head" style={{ flexWrap: 'wrap' }}>
          <input
            className="input"
            style={{ maxWidth: 320 }}
            type="search"
            placeholder="Search title or participant"
            aria-label="Search meetings"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <select
            className="input"
            style={{ maxWidth: 200 }}
            aria-label="Filter by status"
            value={status}
            onChange={(e) => {
              setStatus(e.target.value)
              setLimit(PAGE)
            }}
          >
            {STATUSES.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
          {(folders.data?.folders.length ?? 0) > 0 && (
            <select
              className="input"
              style={{ maxWidth: 220 }}
              aria-label="Filter by folder"
              value={folder}
              onChange={(e) => {
                setFolder(e.target.value)
                setLimit(PAGE)
              }}
            >
              <option value="">All folders</option>
              {folders.data?.folders.map((f) => (
                <option key={f.id} value={f.id}>
                  {f.name}
                </option>
              ))}
            </select>
          )}
        </div>

        {list.loading && !list.data ? (
          <Loading />
        ) : list.error ? (
          <div className="card-body">
            <ErrorBanner error={list.error} onRetry={list.reload} />
          </div>
        ) : shown.length === 0 ? (
          <EmptyState
            icon={<IconMic />}
            title={query || status ? 'No matching meetings' : 'No meetings yet'}
            action={!query && !status ? <LinkButton variant="primary" to="/meetings/new">Upload a recording</LinkButton> : undefined}
          >
            {query || status ? 'Try a different search or filter.' : 'Upload a recording or schedule a meeting to get started.'}
          </EmptyState>
        ) : (
          <>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Meeting</th>
                    <th>When</th>
                    <th>People</th>
                    <th>Status</th>
                    <th style={{ width: '1%' }} aria-label="Actions" />
                  </tr>
                </thead>
                <tbody>
                  {shown.map((m) => (
                    <tr key={m.id} style={{ cursor: 'pointer' }} onClick={() => nav(`/meetings/${m.id}`)}>
                      <td>
                        <Link to={`/meetings/${m.id}`} onClick={(e) => e.stopPropagation()} style={{ fontWeight: 650 }}>
                          {m.title}
                        </Link>
                      </td>
                      <td className="small">{meetingWhen(m)}</td>
                      <td className="small">{plural(m.participants.length, 'participant')}</td>
                      <td>
                        <MeetingBadges m={m} />
                      </td>
                      <td onClick={(e) => e.stopPropagation()}>
                        {m.can_write && (
                          <Button variant="ghost" size="sm" aria-label={`Delete ${m.title}`} title="Delete meeting" onClick={() => setToDelete(m)}>
                            <IconTrash />
                          </Button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="card-body row between">
              <span className="muted small">
                Showing {shown.length} of {list.data?.total ?? 0}
              </span>
              {hasMore && (
                <Button onClick={() => setLimit((l) => Math.min(200, l + PAGE))} busy={list.loading}>
                  Load more
                </Button>
              )}
            </div>
          </>
        )}
      </Card>

      {toDelete && (
        <ConfirmDialog
          title="Delete this meeting permanently?"
          message={
            <>
              This removes <strong>{toDelete.title}</strong> together with its recording, transcript, summary and all action items. This cannot be undone.
            </>
          }
          confirmLabel="Delete forever"
          danger
          busy={deleting}
          onConfirm={() => void doDelete()}
          onCancel={() => setToDelete(null)}
        />
      )}
    </>
  )
}
