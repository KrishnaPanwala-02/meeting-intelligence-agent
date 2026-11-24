import { describe, expect, it } from 'vitest'
import type { CalendarEvent, Meeting, Summary, TaskRow, Transcript } from '../src/api/types'
import { eventDay, eventsByDay, gridRange, monthGrid } from '../src/lib/calendar'
import {
  highlightParts,
  meetingToMarkdown,
  safeFilename,
  speakerClass,
  speakerOrder,
  transcriptToText,
} from '../src/lib/export'
import { bucketOf, filterPriority, groupTasks, sortTasks, type SortKey } from '../src/lib/tasks'

const task = (over: Partial<TaskRow>): TaskRow => ({
  id: 'a', task: 't', assignee: null, due_date: null, priority: 'medium', status: 'pending',
  created_at: null, completed_at: null, meeting_id: 'm', meeting_title: 'M', can_write: true, mine: false, can_update_status: true, ...over,
})

describe('task buckets', () => {
  const today = new Date(2026, 10, 12)
  it('puts tasks in the right urgency group', () => {
    expect(bucketOf(task({ due_date: '2026-11-11T00:00:00' }), today)).toBe('overdue')
    expect(bucketOf(task({ due_date: '2026-11-12T00:00:00' }), today)).toBe('today')
    expect(bucketOf(task({ due_date: '2026-11-19T00:00:00' }), today)).toBe('week')
    expect(bucketOf(task({ due_date: '2026-11-20T00:00:00' }), today)).toBe('later')
    expect(bucketOf(task({ due_date: null }), today)).toBe('nodate')
  })
  it('finished tasks are never overdue', () => {
    expect(bucketOf(task({ status: 'completed', due_date: '2026-01-01T00:00:00' }), today)).toBe('done')
  })
  it('groups a list', () => {
    const g = groupTasks([task({ id: '1', due_date: '2026-11-01T00:00:00' }), task({ id: '2' }), task({ id: '3', status: 'completed' })], today)
    expect(g.overdue.map((t) => t.id)).toEqual(['1'])
    expect(g.nodate.map((t) => t.id)).toEqual(['2'])
    expect(g.done.map((t) => t.id)).toEqual(['3'])
  })
})

describe('calendar grid', () => {
  it('always has 42 cells starting on a Monday', () => {
    for (const [y, m] of [[2026, 10], [2026, 1], [2027, 0], [2024, 1]]) {
      const g = monthGrid(y, m)
      expect(g).toHaveLength(42)
      expect(g[0].date.getDay()).toBe(1)
      expect(g.filter((d) => d.inMonth)).toHaveLength(new Date(y, m + 1, 0).getDate())
    }
  })
  it('November 2026 starts on Sunday so the grid begins the Monday before', () => {
    const g = monthGrid(2026, 10)
    expect(g[0].key).toBe('2026-10-26')
    expect(g[6].key).toBe('2026-11-01')
    expect(gridRange(g)).toEqual({ start: '2026-10-26', end: '2026-12-06' })
  })
  const ev = (over: Partial<CalendarEvent>): CalendarEvent => ({
    id: 'e', summary: 's', start: '2026-11-13T10:00:00', end: '2026-11-13T11:00:00', status: 'scheduled',
    type: 'meeting', meeting_id: 'm', calendar_id: 'internal', date_source: 'scheduled', ...over,
  })
  it('keeps scheduled meetings and deadlines on the day they were entered', () => {
    expect(eventDay(ev({ start: '2026-11-13T23:30:00' }))).toBe('2026-11-13')
    expect(eventDay(ev({ type: 'submission', start: '2026-11-13T00:00:00', date_source: undefined }))).toBe('2026-11-13')
  })
  it('converts recorded/started meetings from UTC to the viewer’s day', () => {
    const e = ev({ date_source: 'recorded', start: '2026-11-13T10:00:00' })
    expect(eventDay(e)).toBe(new Date('2026-11-13T10:00:00Z').toLocaleDateString('en-CA'))
  })
  it('groups by day and sorts within a day', () => {
    const map = eventsByDay([
      ev({ id: 'b', start: '2026-11-13T15:00:00' }),
      ev({ id: 'a', start: '2026-11-13T09:00:00' }),
      ev({ id: 'c', start: '2026-11-14T09:00:00' }),
    ])
    expect(map.get('2026-11-13')!.map((e) => e.id)).toEqual(['a', 'b'])
    expect(map.get('2026-11-14')!.map((e) => e.id)).toEqual(['c'])
  })
})

const transcript: Transcript = {
  meeting_id: 'm', transcript_id: 't', language: 'en', transcript: '', word_count: 19, speaker_count: 2, created_at: null,
  segments: [
    { id: 0, start_time: 0, end_time: 3.5, text: 'I will finish the report by Friday.', speaker: 'Speaker 2', confidence: 0.8 },
    { id: 1, start_time: 65, end_time: 70, text: 'Sounds good.', speaker: 'Speaker 1', confidence: 0.8 },
    { id: 2, start_time: 71, end_time: 72, text: 'Unlabelled.', speaker: null, confidence: 0.8 },
  ],
}

