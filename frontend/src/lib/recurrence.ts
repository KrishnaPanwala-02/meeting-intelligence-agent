// Dates of a repeating meeting: the same rules as the server (src/services/recurrence.py).
import { dayKey } from './format'

export type Repeat = 'daily' | 'weekdays' | 'weekly' | 'biweekly' | 'monthly'

export const REPEAT_OPTIONS: { value: Repeat | ''; label: string }[] = [
  { value: '', label: 'Does not repeat' },
  { value: 'daily', label: 'Every day' },
  { value: 'weekdays', label: 'Every weekday' },
  { value: 'weekly', label: 'Every week' },
  { value: 'biweekly', label: 'Every 2 weeks' },
  { value: 'monthly', label: 'Every month' },
]

export const REPEAT_LABEL: Record<string, string> = {
  daily: 'every day',
  weekdays: 'every weekday',
  weekly: 'every week',
  biweekly: 'every 2 weeks',
  monthly: 'every month',
}

export const MAX_OCCURRENCES = 52
const MAX_HORIZON_DAYS = 366 * 2

const addDays = (d: Date, n: number) => {
  const x = new Date(d.getTime())
  x.setDate(x.getDate() + n)
  return x
}

function addMonths(start: Date, months: number): Date {
  const total = start.getMonth() + months
  const year = start.getFullYear() + Math.floor(total / 12)
  const month = ((total % 12) + 12) % 12
  const last = new Date(year, month + 1, 0).getDate()
  const d = new Date(start.getTime())
  d.setFullYear(year, month, Math.min(start.getDate(), last))
  return d
}

/** The first date is `start`; the rest follow the rule until `count` dates or the `until` day (YYYY-MM-DD). Empty if the inputs are not usable. */
export function occurrences(start: Date, rule: Repeat, count?: number | null, until?: string | null): Date[] {
  if (!count && !until) return []
  if (count && (count < 2 || count > MAX_OCCURRENCES)) return []
  if (until && until < dayKey(start)) return []
  const horizon = addDays(start, MAX_HORIZON_DAYS)
  const limit = count || MAX_OCCURRENCES
  const out = [start]
  let cur = start
  let step = 0
  while (out.length < limit) {
    step += 1
    if (rule === 'daily') cur = addDays(cur, 1)
    else if (rule === 'weekdays') {
      cur = addDays(cur, 1)
      while (cur.getDay() === 0 || cur.getDay() === 6) cur = addDays(cur, 1)
    } else if (rule === 'weekly') cur = addDays(cur, 7)
    else if (rule === 'biweekly') cur = addDays(cur, 14)
    else cur = addMonths(start, step)
    if (cur > horizon || (until && dayKey(cur) > until)) break
    out.push(cur)
  }
  return out.length >= 2 ? out : []
}
