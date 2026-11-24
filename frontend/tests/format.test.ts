import { describe, expect, it } from 'vitest'
import {
  dayKey,
  daysFromToday,
  fmtBytes,
  fmtClock,
  fmtDue,
  fromServerUtc,
  fromWallClock,
  initials,
  plural,
  relativeDue,
  toApiWallClock,
  toLocalInput,
} from '../src/lib/format'

describe('timestamp kinds', () => {
  it('treats server timestamps as UTC and wall-clock ones as local time', () => {
    const utc = fromServerUtc('2026-11-13T10:00:00')
    expect(utc.toISOString()).toBe('2026-11-13T10:00:00.000Z')
    const wall = fromWallClock('2026-11-13T10:00:00')
    expect([wall.getHours(), wall.getMinutes(), wall.getDate()]).toEqual([10, 0, 13]) // never shifted
  })
  it('respects an explicit offset on server timestamps', () => {
    expect(fromServerUtc('2026-11-13T10:00:00+05:30').toISOString()).toBe('2026-11-13T04:30:00.000Z')
    expect(fromServerUtc('2026-11-13T10:00:00Z').toISOString()).toBe('2026-11-13T10:00:00.000Z')
  })
  it('strips a zone from wall-clock values instead of converting', () => {
    expect(fromWallClock('2026-11-13T10:00:00Z').getHours()).toBe(10)
  })
  it('round-trips datetime-local values', () => {
    expect(toApiWallClock('2026-11-13T10:00')).toBe('2026-11-13T10:00:00')
    expect(toApiWallClock('2026-11-13T10:00:30')).toBe('2026-11-13T10:00:30')
    expect(toLocalInput(new Date(2026, 10, 3, 9, 5))).toBe('2026-11-03T09:05')
    expect(dayKey(new Date(2026, 0, 5))).toBe('2026-01-05')
  })
})

describe('deadlines', () => {
  const today = new Date(2026, 10, 12, 15, 30) // 12 Nov, afternoon
  it('counts whole days regardless of the time of day', () => {
    expect(daysFromToday('2026-11-12', today)).toBe(0)
    expect(daysFromToday('2026-11-13', today)).toBe(1)
    expect(daysFromToday('2026-11-11', today)).toBe(-1)
    expect(daysFromToday('2026-12-12', today)).toBe(30)
  })
  it('words relative due dates for people', () => {
    expect(relativeDue('2026-11-12T00:00:00', today)).toBe('Due today')
    expect(relativeDue('2026-11-13T00:00:00', today)).toBe('Due tomorrow')
    expect(relativeDue('2026-11-16T00:00:00', today)).toBe('Due in 4 days')
    expect(relativeDue('2026-11-11T00:00:00', today)).toBe('1 day overdue')
    expect(relativeDue('2026-11-02T00:00:00', today)).toBe('10 days overdue')
    expect(relativeDue(null, today)).toBe('No deadline')
  })
  it('shows due dates without timezone drift', () => {
    expect(fmtDue('2026-11-13T00:00:00')).toMatch(/13/)
    expect(fmtDue(null)).toBe('No deadline')
  })
  it('handles month and year boundaries', () => {
    expect(daysFromToday('2027-01-01', new Date(2026, 11, 31))).toBe(1)
  })
})

describe('small formatters', () => {
  it('formats clock times', () => {
    expect(fmtClock(0)).toBe('0:00')
    expect(fmtClock(5.9)).toBe('0:05')
    expect(fmtClock(65)).toBe('1:05')
    expect(fmtClock(3725)).toBe('1:02:05')
    expect(fmtClock(-3)).toBe('0:00')
  })
  it('formats byte sizes', () => {
    expect(fmtBytes(500)).toBe('500 B')
    expect(fmtBytes(201_716)).toBe('197 KB')
    expect(fmtBytes(5 * 1024 * 1024)).toBe('5.0 MB')
  })
  it('derives initials and plurals', () => {
    expect(initials('anna.smith@x.com')).toBe('AS')
    expect(initials('bob@x.com')).toBe('B')
    expect(initials('')).toBe('?')
    expect(plural(1, 'task')).toBe('1 task')
    expect(plural(3, 'task')).toBe('3 tasks')
  })
})

describe('fmtLength', () => {
  it('reads naturally from seconds to hours', async () => {
    const { fmtLength } = await import('../src/lib/format')
    expect(fmtLength(6.3)).toBe('6 seconds')
    expect(fmtLength(1)).toBe('1 second')
    expect(fmtLength(0)).toBe('0 seconds')
    expect(fmtLength(60)).toBe('1 min')
    expect(fmtLength(65)).toBe('1 min 5 s')
    expect(fmtLength(3600)).toBe('1 h')
    expect(fmtLength(7440)).toBe('2 h 4 min')
    expect(fmtLength(-5)).toBe('0 seconds')
  })
})
