import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { onSessionLost, tokens } from '../src/api/client'
import { highlightTerms } from '../src/lib/text'
import { hitLink } from '../src/pages/SearchPage'
import { emptyApp, jsonResponse, meHandler, mockServer, pair, renderApp } from './helpers'

beforeEach(() => {
  tokens.clear()
  onSessionLost(null)
  tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
  Element.prototype.scrollIntoView = vi.fn()
})
afterEach(() => {
  vi.unstubAllGlobals()
})

describe('highlightTerms', () => {
  const joined = (parts: { text: string; match: boolean }[]) => parts.map((p) => (p.match ? `[${p.text}]` : p.text)).join('')
  it('marks every term, ignoring case, and keeps the original spelling', () => {
    expect(joined(highlightTerms('Rahul writes the Project Report', ['rahul', 'project report']))).toBe('[Rahul] writes the [Project Report]')
  })
  it('prefers the longer term when terms overlap', () => {
    expect(joined(highlightTerms('the report needs reports', ['report', 'reports']))).toBe('the [report] needs [reports]')
  })
  it('treats terms literally and ignores empty ones', () => {
    expect(joined(highlightTerms('C++ (v2) is fine.', ['c++', '(v2)']))).toBe('[C++] [(v2)] is fine.')
    expect(highlightTerms('plain', [])).toEqual([{ text: 'plain', match: false }])
    expect(highlightTerms('plain', ['', '  '])).toEqual([{ text: 'plain', match: false }])
  })
})

describe('hitLink', () => {
  it('opens the right tab, and the exact line for a transcript match', () => {
    expect(hitLink('m1', { kind: 'transcript', text: 'x', segment_id: 3 })).toBe('/meetings/m1?tab=transcript&seg=3')
    expect(hitLink('m1', { kind: 'transcript', text: 'x', segment_id: 0 })).toBe('/meetings/m1?tab=transcript&seg=0')
    expect(hitLink('m1', { kind: 'transcript', text: 'x' })).toBe('/meetings/m1?tab=transcript')
    expect(hitLink('m1', { kind: 'task', text: 'x' })).toBe('/meetings/m1?tab=actions')
    expect(hitLink('m1', { kind: 'summary', text: 'x' })).toBe('/meetings/m1?tab=summary')
    expect(hitLink('m1', { kind: 'title', text: 'x' })).toBe('/meetings/m1')
  })
})

const results = {
  query: 'report', terms: ['report'], total: 9,
  meetings: [
    {
      meeting_id: 'm1', title: 'ZOMATO PROJECT', date: '2026-11-13', hit_count: 9,
      hits: [
        { kind: 'summary', text: 'Rahul writes the project report.' },
        { kind: 'task', text: 'Prepare the project report', assignee: 'Rahul Patel', status: 'in_progress', due: '2026-11-16' },
        { kind: 'transcript', text: 'I will prepare the project report by Monday.', speaker: 'Rahul Patel', segment_id: 1, time: 75 },
      ],
    },
    { meeting_id: 'm2', title: 'Weekly report review', date: null, hit_count: 1, hits: [{ kind: 'title', text: 'Weekly report review' }] },
  ],
}

function setup(reply: (q: string) => unknown) {
  return mockServer({
    ...emptyApp,
    'GET /auth/me': meHandler,
    'GET /api/v1/search': ({ url }) => reply(new URL(url, 'http://x').searchParams.get('q') ?? ''),
  })
}

