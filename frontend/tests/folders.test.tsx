import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { onSessionLost, tokens } from '../src/api/client'
import type { FolderChoice } from '../src/api/types'
import FolderPicker, { folderChoiceError, folderFields } from '../src/components/FolderPicker'
import { parseFollowUp } from '../src/components/FollowUpCard'
import { ToastProvider } from '../src/components/ui'
import { emptyApp, jsonResponse, meHandler, mockServer, pair, renderApp } from './helpers'

beforeEach(() => {
  tokens.clear()
  onSessionLost(null)
  tokens.set(pair(1) as Parameters<typeof tokens.set>[0])
  Element.prototype.scrollIntoView = vi.fn()
})
afterEach(() => vi.unstubAllGlobals())

const FOLDERS = {
  count: 2,
  folders: [
    { id: 'f1', name: 'Zomato project', meeting_count: 3, last_meeting: '2026-11-18T10:00:00', created_at: null },
    { id: 'f2', name: 'Swiggy', meeting_count: 1, last_meeting: '2026-11-10T10:00:00', created_at: null },
  ],
}
const FOLLOW = 'Follows up: The data problem was fixed (Meeting 2).\nNew: Charts will be added next week.\nStill open: Anna: book the review room'

const DETAIL = {
  ...FOLDERS.folders[0],
  meetings: [
    { number: 1, id: 'm1', title: 'Kickoff', status: 'uploaded', when: '2026-11-10T10:00:00', has_summary: true, summary: 'We chose the dashboard.', follow_up: null, open_tasks: 2, done_tasks: 0 },
    { number: 2, id: 'm2', title: 'Review', status: 'uploaded', when: '2026-11-14T10:00:00', has_summary: true, summary: 'Charts are late.', follow_up: FOLLOW, open_tasks: 1, done_tasks: 1 },
    { number: 3, id: 'm3', title: 'Next review', status: 'scheduled', when: '2026-12-01T10:00:00', has_summary: false, summary: null, follow_up: null, open_tasks: 0, done_tasks: 0 },
  ],
}

const base = () => ({ ...emptyApp, 'GET /auth/me': meHandler, 'GET /api/v1/folders': () => FOLDERS })

// ------------------------------------------------------------------ helpers

describe('folder helpers', () => {
  it('sends nothing, an existing folder, or a new one by name', () => {
    expect(folderFields({})).toEqual({})
    expect(folderFields({ folder_id: null })).toEqual({})
    expect(folderFields({ folder_id: 'f1' })).toEqual({ folder_id: 'f1' })
    expect(folderFields({ new_folder: '  Zomato   ' })).toEqual({ new_folder: 'Zomato' })
    expect(folderFields({ new_folder: '   ', folder_id: 'f1' })).toEqual({ folder_id: 'f1' })
  })

  it('stops a "new folder" that was never named', () => {
    expect(folderChoiceError({ new_folder: '' })).toMatch(/Name the new folder/)
    expect(folderChoiceError({ new_folder: '  ' })).toMatch(/Name the new folder/)
    expect(folderChoiceError({ new_folder: 'X' })).toBeNull()
    expect(folderChoiceError({ folder_id: 'f1' })).toBeNull()
    expect(folderChoiceError({})).toBeNull()
  })

  it('splits the follow-up text into its labelled rows', () => {
    expect(parseFollowUp(FOLLOW)).toEqual([
      { label: 'Follows up', body: 'The data problem was fixed (Meeting 2).' },
      { label: 'New', body: 'Charts will be added next week.' },
      { label: 'Still open', body: 'Anna: book the review room' },
    ])
    expect(parseFollowUp('Just a sentence.\n\n')).toEqual([{ label: '', body: 'Just a sentence.' }])
  })
})

// ------------------------------------------------------------- the picker