describe('transcript helpers', () => {
  it('orders speakers by first appearance and assigns stable colours', () => {
    const order = speakerOrder(transcript)
    expect(order).toEqual(['Speaker 2', 'Speaker 1'])
    expect(speakerClass(order, 'Speaker 2')).toBe('sp0')
    expect(speakerClass(order, 'Speaker 1')).toBe('sp1')
    expect(speakerClass(order, null)).toBe('sp5')
    expect(speakerClass(order, 'Someone else')).toBe('sp5')
    expect(speakerOrder(undefined)).toEqual([])
  })
  it('renders plain text with timestamps', () => {
    expect(transcriptToText(transcript).split('\n')).toEqual([
      '[0:00] Speaker 2: I will finish the report by Friday.',
      '[1:05] Speaker 1: Sounds good.',
      '[1:11] Unlabelled.',
    ])
  })
  it('highlights search terms safely, including regex characters', () => {
    expect(highlightParts('Pay $5 (now)', '(now)')).toEqual([{ text: 'Pay $5 ', match: false }, { text: '(now)', match: true }])
    expect(highlightParts('Report REPORT report', 'report').filter((p) => p.match)).toHaveLength(3)
    expect(highlightParts('abc', '')).toEqual([{ text: 'abc', match: false }])
    expect(highlightParts('abc', '   ')).toEqual([{ text: 'abc', match: false }])
  })
})

describe('markdown export', () => {
  const meeting = { id: 'm', title: 'Weekly sync', participants: ['a@x.com', 'b@x.com'] } as Meeting
  const summary: Summary = {
    meeting_id: 'm', summary_id: 's', meeting_title: 'Weekly sync', executive_summary: 'Two commitments were made.',
    key_points: [{ topic: 'Report', description: 'Due Friday' }],
    decisions: [{ decision: 'Ship v2', owner: 'Anna', context: '' }, { decision: 'Cut scope', owner: 'TBD', context: '' }],
    next_steps: ['Finish report'], key_quotes: [], created_at: null,
  }
  const actions = [
    { id: '1', task: 'Finish report', assignee: 'Speaker 1', due_date: '2026-11-13T00:00:00', priority: 'high', status: 'completed', created_at: null, completed_at: null },
    { id: '2', task: 'Book room', assignee: 'TBD', due_date: null, priority: 'low', status: 'pending', created_at: null, completed_at: null },
  ]
  it('writes a complete, readable document', () => {
    const md = meetingToMarkdown(meeting, summary, actions, transcript)
    expect(md).toContain('# Weekly sync')
    expect(md).toContain('**Participants:** a@x.com, b@x.com')
    expect(md).toContain('Two commitments were made.')
    expect(md).toContain('- **Report:** Due Friday')
    expect(md).toContain('- Ship v2 (Anna)')
    expect(md).toContain('- Cut scope\n')
    expect(md).toContain('- [x] Finish report — Speaker 1')
    expect(md).toContain('- [ ] Book room (No deadline)')
    expect(md).toContain('[1:05] Speaker 1: Sounds good.')
  })
  it('copes with missing parts', () => {
    expect(meetingToMarkdown({ id: 'm', title: 'Bare', participants: [] } as unknown as Meeting)).toBe('# Bare\n')
  })
  it('makes safe file names', () => {
    expect(safeFilename('Q4 plan / "final"?')).toBe('Q4-plan-final')
    expect(safeFilename('???')).toBe('meeting')
  })
})

describe('sortTasks', () => {
  const rows = [
    task({ id: '1', task: 'banana report', assignee: 'Rahul Patel', due_date: '2026-11-20T00:00:00', priority: 'low', meeting_title: 'Zomato', created_at: '2026-11-01T10:00:00' }),
    task({ id: '2', task: 'Apple plan', assignee: 'Krishna Panwala', due_date: '2026-11-16T00:00:00', priority: 'high', meeting_title: 'Swiggy', created_at: '2026-11-03T10:00:00' }),
    task({ id: '3', task: 'cherry notes', assignee: 'Speaker 2', due_date: null, priority: 'medium', meeting_title: 'Amazon', created_at: '2026-11-02T10:00:00' }),
    task({ id: '4', task: 'date night', assignee: 'TBD', due_date: '2026-11-16T00:00:00', priority: 'high', meeting_title: 'Swiggy', created_at: '2026-11-04T10:00:00' }),
  ]
  const ids = (key: SortKey) => sortTasks(rows, key).map((t) => t.id)

  it('by deadline, undated last, ties by name', () => expect(ids('due')).toEqual(['2', '4', '1', '3']))
  it('by name, ignoring case', () => expect(ids('name')).toEqual(['2', '1', '3', '4']))
  it('by assignee, named people before unassigned', () => expect(ids('assignee')).toEqual(['2', '1', '4', '3']))
  it('by priority, high first', () => expect(ids('priority')).toEqual(['2', '4', '3', '1']))
  it('by meeting', () => expect(ids('meeting')).toEqual(['3', '2', '4', '1']))
  it('by newest first', () => expect(ids('created')).toEqual(['4', '2', '3', '1']))
  it('does not modify the input', () => {
    const before = rows.map((t) => t.id)
    sortTasks(rows, 'name')
    expect(rows.map((t) => t.id)).toEqual(before)
  })
  it('filters by priority', () => {
    expect(filterPriority(rows, 'high').map((t) => t.id)).toEqual(['2', '4'])
    expect(filterPriority(rows, 'all')).toHaveLength(4)
  })
})