describe('the Search page', () => {
  it('is in the menu and explains itself before you type', async () => {
    const { calls } = setup(() => results)
    renderApp('/search')
    expect(await screen.findByText('Search all your meetings')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Search' })).toHaveAttribute('href', '/search')
    expect(calls.some((c) => c.url.startsWith('/api/v1/search'))).toBe(false)
  })

  it('searches as you type (after a pause) and shows grouped, highlighted results', async () => {
    const user = userEvent.setup()
    const { calls } = setup(() => results)
    renderApp('/search')
    await user.type(await screen.findByLabelText('Search your meetings'), 'report')
    expect(await screen.findByText('9 matches in 2 meetings', {}, { timeout: 3000 })).toBeInTheDocument()
    expect(calls.filter((c) => c.url.startsWith('/api/v1/search')).map((c) => c.url)).toEqual(['/api/v1/search?q=report']) // one request, not one per key

    const zomato = screen.getByRole('region', { name: 'ZOMATO PROJECT' })
    expect(within(zomato).getByText('Summary')).toBeInTheDocument()
    expect(within(zomato).getByText('Task')).toBeInTheDocument()
    expect(within(zomato).getByText('Transcript')).toBeInTheDocument()
    expect(within(zomato).getByText('Rahul Patel · 1:15')).toBeInTheDocument()
    expect(within(zomato).getByText('Rahul Patel · in progress · due 2026-11-16')).toBeInTheDocument()
    expect(zomato.querySelectorAll('mark').length).toBeGreaterThanOrEqual(3)
    expect(within(zomato).getByText(/and 6 more in this meeting/)).toBeInTheDocument()
    expect(screen.getByText('· 1 match')).toBeInTheDocument()
  })

  it('every result links to the right place in the meeting', async () => {
    setup(() => results)
    renderApp('/search?q=report')
    const zomato = await screen.findByRole('region', { name: 'ZOMATO PROJECT' })
    const hrefs = within(zomato).getAllByRole('link').map((a) => a.getAttribute('href'))
    expect(hrefs).toEqual(expect.arrayContaining([
      '/meetings/m1', '/meetings/m1?tab=summary', '/meetings/m1?tab=actions', '/meetings/m1?tab=transcript&seg=1',
    ]))
  })

  it('a search in the address runs straight away, and typing keeps the address in step', async () => {
    const user = userEvent.setup()
    const { calls } = setup((q) => ({ ...results, query: q, terms: [q] }))
    renderApp('/search?q=budget')
    expect(await screen.findByLabelText('Search your meetings')).toHaveValue('budget')
    await screen.findByRole('region', { name: 'ZOMATO PROJECT' })
    expect(calls.find((c) => c.url.startsWith('/api/v1/search'))!.url).toBe('/api/v1/search?q=budget')
    await user.clear(screen.getByLabelText('Search your meetings'))
    await user.type(screen.getByLabelText('Search your meetings'), 'plan')
    await waitFor(() => expect(calls.some((c) => c.url === '/api/v1/search?q=plan')).toBe(true), { timeout: 3000 })
    expect(await screen.findByText('9 matches in 2 meetings')).toBeInTheDocument()
  })

  it('does not search for one letter', async () => {
    const user = userEvent.setup()
    const { calls } = setup(() => results)
    renderApp('/search')
    await user.type(await screen.findByLabelText('Search your meetings'), 'a')
    await new Promise((r) => setTimeout(r, 500))
    expect(calls.some((c) => c.url.startsWith('/api/v1/search'))).toBe(false)
    expect(screen.getByText('Search all your meetings')).toBeInTheDocument()
  })

  it('says so, with advice, when nothing matches', async () => {
    setup(() => ({ query: 'xyzzy plugh', terms: ['xyzzy', 'plugh'], total: 0, meetings: [] }))
    renderApp('/search?q=xyzzy plugh')
    expect(await screen.findByText('No matches')).toBeInTheDocument()
    expect(screen.getByText(/“xyzzy” and “plugh”/)).toBeInTheDocument()
  })

  it('shows an error with a retry', async () => {
    const user = userEvent.setup()
    let fail = true
    const { calls } = setup(() => (fail ? jsonResponse({ detail: 'The server had a problem. Please try again.' }, 500) : results))
    renderApp('/search?q=report')
    expect(await screen.findByRole('alert')).toHaveTextContent('The server had a problem')
    fail = false
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('9 matches in 2 meetings')).toBeInTheDocument()
    expect(calls.filter((c) => c.url.startsWith('/api/v1/search')).length).toBeGreaterThanOrEqual(2)
  })

  it('shows singular wording for one match in one meeting', async () => {
    setup(() => ({ query: 'weekly', terms: ['weekly'], total: 1, meetings: [results.meetings[1]] }))
    renderApp('/search?q=weekly')
    expect(await screen.findByText('1 match in 1 meeting')).toBeInTheDocument()
  })
})

describe('opening a meeting from a search result', () => {
  const segments = [0, 1, 2].map((i) => ({ id: i, start_time: i * 10, end_time: i * 10 + 8, text: `line number ${i}`, speaker: 'Speaker 1', confidence: 0.9 }))
  const meeting = {
    id: 'm1', title: 'Validation', status: 'uploaded', scheduled_time: null, end_time: null, duration_minutes: 1, participants: [], owner_email: 'x@y.com',
    owner_name: null, is_owner: true, started_at: null, ended_at: null, has_audio: false, created_at: '2026-11-13T12:00:00', can_write: true,
    has_transcript: true, has_summary: true, action_count: 1, open_action_count: 1,
  }
  const server = () =>
    mockServer({
      ...emptyApp,
      'GET /auth/me': meHandler,
      '/api/v1/meetings/m1': () => meeting,
      '/api/v1/meetings/m1/jobs/latest': () => jsonResponse({ detail: 'none' }, 404),
      '/api/v1/meetings/m1/transcript': () => ({ meeting_id: 'm1', transcript_id: 't', language: 'en', transcript: '', word_count: 9, speaker_count: 1, created_at: null, segments }),
      '/api/v1/meetings/m1/summary': () => ({ meeting_id: 'm1', executive_summary: 'x', key_points: [], decisions: [], next_steps: [], key_quotes: [], created_at: null }),
      '/api/v1/meetings/m1/actions': () => ({ meeting_id: 'm1', action_items: [], count: 0 }),
      '/api/v1/meetings/m1/speakers': () => ({ meeting_id: 'm1', voices_available: false, speakers: [] }),
      '/api/v1/voice-profiles': () => ({ count: 0, profiles: [] }),
    })

  it('opens on the transcript tab and highlights the matching line', async () => {
    server()
    renderApp('/meetings/m1?tab=transcript&seg=1')
    const line = await screen.findByText('line number 1')
    expect(screen.getByRole('tab', { name: 'Transcript' })).toHaveAttribute('aria-selected', 'true')
    const row = line.closest('[data-seg]') as HTMLElement
    expect(row).toHaveAttribute('data-seg', '1')
    await waitFor(() => expect(row.className).toContain('flash'))
    expect(screen.getByText('line number 0').closest('[data-seg]')!.className).not.toContain('flash')
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled()
  })

  it('opens on the tasks tab when asked', async () => {
    server()
    renderApp('/meetings/m1?tab=actions')
    expect(await screen.findByRole('tab', { name: /Action items/ })).toHaveAttribute('aria-selected', 'true')
  })

  it('ignores a made-up tab or line number', async () => {
    server()
    renderApp('/meetings/m1?tab=nonsense&seg=abc')
    expect(await screen.findByRole('tab', { name: 'Summary' })).toHaveAttribute('aria-selected', 'true') // the usual default
    expect(document.querySelector('.flash')).toBeNull()
  })
})