describe('the folder picker', () => {
  function Harness({ start = {} as FolderChoice }) {
    const [v, setV] = useState<FolderChoice>(start)
    return (
      <MemoryRouter>
        <ToastProvider>
          <FolderPicker value={v} onChange={setV} />
          <output data-testid="choice">{JSON.stringify(v)}</output>
        </ToastProvider>
      </MemoryRouter>
    )
  }

  it('offers no folder, each folder with its size, and a new one', async () => {
    mockServer(base())
    render(<Harness />)
    const select = await screen.findByLabelText('Folder')
    await waitFor(() => expect(within(select).getAllByRole('option')).toHaveLength(4))
    expect(within(select).getByRole('option', { name: 'No folder' })).toBeInTheDocument()
    expect(within(select).getByRole('option', { name: 'Zomato project (3)' })).toBeInTheDocument()
    expect(within(select).getByRole('option', { name: '+ New folder…' })).toBeInTheDocument()
  })

  it('choosing a folder selects it, and "No folder" clears it', async () => {
    const user = userEvent.setup()
    mockServer(base())
    render(<Harness />)
    const select = await screen.findByLabelText('Folder')
    await waitFor(() => expect(within(select).getAllByRole('option').length).toBeGreaterThan(2))
    await user.selectOptions(select, 'Swiggy (1)')
    expect(screen.getByTestId('choice')).toHaveTextContent('{"folder_id":"f2"}')
    await user.selectOptions(select, 'No folder')
    expect(screen.getByTestId('choice')).toHaveTextContent('{"folder_id":null}')
  })

  it('"New folder…" shows a box to name it', async () => {
    const user = userEvent.setup()
    mockServer(base())
    render(<Harness />)
    await user.selectOptions(await screen.findByLabelText('Folder'), '+ New folder…')
    await user.type(screen.getByLabelText('New folder name'), 'Fresh project')
    expect(screen.getByTestId('choice')).toHaveTextContent('{"new_folder":"Fresh project"}')
  })

  it('shows the folder it starts with, and still works if the folders cannot be loaded', async () => {
    mockServer({ ...base(), 'GET /api/v1/folders': () => jsonResponse({ detail: 'x' }, 500) })
    render(<Harness start={{ folder_id: 'f1' }} />)
    expect(await screen.findByLabelText('Folder')).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'No folder' })).toBeInTheDocument()
  })
})

// ------------------------------------------------------------ Folders page

describe('the Folders page', () => {
  it('is in the sidebar and lists the folders with their sizes', async () => {
    mockServer(base())
    renderApp('/folders')
    expect(await screen.findByRole('heading', { name: 'Folders' })).toBeInTheDocument()
    expect(screen.getAllByRole('link', { name: 'Folders' }).length).toBeGreaterThan(0)       // the nav entry
    const card = (await screen.findByRole('link', { name: /Zomato project/ })).closest('.card') as HTMLElement
    expect(within(card).getByText(/3 meetings/)).toBeInTheDocument()
    expect(within(card).getByRole('link', { name: /Ask/ })).toHaveAttribute('href', '/ask?folder=f1')
    expect(screen.getByRole('link', { name: /Swiggy/ })).toBeInTheDocument()
  })

  it('explains itself when there are none', async () => {
    mockServer({ ...base(), 'GET /api/v1/folders': () => ({ count: 0, folders: [] }) })
    renderApp('/folders')
    expect(await screen.findByText('No folders yet')).toBeInTheDocument()
  })

  it('creates a folder and opens it', async () => {
    const user = userEvent.setup()
    const { calls } = mockServer({
      ...base(),
      'POST /api/v1/folders': () => ({ id: 'f9', name: 'New one', meeting_count: 0, last_meeting: null, created_at: null }),
      'GET /api/v1/folders/f9': () => ({ id: 'f9', name: 'New one', meeting_count: 0, last_meeting: null, created_at: null, meetings: [] }),
    })
    renderApp('/folders')
    const create = await screen.findByRole('button', { name: 'Create folder' })
    expect(create).toBeDisabled()                                            // needs a name
    await user.type(screen.getByLabelText('New folder name'), 'New one')
    await user.click(create)
    expect(await screen.findByRole('heading', { name: 'New one' })).toBeInTheDocument()
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ name: 'New one' })
    expect(await screen.findByText('No meetings in this folder yet')).toBeInTheDocument()
  })

  it('shows why a name was refused, such as one that already exists', async () => {
    const user = userEvent.setup()
    mockServer({ ...base(), 'POST /api/v1/folders': () => jsonResponse({ detail: 'You already have a folder with that name' }, 409) })
    renderApp('/folders')
    await user.type(await screen.findByLabelText('New folder name'), 'Swiggy')
    await user.click(screen.getByRole('button', { name: 'Create folder' }))
    expect(await screen.findByText('You already have a folder with that name')).toBeInTheDocument()
  })
})

