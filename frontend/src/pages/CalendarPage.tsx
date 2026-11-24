import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { calendarApi, exportApi } from '../api/endpoints'
import type { CalendarEvent } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { IconChevronLeft, IconChevronRight, IconDownload, IconPlus } from '../components/icons'
import { Badge, Button, Card, Check, EmptyState, ErrorBanner, LinkButton, Loading, useToast } from '../components/ui'
import { eventsByDay, gridRange, monthGrid } from '../lib/calendar'
import { dayKey, fmtDay, fmtMonth, fmtTime, fromServerUtc, fromWallClock } from '../lib/format'
import { downloadBlob } from '../lib/export'
import { useAsync } from '../lib/useAsync'

const DOW = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

function evClass(e: CalendarEvent): string {
  if (e.status === 'cancelled') return 'cancelled'
  if (e.type === 'submission' && e.status === 'completed') return 'done'
  return e.type
}

function evTime(e: CalendarEvent): string | null {
  if (e.type !== 'meeting') return null
  const d = e.date_source === 'recorded' || e.date_source === 'started' ? fromServerUtc(e.start) : fromWallClock(e.start)
  return fmtTime(d)
}

export default function CalendarPage() {
  const toast = useToast()
  async function exportCalendar() {
    try {
      downloadBlob('meeting-intelligence.ics', await exportApi.calendar())
      toast.ok('Calendar file downloaded. Open it to add the events to your calendar app.')
    } catch (e) {
      toast.error(e)
    }
  }

  const { user } = useAuth()
  const now = new Date()
  const [cursor, setCursor] = useState(new Date(now.getFullYear(), now.getMonth(), 1))
  const [selected, setSelected] = useState(dayKey(now))
  const [showMeetings, setShowMeetings] = useState(true)
  const [showTasks, setShowTasks] = useState(true)
  const [showDone, setShowDone] = useState(true)

  const grid = useMemo(() => monthGrid(cursor.getFullYear(), cursor.getMonth()), [cursor])
  const range = gridRange(grid)
  const data = useAsync(
    () => calendarApi.events(user!.email, { start_date: range.start, end_date: range.end }),
    [user!.email, range.start, range.end],
  )

  const events = useMemo(() => {
    return (data.data?.events ?? []).filter((e) => {
      if (e.type === 'meeting' && !showMeetings) return false
      if (e.type === 'submission' && !showTasks) return false
      if (!showDone && (e.status === 'completed' || e.status === 'cancelled')) return false
      return true
    })
  }, [data.data, showMeetings, showTasks, showDone])
  const byDay = useMemo(() => eventsByDay(events), [events])
  const todayKey = dayKey(now)
  const dayEvents = byDay.get(selected) ?? []
  const selectedDate = (() => {
    const [y, m, d] = selected.split('-').map(Number)
    return new Date(y, m - 1, d)
  })()

  const go = (delta: number) => setCursor(new Date(cursor.getFullYear(), cursor.getMonth() + delta, 1))

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Calendar</h1>
          <p>Your meetings and task deadlines, all from this app.</p>
        </div>
        <div className="row wrap">
          <Button onClick={() => void exportCalendar()}>
            <IconDownload /> Export .ics
          </Button>
          <LinkButton variant="primary" to={`/meetings/new?mode=schedule&date=${selected}`}>
            <IconPlus /> Schedule meeting
          </LinkButton>
        </div>
      </div>

      <div className="cal-layout">
        <Card>
          <div className="card-head" style={{ flexWrap: 'wrap' }}>
            <div className="row wrap">
              <Button size="sm" aria-label="Previous month" onClick={() => go(-1)}>
                <IconChevronLeft />
              </Button>
              <h2 style={{ minWidth: '8.5rem', textAlign: 'center' }} aria-live="polite">
                {fmtMonth(cursor)}
              </h2>
              <Button size="sm" aria-label="Next month" onClick={() => go(1)}>
                <IconChevronRight />
              </Button>
              <Button
                size="sm"
                onClick={() => {
                  setCursor(new Date(now.getFullYear(), now.getMonth(), 1))
                  setSelected(todayKey)
                }}
              >
                Today
              </Button>
            </div>
            <div className="row wrap">
              <Check checked={showMeetings} onChange={setShowMeetings}>Meetings</Check>
              <Check checked={showTasks} onChange={setShowTasks}>Deadlines</Check>
              <Check checked={showDone} onChange={setShowDone}>Finished</Check>
            </div>
          </div>

          {data.error ? (
            <div className="card-body">
              <ErrorBanner error={data.error} onRetry={data.reload} />
            </div>
          ) : (
            <div className="cal" role="grid" aria-label={fmtMonth(cursor)} aria-busy={data.loading}>
              {DOW.map((d) => (
                <div key={d} className="cal-dow" role="columnheader">
                  {d}
                </div>
              ))}
              {grid.map((day) => {
                const list = byDay.get(day.key) ?? []
                const cls = ['cal-day', !day.inMonth && 'out', day.key === todayKey && 'today', day.key === selected && 'sel']
                  .filter(Boolean)
                  .join(' ')
                return (
                  <button
                    key={day.key}
                    className={cls}
                    role="gridcell"
                    aria-label={`${fmtDay(day.date)}${list.length ? `, ${list.length} events` : ''}`}
                    aria-selected={day.key === selected}
                    onClick={() => setSelected(day.key)}
                  >
                    <span className="cal-num">{day.date.getDate()}</span>
                    {list.slice(0, 3).map((e) => (
                      <span key={e.id} className={`ev ${evClass(e)}`} title={e.summary}>
                        {e.summary}
                      </span>
                    ))}
                    {list.length > 3 && <span className="more">+{list.length - 3} more</span>}
                    {list.length > 0 && (
                      <span className="ev-dots" aria-hidden="true">
                        {list.slice(0, 6).map((e) => (
                          <i key={e.id} className={e.type === 'submission' ? 's' : ''} />
                        ))}
                      </span>
                    )}
                  </button>
                )
              })}
            </div>
          )}
          <div className="card-body legend">
            <span><i className="dot" style={{ background: 'var(--info)' }} />Meeting</span>
            <span><i className="dot" style={{ background: 'var(--warn)' }} />Task deadline</span>
            <span><i className="dot" style={{ background: 'var(--ok)' }} />Completed</span>
            {data.loading && <span className="muted">Updating…</span>}
          </div>
        </Card>

        <Card>
          <div className="card-head">
            <h2>{fmtDay(selectedDate)}</h2>
            {selected === todayKey && <Badge tone="accent">Today</Badge>}
          </div>
          {data.loading && !data.data ? (
            <Loading />
          ) : dayEvents.length === 0 ? (
            <EmptyState title="Nothing scheduled">No meetings or deadlines on this day.</EmptyState>
          ) : (
            <ul className="list">
              {dayEvents.map((e) => (
                <li key={e.id}>
                  <Link className="list-link" to={`/meetings/${e.meeting_id}`}>
                    <div className="grow">
                      <div style={{ fontWeight: 600, textDecoration: e.status === 'cancelled' ? 'line-through' : undefined }}>
                        {e.summary}
                      </div>
                      <div className="small muted">
                        {e.type === 'meeting'
                          ? [evTime(e), e.date_source === 'recorded' ? 'recorded' : null].filter(Boolean).join(' · ')
                          : `Deadline${e.assignee && e.assignee !== 'TBD' ? ` · ${e.assignee}` : ''}${e.meeting_title ? ` · ${e.meeting_title}` : ''}`}
                      </div>
                    </div>
                    <Badge tone={e.status === 'completed' ? 'ok' : e.type === 'submission' ? 'warn' : e.status === 'cancelled' ? 'neutral' : 'info'}>
                      {e.type === 'submission' ? (e.status === 'completed' ? 'done' : 'due') : e.status.replace('_', ' ')}
                    </Badge>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </>
  )
}
