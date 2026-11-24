import { useEffect, useMemo, useState } from 'react'
import { analyticsApi } from '../api/endpoints'
import type { Analytics, AnalyticsWeek } from '../api/types'
import { IconAlert, IconChart } from '../components/icons'
import { Card, CardHead, EmptyState, ErrorBanner, Loading } from '../components/ui'
import { fmtLength } from '../lib/format'
import { useAsync } from '../lib/useAsync'

const RANGES = [30, 90, 180, 365]
const pct = (x: number | null | undefined) => (x === null || x === undefined ? '—' : `${Math.round(x * 100)}%`)

const NICE = [2, 4, 6, 8, 10, 12, 16, 20, 30, 40, 60, 80, 100]

/** A round, even axis maximum at or above `v` (so the middle gridline is a whole number too). Never below 2. */
export function niceMax(v: number): number {
  let scale = 1
  while (v / scale > 100) scale *= 10
  return (NICE.find((n) => n >= v / scale) ?? 100) * scale
}

/** A column with a 4px rounded top and a square bottom (it grows from the baseline). */
export function columnPath(x: number, y: number, w: number, h: number, r = 4): string {
  const rr = Math.min(r, w / 2, h)
  return `M${x},${y + h}V${y + rr}Q${x},${y} ${x + rr},${y}H${x + w - rr}Q${x + w},${y} ${x + w},${y + rr}V${y + h}Z`
}

const weekLabel = (iso: string) => new Date(`${iso}T00:00:00`).toLocaleDateString([], { day: 'numeric', month: 'short' })

interface Series {
  key: keyof AnalyticsWeek
  label: string
  color: string
}