// ------------------------------------------------------------- one folder

describe('a folder page', () => {
  const server = (extra: Record<string, unknown> = {}) =>
    mockServer({ ...base(), 'GET /api/v1/folders/f1': () => DETAIL, ...extra } as never)

  it('shows the meetings in order, each with its summary and how it continues the earlier ones', async () => {
    server()
    renderApp('/folders/f1')
    expect(await screen.findByRole('heading', { name: 'Zomato project' })).toBeInTheDocument()
    const heads = (await screen.findAllByText(/^Meeting \d ·/)).map((e) => e.textContent)
    expect(heads).toEqual(['Meeting 1 · ', 'Meeting 2 · ', 'Meeting 3 · '])
    expect(screen.getByText('We chose the dashboard.')).toBeInTheDocument()
    expect(screen.getByText('Not held yet.')).toBeInTheDocument()                    // the scheduled one
    // only meetings that remember something show the section, in labelled rows
    expect(screen.getAllByText('Continues from earlier meetings')).toHaveLength(1)
    expect(screen.getByText('Follows up')).toBeInTheDocument()
    expect(screen.getByText('Anna: book the review room')).toBeInTheDocument()
    expect(screen.getByText('2 open')).toBeInTheDocument()
    expect(screen.getByText('1 done')).toBeInTheDocument()
  })

  it('links to upload a meeting here and to ask the folder', async () => {
    server()
    renderApp('/folders/f1')
    expect(await screen.findByRole('link', { name: /Upload a meeting here/ })).toHaveAttribute('href', '/meetings/new?folder=f1')
    expect(screen.getByRole('link', { name: /Ask this folder/ })).toHaveAttribute('href', '/ask?folder=f1')
  })

  it('an empty folder invites the first meeting and does not offer to ask', async () => {
    server({ 'GET /api/v1/folders/f1': () => ({ ...DETAIL, meeting_count: 0, meetings: [] }) })
    renderApp('/folders/f1')
    expect(await screen.findByText('No meetings in this folder yet')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Ask this folder/ })).not.toBeInTheDocument()
  })

  it('renames the folder', async () => {
    const user = userEvent.setup()
    const { calls } = server({ 'PATCH /api/v1/folders/f1': () => ({ ...DETAIL, name: 'Zomato phase 2' }) })
    renderApp('/folders/f1')
    await user.click(await screen.findByRole('button', { name: 'Rename' }))
    const box = await screen.findByLabelText('Folder name')
    expect(box).toHaveValue('Zomato project')
    await user.clear(box)
    await user.type(box, 'Zomato phase 2')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'PATCH')).toBe(true))
    expect(calls.find((c) => c.method === 'PATCH')!.body).toEqual({ name: 'Zomato phase 2' })
  })

  it('deleting says the meetings are kept, and returns to the list', async () => {
    const user = userEvent.setup()
    const { calls } = server({ 'DELETE /api/v1/folders/f1': () => ({ deleted: true, meetings_unfiled: 3 }) })
    renderApp('/folders/f1')
    await user.click(await screen.findByRole('button', { name: 'Delete folder' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/summaries and tasks are kept/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Delete folder' }))
    expect(await screen.findByRole('heading', { name: 'Folders' })).toBeInTheDocument()
    expect(calls.some((c) => c.method === 'DELETE')).toBe(true)
  })

  it('says so when the folder does not exist', async () => {
    server({ 'GET /api/v1/folders/f1': () => jsonResponse({ detail: 'Folder not found' }, 404) })
    renderApp('/folders/f1')
    expect(await screen.findByText('Folder not found')).toBeInTheDocument()
  })
})

// ------------------------------------------------ uploading into a folder

describe('putting a new meeting in a folder', () => {
  const routes = () =>
    mockServer({
      ...base(),
      'POST /api/v1/meetings/upload': () => ({ meeting_id: 'm9' }),
      'POST /api/v1/meetings/m9/process': () => ({ job_id: 'j', state: 'queued', steps: [], status_url: '' }),
    })
  const wav = () => new File([new Uint8Array(2000)], 'standup.wav', { type: 'audio/wav' })

  async function fill(user: ReturnType<typeof userEvent.setup>) {
    await user.upload(await screen.findByTestId('audio-input'), wav())
  }

  it('arriving from a folder preselects it, and the upload sends it', async () => {
    const user = userEvent.setup()
    const { calls } = routes()
    renderApp('/meetings/new?folder=f1')
    await fill(user)
    const select = screen.getByLabelText('Folder') as HTMLSelectElement
    expect(select.value).toBe('f1')
    await user.click(screen.getByRole('button', { name: /Upload and process/ }))
    await waitFor(() => expect(calls.some((c) => c.url === '/api/v1/meetings/upload')).toBe(true))
    const sent = calls.find((c) => c.url === '/api/v1/meetings/upload')!.body as FormData
    expect(sent.get('folder_id')).toBe('f1')
    expect(sent.get('new_folder')).toBeNull()
  })

  it('a new folder can be made while uploading', async () => {
    const user = userEvent.setup()
    const { calls } = routes()
    renderApp('/meetings/new')
    await fill(user)
    await user.selectOptions(await screen.findByLabelText('Folder'), '+ New folder…')
    await user.type(screen.getByLabelText('New folder name'), 'Brand new')
    await user.click(screen.getByRole('button', { name: /Upload and process/ }))
    await waitFor(() => expect(calls.some((c) => c.url === '/api/v1/meetings/upload')).toBe(true))
    const sent = calls.find((c) => c.url === '/api/v1/meetings/upload')!.body as FormData
    expect(sent.get('new_folder')).toBe('Brand new')
    expect(sent.get('folder_id')).toBeNull()
  })

  it('with no folder, nothing about folders is sent', async () => {
    const user = userEvent.setup()
    const { calls } = routes()
    renderApp('/meetings/new')
    await fill(user)
    await user.click(screen.getByRole('button', { name: /Upload and process/ }))
    await waitFor(() => expect(calls.some((c) => c.url === '/api/v1/meetings/upload')).toBe(true))
    const sent = calls.find((c) => c.url === '/api/v1/meetings/upload')!.body as FormData
    expect(sent.get('folder_id')).toBeNull()
    expect(sent.get('new_folder')).toBeNull()
  })

  it('a new folder left unnamed stops the upload with a clear message', async () => {
    const user = userEvent.setup()
    const { calls } = routes()
    renderApp('/meetings/new')
    await fill(user)
    await user.selectOptions(await screen.findByLabelText('Folder'), '+ New folder…')
    await user.click(screen.getByRole('button', { name: /Upload and process/ }))
    expect(await screen.findByText(/Name the new folder/)).toBeInTheDocument()
    expect(calls.some((c) => c.url === '/api/v1/meetings/upload')).toBe(false)
  })

  it('a scheduled meeting can go in a folder too', async () => {
    const user = userEvent.setup()
    const { calls } = mockServer({
      ...base(),
      'POST /api/v1/meetings/schedule': () => ({ id: 'm5', title: 'Standup' }),
      'GET /api/v1/meetings/m5': () => jsonResponse({ detail: 'x' }, 404),
    })
    renderApp('/meetings/new?mode=schedule&folder=f2')
    await user.type(await screen.findByLabelText('Title'), 'Standup')
    expect((screen.getByLabelText('Folder') as HTMLSelectElement).value).toBe('f2')
    await user.click(screen.getByRole('button', { name: /^Schedule/ }))
    await waitFor(() => expect(calls.some((c) => c.url === '/api/v1/meetings/schedule')).toBe(true))
    expect(calls.find((c) => c.url === '/api/v1/meetings/schedule')!.body).toMatchObject({ title: 'Standup', folder_id: 'f2' })
  })
})

// ------------------------------------------------------- on a meeting page

describe('a meeting that is in a folder', () => {
  const meeting = (over: Record<string, unknown> = {}) => ({
    id: 'm2', title: 'Review', status: 'uploaded', scheduled_time: null, end_time: null, duration_minutes: 1,
    participants: [], owner_email: 'anna@example.com', started_at: null, ended_at: null, has_audio: true,
    created_at: '2026-11-14T10:00:00', can_write: true, is_owner: true, has_transcript: true, has_summary: true,
    action_count: 0, open_action_count: 0, folder: { id: 'f1', name: 'Zomato project', number: 2, meeting_count: 3 }, ...over,
  })
  const server = (m = meeting(), extra: Record<string, unknown> = {}) =>
    mockServer({
      ...base(),
      'GET /api/v1/meetings/m2': () => m,
      '/api/v1/meetings/m2/jobs/latest': () => jsonResponse({ detail: 'none' }, 404),
      '/api/v1/meetings/m2/transcript': () => ({ meeting_id: 'm2', transcript_id: 't', language: 'en', transcript: '', word_count: 1, speaker_count: 1, created_at: null, segments: [] }),
      '/api/v1/meetings/m2/summary': () => ({ meeting_id: 'm2', summary_id: 's', meeting_title: 'Review', executive_summary: 'Charts are late.', key_points: [], decisions: [], next_steps: [], key_quotes: [], follow_up: FOLLOW, created_at: null }),
      '/api/v1/meetings/m2/actions': () => ({ meeting_id: 'm2', count: 0, action_items: [] }),
      ...extra,
    } as never)

  it('shows which meeting of the folder it is, linking to the folder', async () => {
    server()
    renderApp('/meetings/m2')
    const badge = await screen.findByRole('link', { name: /Meeting 2 · Zomato project/ })
    expect(badge).toHaveAttribute('href', '/folders/f1')
  })

  it('shows how it continues the earlier meetings, as labelled rows with a caution', async () => {
    server()
    renderApp('/meetings/m2')
    expect(await screen.findByText('Continues from earlier meetings in Zomato project')).toBeInTheDocument()
    expect(screen.getByText('Follows up')).toBeInTheDocument()
    expect(screen.getByText('The data problem was fixed (Meeting 2).')).toBeInTheDocument()
    expect(screen.getByText('Still open')).toBeInTheDocument()
    expect(screen.getByText(/It can be wrong, so check it/)).toBeInTheDocument()
  })

  it('has no such card for a meeting that remembered nothing', async () => {
    server(meeting(), {
      '/api/v1/meetings/m2/summary': () => ({ meeting_id: 'm2', summary_id: 's', meeting_title: 'Review', executive_summary: 'Charts are late.', key_points: [], decisions: [], next_steps: [], key_quotes: [], follow_up: null, created_at: null }),
    })
    renderApp('/meetings/m2')
    await screen.findByText('Charts are late.')
    expect(screen.queryByText(/Continues from earlier meetings/)).not.toBeInTheDocument()
  })

  it('moves to another folder, or out of folders', async () => {
    const user = userEvent.setup()
    const { calls } = server(meeting(), { 'PUT /api/v1/meetings/m2/folder': () => meeting({ folder: { id: 'f2', name: 'Swiggy' } }) })
    renderApp('/meetings/m2')
    await user.click(await screen.findByRole('button', { name: /Change folder/ }))
    const dialog = await screen.findByRole('dialog')
    const select = within(dialog).getByLabelText('Folder') as HTMLSelectElement
    await waitFor(() => expect(within(select).getAllByRole('option').length).toBeGreaterThan(2))
    expect(select.value).toBe('f1')                                           // starts on where it is
    await user.selectOptions(select, 'Swiggy (1)')
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT' && c.url.endsWith('/folder'))).toBe(true))
    expect(calls.find((c) => c.url.endsWith('/folder'))!.body).toEqual({ folder_id: 'f2' })
  })

  it('takes it out of its folder when "No folder" is chosen', async () => {
    const user = userEvent.setup()
    const { calls } = server(meeting(), { 'PUT /api/v1/meetings/m2/folder': () => meeting({ folder: null }) })
    renderApp('/meetings/m2')
    await user.click(await screen.findByRole('button', { name: /Change folder/ }))
    const dialog = await screen.findByRole('dialog')
    await user.selectOptions(within(dialog).getByLabelText('Folder'), 'No folder')
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.some((c) => c.url.endsWith('/folder'))).toBe(true))
    expect(calls.find((c) => c.url.endsWith('/folder'))!.body).toEqual({ folder_id: null })
  })

  it('a meeting with no folder offers "Add to folder"; a shared one offers nothing', async () => {
    server(meeting({ folder: null }))
    const { unmount } = renderApp('/meetings/m2')
    expect(await screen.findByRole('button', { name: /Add to folder/ })).toBeInTheDocument()
    unmount()
    server(meeting({ folder: null, is_owner: false, can_write: false, owner_email: 'rahul@example.com' }))
    renderApp('/meetings/m2')
    await screen.findByText(/Review/)
    expect(screen.queryByRole('button', { name: /folder/i })).not.toBeInTheDocument()
  })
})

