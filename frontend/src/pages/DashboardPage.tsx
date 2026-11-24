import { Link } from 'react-router-dom'
import { calendarApi, meetingsApi, tasksApi } from '../api/endpoints'
import type { TaskRow } from '../api/types'
import { useMemo, useState } from 'react'
import { useAuth } from '../auth/AuthContext'
import { MeetingBadges, meetingWhen, TaskRowItem, upcomingOnly } from '../components/domain'
import { IconCalendar, IconCheck, IconMic, IconPlus, IconUpload } from '../components/icons'
import { Card, CardHead, EmptyState, ErrorBanner, LinkButton, Loading } from '../components/ui'
import { dayKey, fmtDay, fromWallClock, plural } from '../lib/format'
import { filterPriority, SORT_LABEL, sortTasks, type PriorityFilter, type SortKey } from '../lib/tasks'
import { useAsync } from '../lib/useAsync'

function greeting(): string {
  const h = new Date().getHours()
  return h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening'
}

export default function DashboardPage() {
  const { user } = useAuth()
  const email = user!.email
  const today = new Date()
  const todayKey = dayKey(today)
  const weekEnd = dayKey(new Date(today.getFullYear(), today.getMonth(), today.getDate() + 7))
  const yesterday = dayKey(new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1))

  const data = useAsync(async () => {
    const [meetings, open, mine, overdue, upcoming] = await Promise.all([
      meetingsApi.list({ limit: 5 }),
      tasksApi.list({ status: 'pending,in_progress', limit: 100 }),
      tasksApi.list({ status: 'pending,in_progress', mine: true, limit: 1 }),
      tasksApi.list({ status: 'pending,in_progress', mine: true, due_to: yesterday, limit: 1 }),
      calendarApi.events(email, { start_date: todayKey, end_date: weekEnd, event_type: 'meeting', status: 'scheduled' }),
    ])
    return { meetings, open, mine, overdue, upcoming }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [email, todayKey])

  // Today's meetings stay listed until they finish, not until midnight.
  const upcoming = data.data ? upcomingOnly(data.data.upcoming.events) : []

  const [sort, setSort] = useState<SortKey>('due')
  const [priority, setPriority] = useState<PriorityFilter>('all')
  // Tasks assigned to you first, in the chosen order; then everyone else's.
  const openTasks = useMemo(() => {
    const sorted = sortTasks(filterPriority(data.data?.open.action_items ?? [], priority), sort)
    return [...sorted.filter((t) => t.mine), ...sorted.filter((t) => !t.mine)]
  }, [data.data, sort, priority])

  // Tasks the signed-in person may fix whose deadline was too vague to become a date.
  const toReview = (data.data?.open.action_items ?? []).filter((t) => t.needs_review && t.can_write).length

  const setTask = (t: TaskRow) =>
    data.setData((d) =>
      d
        ? {
            ...d,
            open: {
              ...d.open,
              action_items: d.open.action_items.map((x) => (x.id === t.id ? t : x)),
            },
          }
        : d,
    )

  return (
    <>
      <div className="page-head">
        <div>
          <h1>
            {greeting()}, {user!.name?.split(' ')[0] || email.split('@')[0]}
          </h1>
          <p>Here is where your meetings and follow-ups stand.</p>
        </div>
        <div className="row wrap">
          <LinkButton to="/meetings/new?mode=schedule">
            <IconPlus /> Schedule meeting
          </LinkButton>
          <LinkButton variant="primary" to="/meetings/new">
            <IconUpload /> Upload recording
          </LinkButton>
        </div>
      </div>

      {toReview > 0 && (
        <div className="alert warn" role="status" style={{ marginBottom: '1.2rem' }}>
          <div className="grow">
            {toReview} task{toReview === 1 ? ' needs' : 's need'} a deadline: it was said, but too vaguely to become a date.
          </div>
          <Link className="btn sm" to="/tasks?review=1">
            Review
          </Link>
        </div>
      )}

      {data.loading && !data.data ? (
        <Loading />
      ) : data.error ? (
        <ErrorBanner error={data.error} onRetry={data.reload} />
      ) : (
        data.data && (
          <div className="stack" style={{ gap: '1.2rem' }}>
            <div className="grid stats">
              <Card className="stat">
                <div className="label">Meetings</div>
                <div className="value">{data.data.meetings.total}</div>
                <div className="hint">recorded or scheduled</div>
              </Card>
              <Card className="stat">
                <div className="label">Assigned to you</div>
                <div className="value">{data.data.mine.total}</div>
                <div className="hint">
                  {data.data.open.total > data.data.mine.total
                    ? `of ${data.data.open.total} open in your meetings`
                    : 'open tasks'}
                </div>
              </Card>
              <Card className={`stat ${data.data.overdue.total > 0 ? 'is-late' : ''}`}>
                <div className="label">Overdue</div>
                <div className="value">{data.data.overdue.total}</div>
                <div className="hint">{data.data.overdue.total > 0 ? 'of your tasks need attention' : 'your tasks are on track'}</div>
              </Card>
              <Card className="stat">
                <div className="label">Upcoming (7 days)</div>
                <div className="value">{upcoming.length}</div>
                <div className="hint">scheduled meetings</div>
              </Card>
            </div>

            <div className="grid two">
              <Card>
                <CardHead title="Open tasks">
                  <select className="input sm" aria-label="Sort tasks by" value={sort} onChange={(e) => setSort(e.target.value as SortKey)} style={{ width: 'auto' }}>
                    {(Object.keys(SORT_LABEL) as SortKey[]).map((k) => (
                      <option key={k} value={k}>
                        Sort: {SORT_LABEL[k]}
                      </option>
                    ))}
                  </select>
                  <select className="input sm" aria-label="Filter tasks by priority" value={priority} onChange={(e) => setPriority(e.target.value as PriorityFilter)} style={{ width: 'auto' }}>
                    <option value="all">Any priority</option>
                    <option value="high">High</option>
                    <option value="medium">Medium</option>
                    <option value="low">Low</option>
                  </select>
                  <Link to="/tasks" className="small">View all</Link>
                </CardHead>
                {data.data.open.action_items.length === 0 ? (
                  <EmptyState icon={<IconCheck />} title="Nothing open">
                    Tasks extracted from your meetings will appear here.
                  </EmptyState>
                ) : openTasks.length === 0 ? (
                  <EmptyState icon={<IconCheck />} title="No matching tasks">
                    No open task has {priority} priority.
                  </EmptyState>
                ) : (
                  <>
                    {openTasks.slice(0, 8).map((t) => (
                      <TaskRowItem key={t.id} task={t} onUpdated={setTask} />
                    ))}
                    {openTasks.length > 8 && (
                      <div className="card-body muted small">
                        Showing 8 of {openTasks.length}. <Link to="/tasks">See all tasks</Link>
                      </div>
                    )}
                  </>
                )}
              </Card>

              <div className="stack" style={{ gap: '1.2rem' }}>
                <Card>
                  <CardHead title="Coming up">
                    <Link to="/calendar" className="small">Calendar</Link>
                  </CardHead>
                  {upcoming.length === 0 ? (
                    <EmptyState icon={<IconCalendar />} title="No meetings this week">
                      Schedule one and it will show up here.
                    </EmptyState>
                  ) : (
                    <ul className="list">
                      {upcoming.slice(0, 5).map((e) => (
                        <li key={e.id}>
                          <Link className="list-link" to={`/meetings/${e.meeting_id}`}>
                            <div className="grow">
                              <div style={{ fontWeight: 600 }}>{e.summary}</div>
                              <div className="small muted">
                                {fmtDay(fromWallClock(e.start))} ·{' '}
                                {fromWallClock(e.start).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}
                                {fromWallClock(e.start).getTime() <= Date.now() && ' · in progress'}
                              </div>
                            </div>
                          </Link>
                        </li>
                      ))}
                    </ul>
                  )}
                </Card>

                <Card>
                  <CardHead title="Recent meetings">
                    <Link to="/meetings" className="small">All meetings</Link>
                  </CardHead>
                  {data.data.meetings.meetings.length === 0 ? (
                    <EmptyState icon={<IconMic />} title="No meetings yet" action={<LinkButton variant="primary" to="/meetings/new">Upload your first recording</LinkButton>}>
                      Upload a recording and get a transcript, summary and action items.
                    </EmptyState>
                  ) : (
                    <ul className="list">
                      {data.data.meetings.meetings.map((m) => (
                        <li key={m.id}>
                          <Link className="list-link" to={`/meetings/${m.id}`}>
                            <div className="grow">
                              <div style={{ fontWeight: 600 }} className="truncate">{m.title}</div>
                              <div className="small muted">
                                {meetingWhen(m)} · {plural(m.participants.length, 'participant')}
                              </div>
                            </div>
                            <MeetingBadges m={m} />
                          </Link>
                        </li>
                      ))}
                    </ul>
                  )}
                </Card>
              </div>
            </div>
          </div>
        )
      )}
    </>
  )
}
