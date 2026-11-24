import { useState } from 'react'
import { Link } from 'react-router-dom'
import { meetingsApi } from '../api/endpoints'
import type { TaskRow } from '../api/types'
import { deadlinePresets } from '../lib/deadlines'
import { dayKey } from '../lib/format'
import { Badge, Button, useToast } from './ui'

function ReviewRow({ task, onResolved }: { task: TaskRow; onResolved: (id: string) => void }) {
  const toast = useToast()
  const [date, setDate] = useState('')
  const [busy, setBusy] = useState(false)
  const presets = deadlinePresets()

  async function save(value: string) {
    if (!value) return
    setBusy(true)
    try {
      await meetingsApi.updateAction(task.meeting_id, task.id, { due_date: `${value}T00:00:00` })
      toast.ok(`Deadline set for “${task.task}”.`)
      onResolved(task.id)
    } catch (e) {
      toast.error(e)
      setBusy(false)
    }
  }

  return (
    <div className="review-row">
      <div className="grow">
        <div className="task-title">{task.task}</div>
        <div className="row wrap small muted" style={{ marginTop: 2 }}>
          <Link to={`/meetings/${task.meeting_id}?tab=actions`}>{task.meeting_title}</Link>
          {task.assignee && task.assignee !== 'TBD' && <Badge>{task.assignee}</Badge>}
          <span>
            Said: <q>{task.due_phrase}</q>
          </span>
        </div>
      </div>
      {task.can_write ? (
        <div className="stack tight" style={{ alignItems: 'flex-end' }}>
          <div className="row wrap" style={{ justifyContent: 'flex-end' }}>
            {presets.map((p) => (
              <button key={p.date} type="button" className="chip-btn" disabled={busy} onClick={() => void save(p.date)} title={p.date}>
                {p.label}
              </button>
            ))}
          </div>
          <div className="row">
            <input className="input sm" type="date" aria-label={`Deadline for ${task.task}`} min={dayKey(new Date())} value={date} onChange={(e) => setDate(e.target.value)} />
            <Button size="sm" variant="primary" busy={busy} disabled={!date} onClick={() => void save(date)} aria-label={`Save deadline for ${task.task}`}>
              Set date
            </Button>
          </div>
        </div>
      ) : (
        <span className="muted small">Only the meeting owner can set it.</span>
      )}
    </div>
  )
}

/** Tasks whose deadline was spoken but could not become a date: pick one, or choose a quick answer. */
export default function ReviewDeadlines({ tasks, onResolved }: { tasks: TaskRow[]; onResolved: (id: string) => void }) {
  return (
    <div>
      {tasks.map((t) => (
        <ReviewRow key={t.id} task={t} onResolved={onResolved} />
      ))}
    </div>
  )
}