/** Weekly columns for one or two series on ONE axis, with a hover/focus tooltip. */
function WeeklyColumns({ weeks, series, title }: { weeks: AnalyticsWeek[]; series: Series[]; title: string }) {
  const [hot, setHot] = useState<number | null>(null)
  const W = 640
  const H = 170
  const left = 30
  const bottom = 22
  const plotH = H - bottom - 6
  const max = niceMax(Math.max(1, ...weeks.flatMap((w) => series.map((s) => Number(w[s.key])))))
  const slot = (W - left) / Math.max(1, weeks.length)
  const barW = Math.min(16, (slot - 2 * series.length) / series.length) // thin: never fills its slot (16 units is about 23px on screen)
  const step = Math.max(1, Math.ceil(weeks.length / 8))
  const y = (v: number) => 6 + plotH - (v / max) * plotH

  return (
    <div className="cols">
      {series.length > 1 && (
        <div className="viz-legend" aria-hidden="true">
          {series.map((s) => (
            <span key={s.key}>
              <i style={{ background: s.color }} />
              {s.label}
            </span>
          ))}
        </div>
      )}
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${title}, ${weeks.length} weeks. A table with the numbers is below.`}>
        {[0, max / 2, max].map((t) => (
          <g key={t}>
            <line className="grid" x1={left} x2={W} y1={y(t)} y2={y(t)} />
            <text className="tick" x={left - 6} y={y(t) + 4} textAnchor="end">
              {Number.isInteger(t) ? t : ''}
            </text>
          </g>
        ))}
        {weeks.map((w, i) => {
          const x0 = left + i * slot
          const groupW = series.length * barW + (series.length - 1) * 2
          const gx = x0 + (slot - groupW) / 2
          return (
            <g key={w.week_start}>
              {series.map((s, j) => {
                const v = Number(w[s.key])
                const h = (v / max) * plotH
                return v > 0 ? <path key={s.key} d={columnPath(gx + j * (barW + 2), y(v), barW, Math.max(h, 1))} fill={s.color} /> : null
              })}
              {i % step === 0 && (
                <text className="tick" x={x0 + slot / 2} y={H - 6} textAnchor="middle">
                  {weekLabel(w.week_start)}
                </text>
              )}
              <rect
                className="col-hit"
                x={x0}
                y={0}
                width={slot}
                height={H - bottom}
                fill="transparent"
                tabIndex={0}
                role="img"
                aria-label={`Week of ${weekLabel(w.week_start)}: ${series.map((s) => `${Number(w[s.key])} ${s.label.toLowerCase()}`).join(', ')}`}
                onMouseEnter={() => setHot(i)}
                onMouseLeave={() => setHot((c) => (c === i ? null : c))}
                onFocus={() => setHot(i)}
                onBlur={() => setHot((c) => (c === i ? null : c))}
              />
            </g>
          )
        })}
      </svg>
      {hot !== null && (
        <div
          className="tip"
          role="status"
          style={{
            left: `${((left + hot * slot + slot / 2) / W) * 100}%`,
            // sits just above the tallest mark of the column it describes
            top: `${((y(Math.max(...series.map((s) => Number(weeks[hot][s.key])))) - 8) / H) * 100}%`,
          }}
        >
          <strong>Week of {weekLabel(weeks[hot].week_start)}</strong>
          {series.map((s) => (
            <div key={s.key}>
              <i style={{ background: s.color }} />
              {s.label}: {Number(weeks[hot][s.key])}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export default function AnalyticsPage() {
  const [days, setDays] = useState(90)
  const data = useAsync(() => analyticsApi.get(days), [days])
  const a: Analytics | undefined = data.data

  const talkers = useMemo(() => (a?.people ?? []).filter((p) => p.talk_seconds > 0), [a])
  const withTasks = useMemo(() => (a?.people ?? []).filter((p) => p.tasks_total > 0).sort((x, y) => y.tasks_total - x.tasks_total || x.name.localeCompare(y.name)), [a])
  const topTalk = Math.max(1, ...talkers.map((p) => p.talk_seconds))

  // Keep the page title honest while the numbers reload.
  useEffect(() => {
    document.title = 'Analytics'
  }, [])

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Analytics</h1>
          <p>How your meetings and tasks are going, from the meetings you own or were invited to.</p>
        </div>
        <div className="seg-control" role="group" aria-label="Period">
          {RANGES.map((d) => (
            <button key={d} aria-pressed={days === d} onClick={() => setDays(d)}>
              {d} days
            </button>
          ))}
        </div>
      </div>

      {data.loading && !a ? (
        <Loading />
      ) : data.error && !a ? (
        <ErrorBanner error={data.error} onRetry={data.reload} />
      ) : a && a.overview.meetings === 0 ? (
        <Card>
          <EmptyState icon={<IconChart />} title="Nothing to show yet">
            There are no meetings in the last {a.days} days. Record or upload one, or choose a longer period.
          </EmptyState>
        </Card>
      ) : a ? (
        <div className="stack" style={{ gap: '1.2rem' }}>
          <div className="grid stats">
            <Card className="stat">
              <div className="label">Meetings</div>
              <div className="value">{a.overview.meetings}</div>
              <div className="hint">in the last {a.days} days</div>
            </Card>
            <Card className="stat">
              <div className="label">Recorded time</div>
              <div className="value" style={{ fontSize: '1.5rem' }}>{a.overview.recorded_seconds > 0 ? fmtLength(a.overview.recorded_seconds) : '—'}</div>
              <div className="hint">{a.overview.meetings_with_transcript} with a transcript</div>
            </Card>
            <Card className="stat">
              <div className="label">Tasks done</div>
              <div className="value">{pct(a.overview.completion_rate)}</div>
              <div className="hint">{a.overview.tasks_done} of {a.overview.tasks} tasks</div>
            </Card>
            <Card className={`stat ${a.overview.tasks_overdue > 0 ? 'is-late' : ''}`}>
              <div className="label">Overdue</div>
              <div className="value">{a.overview.tasks_overdue}</div>
              <div className="hint">{a.overview.tasks_overdue > 0 ? 'open tasks past their deadline' : 'nothing is late'}</div>
            </Card>
          </div>

          <div className="grid two" style={{ alignItems: 'start' }}>
            <Card>
              <CardHead title="Who talks" />
              <div className="card-body">
                {talkers.length === 0 ? (
                  <p className="muted small" style={{ margin: 0 }}>No named speakers yet. Name the speakers in a meeting’s Transcript tab to see talk time per person.</p>
                ) : (
                  <div className="hbars" role="list" aria-label="Talk time per person">
                    {talkers.map((p) => (
                      <div key={p.name} style={{ display: 'contents' }} role="listitem" title={`${p.name}: ${fmtLength(p.talk_seconds)}, ${pct(p.talk_share)} of all talk, ${p.words} words in ${p.meetings} meeting${p.meetings === 1 ? '' : 's'}`}>
                        <div className="name">{p.name}</div>
                        <div className="hbar-track">
                          <div className="hbar" style={{ width: `${Math.max(1, (p.talk_seconds / topTalk) * 55)}%` }} />
                          <span className="hbar-val">
                            {fmtLength(p.talk_seconds)} · {pct(p.talk_share)}
                          </span>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
                {a.overview.unnamed_talk_seconds > 0 && (
                  <p className="muted small" style={{ marginBottom: 0 }}>Speakers not named yet: {fmtLength(a.overview.unnamed_talk_seconds)} (not shown above).</p>
                )}
              </div>
            </Card>

            <Card>
              <CardHead title="Tasks by person" />
              <div className="card-body stack">
                {withTasks.length === 0 ? (
                  <p className="muted small" style={{ margin: 0 }}>No tasks in this period.</p>
                ) : (
                  withTasks.map((p) => (
                    <div key={p.name} className="stack tight">
                      <div className="row between">
                        <strong>{p.name}</strong>
                        <span className="muted small">
                          {p.tasks_done} of {p.tasks_total} done · {pct(p.completion_rate)}
                        </span>
                      </div>
                      <div className="pbar" role="img" aria-label={`${p.name}: ${p.tasks_done} of ${p.tasks_total} tasks done`}>
                        {p.tasks_done > 0 && <div className="done" style={{ flex: p.tasks_done }} />}
                        {p.tasks_open > 0 && <div className="open" style={{ flex: p.tasks_open }} />}
                      </div>
                      {p.tasks_overdue > 0 && (
                        <span className="small late">
                          <IconAlert size={14} /> {p.tasks_overdue} overdue
                        </span>
                      )}
                    </div>
                  ))
                )}
                {a.overview.tasks_unassigned_open > 0 && (
                  <p className="muted small" style={{ marginBottom: 0 }}>
                    {a.overview.tasks_unassigned_open} open task{a.overview.tasks_unassigned_open === 1 ? '' : 's'} not assigned to anyone yet.
                  </p>
                )}
              </div>
            </Card>
          </div>

          <Card>
            <CardHead title="Tasks created and completed, by week" />
            <div className="card-body">
              <WeeklyColumns
                weeks={a.weeks}
                title="Tasks created and completed per week"
                series={[
                  { key: 'tasks_created', label: 'Created', color: 'var(--viz-1)' },
                  { key: 'tasks_completed', label: 'Completed', color: 'var(--viz-2)' },
                ]}
              />
            </div>
          </Card>

          <Card>
            <CardHead title="Meetings per week" />
            <div className="card-body">
              <WeeklyColumns weeks={a.weeks} title="Meetings per week" series={[{ key: 'meetings', label: 'Meetings', color: 'var(--viz-1)' }]} />
            </div>
          </Card>

          <Card>
            <details>
              <summary style={{ padding: '0.9rem 1.25rem', cursor: 'pointer', fontWeight: 650 }}>Show the numbers as tables</summary>
              <div className="card-body stack">
                <div className="table-wrap">
                  <table className="table">
                    <caption className="sr-only">Per person</caption>
                    <thead>
                      <tr>
                        <th>Person</th><th>Talk time</th><th>Share</th><th>Meetings</th><th>Tasks</th><th>Done</th><th>Open</th><th>Overdue</th>
                      </tr>
                    </thead>
                    <tbody>
                      {a.people.map((p) => (
                        <tr key={p.name}>
                          <td>{p.name}</td><td>{fmtLength(p.talk_seconds)}</td><td>{pct(p.talk_share)}</td><td>{p.meetings}</td>
                          <td>{p.tasks_total}</td><td>{p.tasks_done}</td><td>{p.tasks_open}</td><td>{p.tasks_overdue}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="table-wrap">
                  <table className="table">
                    <caption className="sr-only">Per week</caption>
                    <thead>
                      <tr>
                        <th>Week of</th><th>Meetings</th><th>Tasks created</th><th>Tasks completed</th><th>Talk (min)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {a.weeks.map((w) => (
                        <tr key={w.week_start}>
                          <td>{weekLabel(w.week_start)}</td><td>{w.meetings}</td><td>{w.tasks_created}</td><td>{w.tasks_completed}</td><td>{w.talk_minutes}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </details>
          </Card>
        </div>
      ) : null}
    </>
  )
}
