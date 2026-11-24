import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { onSessionLost, tokens } from '../src/api/client'
import type { ActionItem, Meeting, Summary, Transcript } from '../src/api/types'
import ExportMenu from '../src/components/ExportMenu'
import ShareDialog from '../src/components/ShareDialog'
import { ToastProvider } from '../src/components/ui'
import { escapeHtml, meetingToHtml, printHtml, summaryToPlainText } from '../src/lib/export'
import { emptyApp, jsonResponse, meHandler, mockServer, pair, renderApp } from './helpers'

const meeting = {
  id: 'm1', title: 'ZOMATO <PROJECT> & "co"', status: 'uploaded', scheduled_time: null, end_time: null, duration_minutes: 1,
  participants: ['rahul@example.com', 'Anna', 'sam@example.com'], owner_email: 'k@example.com', owner_name: 'Krishna Panwala', is_owner: true,
  started_at: null, ended_at: null, has_audio: false, created_at: '2026-11-13T12:00:00', can_write: true,
  has_transcript: true, has_summary: true, action_count: 2, open_action_count: 1,
} as unknown as Meeting
const summary = {
  meeting_id: 'm1', executive_summary: 'Krishna trains the model <b>today</b>.', created_at: null,
  key_points: [{ topic: 'Model', description: 'Training ends Friday' }],
  decisions: [{ decision: 'Ship Friday', owner: 'Rahul Patel', context: '' }, { decision: 'Anyone', owner: 'TBD', context: '' }],
  next_steps: ['Rahul sends the invite'], key_quotes: [],
} as unknown as Summary
const actions = [
  { id: 'a', task: 'Write the report', assignee: 'Rahul Patel', due_date: '2026-11-16T00:00:00', priority: 'high', status: 'pending', created_at: null, completed_at: null },
  { id: 'b', task: 'Book the room', assignee: 'TBD', due_date: null, priority: 'low', status: 'completed', created_at: null, completed_at: null },
] as ActionItem[]
const transcript = {
  meeting_id: 'm1', transcript_id: 't', language: 'en', transcript: '', word_count: 4, speaker_count: 1, created_at: null,
  segments: [{ id: 0, start_time: 65, end_time: 70, text: 'Hello <script>alert(1)</script> team', speaker: 'Speaker 1', confidence: 0.9 }],
} as unknown as Transcript

let downloads: { name: string; blobSize?: number }[]

beforeEach(() => {
  tokens.clear()
  onSessionLost(null)
  tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
  downloads = []
  URL.createObjectURL = vi.fn((b: Blob) => `blob:${b.size}`)
  URL.revokeObjectURL = vi.fn()
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    downloads.push({ name: this.download })
  })
})
afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('export helpers', () => {
  it('escapeHtml neutralises markup and quotes', () => {
    expect(escapeHtml(`<img src=x onerror="a('b')"> & </p>`)).toBe('&lt;img src=x onerror=&quot;a(&#39;b&#39;)&quot;&gt; &amp; &lt;/p&gt;')
    expect(escapeHtml(null)).toBe('')
  })

  it('the printable page has every section and never lets text become markup', () => {
    const html = meetingToHtml(meeting, summary, actions, transcript)
    for (const needle of ['<h2>Summary</h2>', '<h2>Key points</h2>', '<h2>Decisions</h2>', '<h2>Next steps</h2>', '<h2>Action items</h2>', '<h2>Transcript</h2>', 'Write the report', 'Rahul Patel', '1:05'])
      expect(html).toContain(needle)
    expect(html).not.toContain('<script>alert(1)</script>')
    expect(html).not.toContain('<b>today</b>')
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;')
    expect(html).toContain('<title>ZOMATO &lt;PROJECT&gt; &amp; &quot;co&quot;</title>')
    expect(html).not.toMatch(/<(link|img|iframe)\b|src=|href=/) // self-contained: nothing is fetched
    expect(meetingToHtml(meeting)).not.toContain('<h2>')
  })

  it('the e-mail text has the summary, decisions, steps and tasks', () => {
    const t = summaryToPlainText(meeting, summary, actions)
    expect(t).toContain('Summary of “ZOMATO <PROJECT> & "co"”')
    expect(t).toContain('- Ship Friday (Rahul Patel)')
    expect(t).toContain('- Anyone\n')
    expect(t).toContain('- Write the report — Rahul Patel (')
    expect(t).toContain('- Book the room (')
    expect(t).toContain('[done]')
    expect(t.endsWith('\n')).toBe(true)
  })

  it('printHtml opens a window, writes the page and prints; reports a blocked pop-up', () => {
    vi.useFakeTimers()
    const w = { document: { open: vi.fn(), write: vi.fn(), close: vi.fn() }, focus: vi.fn(), print: vi.fn() }
    const open = vi.spyOn(window, 'open').mockReturnValue(w as unknown as Window)
    expect(printHtml('<p>hi</p>')).toBe(true)
    expect(w.document.write).toHaveBeenCalledWith('<p>hi</p>')
    vi.advanceTimersByTime(400)
    expect(w.print).toHaveBeenCalled()
    open.mockReturnValue(null)
    expect(printHtml('<p>x</p>')).toBe(false)
    vi.useRealTimers()
  })
})

