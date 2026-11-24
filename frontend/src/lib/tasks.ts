import type { TaskRow } from '../api/types'
import { daysFromToday } from './format'

export type Bucket = 'overdue' | 'today' | 'week' | 'later' | 'nodate' | 'done'

export const BUCKET_ORDER: Bucket[] = ['overdue', 'today', 'week', 'later', 'nodate', 'done']

export const BUCKET_LABEL: Record<Bucket, string> = {
  overdue: 'Overdue',
  today: 'Due today',
  week: 'This week',
  later: 'Later',
  nodate: 'No deadline',
  done: 'Completed',
}

/** Which group a task belongs to on the "My tasks" page. */
export function bucketOf(t: Pick<TaskRow, 'status' | 'due_date'>, today: Date = new Date()): Bucket {
  if (t.status === 'completed') return 'done'
  if (!t.due_date) return 'nodate'
  const n = daysFromToday(t.due_date.slice(0, 10), today)
  if (n < 0) return 'overdue'
  if (n === 0) return 'today'
  if (n <= 7) return 'week'
  return 'later'
}

export function groupTasks(tasks: TaskRow[], today: Date = new Date()): Record<Bucket, TaskRow[]> {
  const out: Record<Bucket, TaskRow[]> = {
    overdue: [],
    today: [],
    week: [],
    later: [],
    nodate: [],
    done: [],
  }
  for (const t of tasks) out[bucketOf(t, today)].push(t)
  return out
}

export function isOpen(status: string): boolean {
  return status !== 'completed'
}

export type SortKey = 'due' | 'name' | 'assignee' | 'priority' | 'meeting' | 'created'
export type PriorityFilter = 'all' | 'high' | 'medium' | 'low'

export const SORT_LABEL: Record<SortKey, string> = {
  due: 'Deadline',
  name: 'Task name (A–Z)',
  assignee: 'Assigned to',
  priority: 'Priority',
  meeting: 'Meeting',
  created: 'Newest first',
}

const PRIORITY_RANK: Record<string, number> = { high: 0, medium: 1, low: 2 }
const UNCLAIMED = /^(tbd|unassigned|speaker \d+)?$/i

const byText = (a: string, b: string) => a.localeCompare(b, undefined, { sensitivity: 'base', numeric: true })
const dueKey = (t: TaskRow) => (t.due_date ? t.due_date.slice(0, 10) : '9999-12-31')

/** A new array ordered by the chosen key; ties fall back to deadline, then name. */
export function sortTasks(tasks: TaskRow[], key: SortKey): TaskRow[] {
  const tie = (a: TaskRow, b: TaskRow) => byText(dueKey(a), dueKey(b)) || byText(a.task, b.task)
  const cmp: Record<SortKey, (a: TaskRow, b: TaskRow) => number> = {
    due: tie,
    name: (a, b) => byText(a.task, b.task) || byText(dueKey(a), dueKey(b)),
    assignee: (a, b) => {
      const ua = UNCLAIMED.test((a.assignee ?? '').trim())
      const ub = UNCLAIMED.test((b.assignee ?? '').trim())
      if (ua !== ub) return ua ? 1 : -1          // named people before unassigned
      if (ua && ub) return tie(a, b)             // all unassigned: by deadline
      return byText(a.assignee ?? '', b.assignee ?? '') || tie(a, b)
    },
    priority: (a, b) => (PRIORITY_RANK[a.priority] ?? 3) - (PRIORITY_RANK[b.priority] ?? 3) || tie(a, b),
    meeting: (a, b) => byText(a.meeting_title, b.meeting_title) || tie(a, b),
    created: (a, b) => byText(b.created_at ?? '', a.created_at ?? '') || tie(a, b),
  }
  return [...tasks].sort(cmp[key])
}

export function filterPriority(tasks: TaskRow[], p: PriorityFilter): TaskRow[] {
  return p === 'all' ? tasks : tasks.filter((t) => t.priority === p)
}
