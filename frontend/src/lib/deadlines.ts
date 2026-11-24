import { dayKey } from './format'

export interface Preset {
  label: string
  /** YYYY-MM-DD */
  date: string
}

const addDays = (d: Date, n: number) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n)

/** The Friday of this week, or of next week if this one is nearly over (Friday, Saturday, Sunday). */
function nextFriday(d: Date, weeksAhead = 0): Date {
  const toFriday = (5 - d.getDay() + 7) % 7 // days until the coming Friday (0 on a Friday)
  const base = toFriday === 0 ? 7 : toFriday
  return addDays(d, base + weeksAhead * 7)
}

/** Quick answers to "when?" for a deadline that was only said as "soon" or "next week". */
export function deadlinePresets(today: Date = new Date()): Preset[] {
  const t = new Date(today.getFullYear(), today.getMonth(), today.getDate())
  const list: Preset[] = [
    { label: 'Tomorrow', date: dayKey(addDays(t, 1)) },
    { label: 'In 3 days', date: dayKey(addDays(t, 3)) },
    { label: 'End of this week', date: dayKey(nextFriday(t)) },
    { label: 'End of next week', date: dayKey(nextFriday(t, 1)) },
    { label: 'End of this month', date: dayKey(new Date(t.getFullYear(), t.getMonth() + 1, 0)) },
  ]
  const seen = new Set<string>()
  return list.filter((p) => {
    if (seen.has(p.date) || p.date <= dayKey(t)) return false // never offer today or the past, nor the same date twice
    seen.add(p.date)
    return true
  })
}