// ------------------------------------------------- the meetings list, Ask

describe('the meetings list', () => {
  it('can be limited to one folder', async () => {
    const user = userEvent.setup()
    const { calls } = mockServer(base())
    renderApp('/meetings')
    const filter = await screen.findByLabelText('Filter by folder')
    await waitFor(() => expect(within(filter).getAllByRole('option').length).toBe(3))
    await user.selectOptions(filter, 'Swiggy')
    await waitFor(() => expect(calls.some((c) => c.url.includes('folder_id=f2'))).toBe(true))
  })

  it('has no folder filter when there are no folders', async () => {
    mockServer({ ...base(), 'GET /api/v1/folders': () => ({ count: 0, folders: [] }) })
    renderApp('/meetings')
    await screen.findByLabelText('Filter by status')
    expect(screen.queryByLabelText('Filter by folder')).not.toBeInTheDocument()
  })
})

describe('asking a whole folder', () => {
  const ask = (extra: Record<string, unknown> = {}) =>
    mockServer({
      ...base(),
      'GET /api/v1/ask/history': () => ({ turns: [] }),
      'POST /api/v1/ask': () => ({ answer: 'It was approved [M1].', sources: [], searched: 3, id: 'a1', created_at: '2026-11-18T10:00:00' }),
      ...extra,
    } as never)

  it('opens on the folder, with questions for a whole project, and its own saved conversation', async () => {
    const { calls } = ask()
    renderApp('/ask?folder=f1')
    expect(await screen.findByText('Ask about the folder “Zomato project”')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'What is still open?' })).toBeInTheDocument()
    expect((screen.getByLabelText('Search in') as HTMLSelectElement).value).toBe('folder:f1')
    await waitFor(() => expect(calls.some((c) => c.url.startsWith('/api/v1/ask/history') && c.url.includes('folder_id=f1'))).toBe(true))
  })

  it('sends the folder with the question', async () => {
    const user = userEvent.setup()
    const { calls } = ask()
    renderApp('/ask?folder=f1')
    await user.click(await screen.findByRole('button', { name: 'What is still open?' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url === '/api/v1/ask')).toBe(true))
    expect(calls.find((c) => c.method === 'POST' && c.url === '/api/v1/ask')!.body).toMatchObject({ question: 'What is still open?', folder_id: 'f1' })
    expect(calls.find((c) => c.method === 'POST' && c.url === '/api/v1/ask')!.body.meeting_id).toBeUndefined()
  })

  it('folders can be picked from the search-in list', async () => {
    const user = userEvent.setup()
    const { calls } = ask()
    renderApp('/ask')
    const scope = await screen.findByLabelText('Search in')
    await waitFor(() => expect(within(scope).getByRole('option', { name: 'Swiggy' })).toBeInTheDocument())
    await user.selectOptions(scope, 'Swiggy')
    expect(await screen.findByText('Ask about the folder “Swiggy”')).toBeInTheDocument()
    await waitFor(() => expect(calls.some((c) => c.url.includes('folder_id=f2'))).toBe(true))
  })
})