function renderMenu(over: Partial<{ summary: Summary | null }> = {}) {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <ExportMenu meeting={meeting} summary={over.summary === undefined ? summary : over.summary} actions={actions} transcript={transcript} />
      </ToastProvider>
    </MemoryRouter>,
  )
}

describe('the Export menu', () => {
  it('lists the ways to export, and closes on Escape or a click elsewhere', async () => {
    const user = userEvent.setup()
    mockServer({})
    renderMenu()
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /Export/ }))
    const menu = screen.getByRole('menu')
    for (const n of ['Markdown file (.md)', 'PDF (print or save as PDF)', 'Add to calendar (.ics)', 'E-mail the summary…']) expect(within(menu).getByRole('menuitem', { name: n })).toBeInTheDocument()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /Export/ }))
    await user.click(document.body)
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })

  it('downloads Markdown named after the meeting', async () => {
    const user = userEvent.setup()
    mockServer({})
    renderMenu()
    await user.click(screen.getByRole('button', { name: /Export/ }))
    await user.click(screen.getByRole('menuitem', { name: 'Markdown file (.md)' }))
    expect(downloads.map((d) => d.name)).toEqual(['ZOMATO-PROJECT-co.md'])
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })

  it('prints the meeting as a PDF, or explains a blocked pop-up', async () => {
    const user = userEvent.setup()
    mockServer({})
    renderMenu()
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    await user.click(screen.getByRole('button', { name: /Export/ }))
    await user.click(screen.getByRole('menuitem', { name: 'PDF (print or save as PDF)' }))
    expect(await screen.findByText(/blocked the print window/)).toBeInTheDocument()
    const w = { document: { open: vi.fn(), write: vi.fn(), close: vi.fn() }, focus: vi.fn(), print: vi.fn() }
    open.mockReturnValue(w as unknown as Window)
    await user.click(screen.getByRole('button', { name: /Export/ }))
    await user.click(screen.getByRole('menuitem', { name: 'PDF (print or save as PDF)' }))
    expect(w.document.write).toHaveBeenCalledOnce()
    expect(String(w.document.write.mock.calls[0][0])).toContain('<h2>Summary</h2>')
  })

  it('fetches the calendar file with the user’s sign-in and downloads it', async () => {
    const user = userEvent.setup()
    const { calls } = mockServer({ '/api/v1/meetings/m1/calendar.ics': () => ({ ok: 1 }) })
    renderMenu()
    await user.click(screen.getByRole('button', { name: /Export/ }))
    await user.click(screen.getByRole('menuitem', { name: 'Add to calendar (.ics)' }))
    await waitFor(() => expect(downloads.map((d) => d.name)).toEqual(['ZOMATO-PROJECT-co.ics']))
    expect(calls[0].url).toBe('/api/v1/meetings/m1/calendar.ics')
  })

  it('shows why a calendar download failed', async () => {
    const user = userEvent.setup()
    mockServer({ '/api/v1/meetings/m1/calendar.ics': () => jsonResponse({ detail: 'Meeting not found' }, 404) })
    renderMenu()
    await user.click(screen.getByRole('button', { name: /Export/ }))
    await user.click(screen.getByRole('menuitem', { name: 'Add to calendar (.ics)' }))
    expect(await screen.findByText('Meeting not found')).toBeInTheDocument()
    expect(downloads).toEqual([])
  })

  it('offers e-mail only when there is a summary', async () => {
    const user = userEvent.setup()
    mockServer({})
    renderMenu({ summary: null })
    await user.click(screen.getByRole('button', { name: /Export/ }))
    expect(screen.queryByRole('menuitem', { name: 'E-mail the summary…' })).not.toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Markdown file (.md)' })).toBeInTheDocument()
  })
})

