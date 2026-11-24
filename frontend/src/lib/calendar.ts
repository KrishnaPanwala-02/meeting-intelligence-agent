import type { CalendarEvent } from '../api/types'
import { dayKey, dayKeyOfWallClock, fromServerUtc } from './format'

export interface GridDay {
  date: Date
  key: string
  inMonth: boolean
}

/** Six Monday-first weeks covering the month (a stable 42-cell grid). */
export function monthGrid(year: number, month0: number): GridDay[] {
  const first = new Date(year, month0, 1)
  const offset = (first.getDay() + 6) % 7 // Monday = 0
  const start = new Date(year, month0, 1 - offset)
  const days: GridDay[] = []
  for (let i = 0; i < 42; i++) {
    const d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i)
    days.push({ date: d, key: dayKey(d), inMonth: d.getMonth() === month0 })
  }
  return days
}

/** The local calendar day an event belongs to. */
export function eventDay(e: CalendarEvent): string {
  // Meetings placed by their recording/start time are naive UTC; scheduled
  // meetings and task deadlines are wall-clock and must not be shifted.
  if (e.type === 'meeting' && (e.date_source === 'recorded' || e.date_source === 'started')) {
    return dayKey(fromServerUtc(e.start))
  }
  return dayKeyOfWallClock(e.start)
}

export function eventsByDay(events: CalendarEvent[]): Map<string, CalendarEvent[]> {
  const map = new Map<string, CalendarEvent[]>()
  for (const e of events) {
    const k = eventDay(e)
    const list = map.get(k)
    if (list) list.push(e)
    else map.set(k, [e])
  }
  for (const list of map.values()) list.sort((a, b) => a.start.localeCompare(b.start))
  return map
}

/** Range to request from the API for a month grid (inclusive YYYY-MM-DD). */
export function gridRange(grid: GridDay[]): { start: string; end: string } {
  return { start: grid[0].key, end: grid[grid.length - 1].key }
}
