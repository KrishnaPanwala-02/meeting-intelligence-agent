import { useMemo, useState } from 'react'
import { exportApi, tasksApi } from '../api/endpoints'
import type { TaskRow } from '../api/types'
import { TaskRowItem } from '../components/domain'
import { IconCheck, IconDownload } from '../components/icons'
import ReviewDeadlines from '../components/ReviewDeadlines'
import { Badge, Button, Card, CardHead, EmptyState, ErrorBanner, Loading, useToast } from '../components/ui'
import { BUCKET_LABEL, BUCKET_ORDER, filterPriority, groupTasks, SORT_LABEL, sortTasks, type Bucket, type PriorityFilter, type SortKey } from '../lib/tasks'
import { downloadBlob } from '../lib/export'
import { useAsync } from '../lib/useAsync'

type StatusFilter = 'open' | 'done' | 'all'
type DueFilter = 'all' | 'overdue' | 'week' | 'nodate'
interface Group {
  key: string
  title?: string
  overdue?: boolean
  rows: TaskRow[]
}

const STATUS_QUERY: Record<StatusFilter, string | undefined> = {
  open: 'pending,in_progress',
  done: 'completed',
  all: undefined,
}

export default function TasksPage() {
  const toast = useToast()
  async function exportDeadlines() {
    try {
      downloadBlob('my-task-deadlines.ics', await exportApi.calendar({ meetings: false, tasks: true, mine: true }))
      toast.ok('Downloaded. Open the file to add your task deadlines to your calendar app.')
    } catch (e) {
      toast.error(e)
    }
  }

  const [status, setStatus] = useState<StatusFilter>('open')
  const [due, setDue] = useState<DueFilter>('all')
  const [text, setText] = useState('')
  const [sort, setSort] = useState<SortKey>('due')
  const [priority, setPriority] = useState<PriorityFilter>('all')

  const list = useAsync(() => tasksApi.list({ status: STATUS_QUERY[status], limit: 500 }), [status])
  const review = useAsync(() => tasksApi.list({ needs_review: true, limit: 100 }), [])
  const toReview = (review.data?.action_items ?? []).filter((t) => t.needs_review && t.due_phrase)
  const resolved = (id: string) => {
    review.setData((d) => (d ? { ...d, total: d.total - 1, count: d.count - 1, action_items: d.action_items.filter((t) => t.id !== id) } : d))
    list.reload()
  }

  const setTask = (t: TaskRow) =>
    list.setData((d) => (d ? { ...d, action_items: d.action_items.map((x) => (x.id === t.id ? t : x)) } : d))

  const sections = useMemo(() => {
    const q = text.trim().toLowerCase()
    // The server only sends your own tasks; this is a second guard so nobody else's can ever be listed.
    const rows = filterPriority((list.data?.action_items ?? []).filter((t) => t.mine), priority).filter((t) => {
      if (!q) return true
      return (
        t.task.toLowerCase().includes(q) ||
        (t.assignee ?? '').toLowerCase().includes(q) ||
        t.meeting_title.toLowerCase().includes(q)
      )
    })
    const keep: Record<DueFilter, Bucket[]> = {
      all: BUCKET_ORDER,
      overdue: ['overdue'],
      week: ['overdue', 'today', 'week'],
      nodate: ['nodate'],
    }
    const build = (subset: TaskRow[]): Group[] => {
      if (sort !== 'due') {
        // Any other ordering is one flat list, so the order is not broken up by deadline groups.
        const g = groupTasks(subset)
        const visible = BUCKET_ORDER.filter((b) => keep[due].includes(b)).flatMap((b) => g[b])
        return visible.length ? [{ key: 'flat', rows: sortTasks(visible, sort) }] : []
      }
      const g = groupTasks(subset)
      return BUCKET_ORDER.filter((b) => keep[due].includes(b) && g[b].length > 0).map((b) => ({
        key: b,
        title: BUCKET_LABEL[b],
        overdue: b === 'overdue',
        rows: sortTasks(g[b], 'due'),
      }))
    }
    return build(rows)
  }, [list.data, text, due, sort, priority])

  const total = sections.reduce((n, g) => n + g.rows.length, 0)

  const renderGroups = (groups: Group[]) =>
    groups.map(({ key, title, overdue, rows }) => (
      <section key={key} aria-label={title ?? 'Tasks'}>
        {title && (
          <div className={`group-title ${overdue ? 'overdue' : ''}`}>
            {title} <span className="muted">· {rows.length}</span>
          </div>
        )}
        {rows.map((t) => (
          <TaskRowItem key={t.id} task={t} onUpdated={setTask} />
        ))}
      </section>
    ))

  return (
    <>
      <div className="page-head">
        <div>
          <h1>My tasks</h1>
          <p>Only the tasks assigned to you. Other people’s tasks are never shown here.</p>
        </div>
        <Button onClick={() => void exportDeadlines()}>
          <IconDownload /> My deadlines (.ics)
        </Button>
      </div>

      {toReview.length > 0 && (
        <Card className="mb-card">
          <CardHead
            title={
              <>
                Needs a deadline <Badge tone="warn">{toReview.length}</Badge>
              </>
            }
          />
          <p className="muted small" style={{ margin: '0 1.25rem 0.4rem' }}>
            These tasks had a deadline that was too vague to turn into a date (such as “soon”). Choose one.
          </p>
          <ReviewDeadlines tasks={toReview} onResolved={resolved} />
        </Card>
      )}

      <Card>
        <div className="card-head" style={{ flexWrap: 'wrap' }}>
          <div className="seg-control" role="group" aria-label="Status">
            {(['open', 'done', 'all'] as StatusFilter[]).map((s) => (
              <button key={s} aria-pressed={status === s} onClick={() => setStatus(s)}>
                {s === 'open' ? 'Open' : s === 'done' ? 'Completed' : 'All'}
              </button>
            ))}
          </div>
          <div className="seg-control" role="group" aria-label="Deadline">
            {(['all', 'overdue', 'week', 'nodate'] as DueFilter[]).map((d) => (
              <button key={d} aria-pressed={due === d} onClick={() => setDue(d)}>
                {d === 'all' ? 'Any deadline' : d === 'overdue' ? 'Overdue' : d === 'week' ? 'This week' : 'No deadline'}
              </button>
            ))}
          </div>
          <select className="input" style={{ maxWidth: 190 }} aria-label="Sort by" value={sort} onChange={(e) => setSort(e.target.value as SortKey)}>
            {(Object.keys(SORT_LABEL) as SortKey[]).map((k) => (
              <option key={k} value={k}>
                Sort: {SORT_LABEL[k]}
              </option>
            ))}
          </select>
          <select className="input" style={{ maxWidth: 160 }} aria-label="Priority" value={priority} onChange={(e) => setPriority(e.target.value as PriorityFilter)}>
            <option value="all">Any priority</option>
            <option value="high">High priority</option>
            <option value="medium">Medium priority</option>
            <option value="low">Low priority</option>
          </select>
          <input
            className="input"
            style={{ maxWidth: 260 }}
            type="search"
            placeholder="Search tasks, people, meetings"
            aria-label="Search tasks"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </div>

        {list.loading && !list.data ? (
          <Loading />
        ) : list.error ? (
          <div className="card-body">
            <ErrorBanner error={list.error} onRetry={list.reload} />
          </div>
        ) : total === 0 ? (
          <EmptyState icon={<IconCheck />} title={status === 'open' && !text && due === 'all' ? 'You are all caught up' : 'No matching tasks'}>
            {status === 'open' && !text && due === 'all'
              ? 'Tasks assigned to you appear here after a meeting is processed.'
              : 'Try changing the filters.'}
          </EmptyState>
        ) : (
          <>{renderGroups(sections)}</>
        )}
      </Card>
      {list.data && list.data.total > list.data.count && (
        <p className="muted small" style={{ marginTop: '0.8rem' }}>
          Showing the first {list.data.count} of {list.data.total} tasks. Narrow the filters to see the rest.
        </p>
      )}
    </>
  )
}