function renderShare(emailOn: boolean, over: Record<string, unknown> = {}) {
  const server = mockServer({
    'GET /api/v1/features': () => ({ email_enabled: emailOn }),
    'POST /api/v1/meetings/m1/send-summary': () => ({ sent: ['rahul@example.com', 'sam@example.com'], failed: [] }),
    ...over,
  } as never)
  const onClose = vi.fn()
  render(
    <MemoryRouter>
      <ToastProvider>
        <ShareDialog meeting={meeting} summary={summary} actions={actions} onClose={onClose} />
      </ToastProvider>
    </MemoryRouter>,
  )
  return { ...server, onClose }
}

describe('the e-mail dialog', () => {
  it('when the server cannot send mail, explains it and still offers the mail app', async () => {
    renderShare(false)
    expect(await screen.findByText(/not set up to send e-mail/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    const link = screen.getByRole('link', { name: 'Open in mail app' })
    const href = link.getAttribute('href')!
    expect(href.startsWith('mailto:rahul%40example.com,sam%40example.com?subject=')).toBe(true)
    expect(href).toContain('subject=Summary%3A%20ZOMATO')
    expect(decodeURIComponent(href.split('&body=')[1])).toContain('Krishna trains the model')
  })

  it('lists only participants that are e-mail addresses, all ticked, and follows the choices', async () => {
    const user = userEvent.setup()
    renderShare(false)
    await screen.findByText(/not set up/)
    expect(screen.getByRole('checkbox', { name: 'rahul@example.com' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'sam@example.com' })).toBeChecked()
    expect(screen.queryByText('Anna')).not.toBeInTheDocument() // a name, not an address
    await user.click(screen.getByRole('checkbox', { name: 'sam@example.com' }))
    await user.click(screen.getByRole('checkbox', { name: /Send me a copy/ }))
    expect(screen.getByRole('link', { name: 'Open in mail app' }).getAttribute('href')!.startsWith('mailto:rahul%40example.com,k%40example.com?')).toBe(true)
  })

  it('with nobody chosen there is nobody to send to', async () => {
    const user = userEvent.setup()
    renderShare(true)
    await screen.findByRole('checkbox', { name: 'rahul@example.com' })
    await user.click(screen.getByRole('checkbox', { name: 'rahul@example.com' }))
    await user.click(screen.getByRole('checkbox', { name: 'sam@example.com' }))
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    expect(screen.getByRole('link', { name: 'Open in mail app' })).toHaveAttribute('aria-disabled', 'true')
  })

  it('sends through the server with the chosen people and the message', async () => {
    const user = userEvent.setup()
    const { calls, onClose } = renderShare(true)
    await screen.findByRole('checkbox', { name: 'rahul@example.com' })
    expect(screen.queryByText(/not set up to send e-mail/)).not.toBeInTheDocument()
    await user.type(screen.getByLabelText(/Message/), 'Please check your tasks.')
    await user.click(screen.getByRole('checkbox', { name: 'sam@example.com' }))
    await user.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ recipients: ['rahul@example.com'], note: 'Please check your tasks.' })
    expect(await screen.findByText(/Sent to rahul@example.com, sam@example.com/)).toBeInTheDocument()
  })

  it('reports people who could not be reached', async () => {
    const user = userEvent.setup()
    renderShare(true, { 'POST /api/v1/meetings/m1/send-summary': () => ({ sent: ['rahul@example.com'], failed: ['sam@example.com'] }) })
    await user.click(await screen.findByRole('button', { name: 'Send' }))
    expect(await screen.findByText(/Could not reach sam@example.com/)).toBeInTheDocument()
  })

  it('shows the server’s reason and stays open when sending fails', async () => {
    const user = userEvent.setup()
    const { onClose } = renderShare(true, { 'POST /api/v1/meetings/m1/send-summary': () => jsonResponse({ detail: 'The e-mail server did not accept the messages. Check the SMTP settings.' }, 502) })
    await user.click(await screen.findByRole('button', { name: 'Send' }))
    expect(await screen.findByText(/Check the SMTP settings/)).toBeInTheDocument()
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled()
  })

  it('shortens a very long message for the mail app, and can copy the full text', async () => {
    const user = userEvent.setup()
    const longActions = Array.from({ length: 80 }, (_, i) => ({ ...actions[0], id: `x${i}`, task: `Long task number ${i} with some words` }))
    mockServer({ 'GET /api/v1/features': () => ({ email_enabled: false }) })
    const write = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText: write }, configurable: true })
    render(
      <MemoryRouter><ToastProvider><ShareDialog meeting={meeting} summary={summary} actions={longActions} onClose={() => undefined} /></ToastProvider></MemoryRouter>,
    )
    const href = (await screen.findByRole('link', { name: 'Open in mail app' })).getAttribute('href')!
    expect(decodeURIComponent(href.split('&body=')[1])).toContain('(shortened)')
    await user.click(screen.getByRole('button', { name: 'Copy summary text' }))
    expect(write).toHaveBeenCalledOnce()
    expect(String(write.mock.calls[0][0])).toContain('Long task number 79') // the copy is complete
    expect(await screen.findByText('Summary copied.')).toBeInTheDocument()
  })
})

