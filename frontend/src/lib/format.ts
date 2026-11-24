// Date/time helpers.
//
// The backend stores naive timestamps of two kinds, and mixing them up shifts
// times by the viewer's UTC offset:
//   * server-generated (created_at, started_at, recorded-date events): naive UTC
//   * user-entered (scheduled_time, task due dates): naive wall-clock time,
//     which must be shown exactly as entered, never converted.

const HAS_ZONE = /(Z|[+-]\d{2}:?\d{2})$/

/** A server-generated timestamp (naive UTC) as a Date in the viewer's zone. */
export function fromServerUtc(iso: string): Date {
  return new Date(HAS_ZONE.test(iso) ? iso : `${iso}Z`)
}

/** A user-entered wall-clock timestamp, kept exactly as entered. */
export function fromWallClock(iso: string): Date {
  return new Date(iso.replace(HAS_ZONE, ''))
}

export function pad(n: number): string {
  return String(n).padStart(2, '0')
}

/** Local calendar day as YYYY-MM-DD. */
export function dayKey(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

/** YYYY-MM-DD part of a wall-clock string, without any zone conversion. */
export function dayKeyOfWallClock(iso: string): string {
  return iso.slice(0, 10)
}

/** Value for <input type="datetime-local"> from a Date. */
export function toLocalInput(d: Date): string {
  return `${dayKey(d)}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** Wall-clock string for the API from a datetime-local value. */
export function toApiWallClock(localInput: string): string {
  return localInput.length === 16 ? `${localInput}:00` : localInput
}

const dateFmt = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', year: 'numeric' })
const dayFmt = new Intl.DateTimeFormat(undefined, { weekday: 'short', day: 'numeric', month: 'short' })
const timeFmt = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' })
const monthFmt = new Intl.DateTimeFormat(undefined, { month: 'long', year: 'numeric' })

export const fmtDate = (d: Date) => dateFmt.format(d)
export const fmtDay = (d: Date) => dayFmt.format(d)
export const fmtTime = (d: Date) => timeFmt.format(d)
export const fmtMonth = (d: Date) => monthFmt.format(d)
export const fmtDateTime = (d: Date) => `${dateFmt.format(d)}, ${timeFmt.format(d)}`

/** A task due date (date-only semantics) for display. */
export function fmtDue(iso: string | null): string {
  if (!iso) return 'No deadline'
  const [y, m, d] = iso.slice(0, 10).split('-').map(Number)
  return dayFmt.format(new Date(y, m - 1, d))
}

/** Whole days from today (local) to a YYYY-MM-DD day; negative = in the past. */
export function daysFromToday(dayStr: string, today: Date = new Date()): number {
  const [y, m, d] = dayStr.split('-').map(Number)
  const a = Date.UTC(y, m - 1, d)
  const b = Date.UTC(today.getFullYear(), today.getMonth(), today.getDate())
  return Math.round((a - b) / 86_400_000)
}

export function relativeDue(iso: string | null, today: Date = new Date()): string {
  if (!iso) return 'No deadline'
  const n = daysFromToday(iso.slice(0, 10), today)
  if (n === 0) return 'Due today'
  if (n === 1) return 'Due tomorrow'
  if (n === -1) return '1 day overdue'
  if (n < 0) return `${-n} days overdue`
  return `Due in ${n} days`
}

/** Seconds as m:ss or h:mm:ss. */
export function fmtClock(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`
}

/** "6 seconds", "1 min 5 s", "2 h 4 min": a human length for a recording. */
export function fmtLength(totalSeconds: number): string {
  const s = Math.max(0, Math.round(totalSeconds))
  if (s < 60) return `${s} second${s === 1 ? '' : 's'}`
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  if (h > 0) return m ? `${h} h ${m} min` : `${h} h`
  return sec ? `${m} min ${sec} s` : `${m} min`
}

export function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

export function initials(email: string): string {
  const name = email.split('@')[0].replace(/[^a-zA-Z0-9]+/g, ' ').trim()
  const parts = name.split(' ').filter(Boolean)
  if (parts.length === 0) return '?'
  return (parts[0][0] + (parts[1]?.[0] ?? '')).toUpperCase()
}

export function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? '' : 's'}`
}

/** "just now", "5 min ago", "3 h ago", "2 days ago", then a date. */
export function ago(iso: string, now: Date = new Date()): string {
  const s = Math.max(0, Math.round((now.getTime() - fromServerUtc(iso).getTime()) / 1000))
  if (s < 45) return 'just now'
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`
  if (s < 86400) return `${Math.round(s / 3600)} h ago`
  if (s < 7 * 86400) return `${Math.round(s / 86400)} day${Math.round(s / 86400) === 1 ? '' : 's'} ago`
  return fromServerUtc(iso).toLocaleDateString([], { day: 'numeric', month: 'short' })
}