describe('calendar files from the Calendar and Tasks pages', () => {
  it('the Calendar page downloads every meeting and my deadlines', async () => {
    const user = userEvent.setup()
    const { calls } = mockServer({ ...emptyApp, 'GET /auth/me': meHandler, '/api/v1/export/calendar.ics': () => ({ ok: 1 }) })
    renderApp('/calendar')
    await user.click(await screen.findByRole('button', { name: /Export \.ics/ }))
    await waitFor(() => expect(downloads.map((d) => d.name)).toEqual(['meeting-intelligence.ics']))
    expect(calls.find((c) => c.url.startsWith('/api/v1/export/calendar.ics'))!.url).toBe('/api/v1/export/calendar.ics')
    expect(await screen.findByText(/Calendar file downloaded/)).toBeInTheDocument()
  })

  it('the Tasks page downloads only my deadlines', async () => {
    const user = userEvent.setup()
    const { calls } = mockServer({
      ...emptyApp,
      'GET /auth/me': meHandler,
      '/api/v1/actions': () => ({ count: 0, total: 0, action_items: [] }),
      '/api/v1/export/calendar.ics': () => ({ ok: 1 }),
    })
    renderApp('/tasks')
    await user.click(await screen.findByRole('button', { name: /My deadlines \(\.ics\)/ }))
    await waitFor(() => expect(downloads.map((d) => d.name)).toEqual(['my-task-deadlines.ics']))
    expect(calls.find((c) => c.url.startsWith('/api/v1/export/calendar.ics'))!.url).toBe('/api/v1/export/calendar.ics?meetings=false&tasks=true&mine=true')
  })
})
