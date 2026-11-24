import { expect, test, type Browser, type BrowserContext, type Page } from '@playwright/test'
import fs from 'node:fs'
import { newUser, nextWeekday, shot, uniqueEmail, WAV } from './helpers'

// One realistic story, start to finish, in a real browser against the real
// backend: upload a recording → background processing → results → edit tasks
// → tasks page → calendar → sharing with a participant → export → delete.
/** The task text box of a row, found by its current value (Playwright has no getByDisplayValue). */
const taskInput = (page: Page, text: string) => page.locator(`input[aria-label="Task"][value*="${text}" i]`)

test.describe.serial('recording to insights', () => {
  let owner: { email: string; context: BrowserContext; page: Page }
  let bob: { email: string; context: BrowserContext; page: Page }
  let carol: { email: string; context: BrowserContext; page: Page }
  let meetingUrl = ''
  let trainingDue = ''
  let reportDue = ''

  test.beforeAll(async ({ browser }: { browser: Browser }) => {
    owner = await newUser(browser, 'owner')
    bob = await newUser(browser, 'bob')
    carol = await newUser(browser, 'carol')
  })
  test.afterAll(async () => {
    await owner?.context.close()
    await bob?.context.close()
    await carol?.context.close()
  })

  test('the dashboard starts empty and inviting', async () => {
    const { page } = owner
    await expect(page.getByText('No meetings yet')).toBeVisible()
    await expect(page.getByText('You are all caught up').or(page.getByText('Nothing open'))).toBeVisible()
    await shot(page, '01-dashboard-empty')
  })

  test('upload a real recording and start processing', async () => {
    const { page } = owner
    await page.getByRole('link', { name: /Upload recording/ }).first().click()
    await expect(page.getByRole('heading', { name: 'New meeting' })).toBeVisible()

    // wrong file type is rejected client-side with a clear message
    await page.getByTestId('audio-input').setInputFiles({ name: 'notes.txt', mimeType: 'text/plain', buffer: Buffer.from('hello') })
    await expect(page.getByRole('alert')).toContainText('Unsupported file type')

    await page.getByTestId('audio-input').setInputFiles(WAV)
    await expect(page.getByLabel('Title')).toHaveValue('two speakers') // derived from the file name
    await page.getByLabel('Title').fill('E2E planning')
    await page.getByLabel('Participants').fill(bob.email)
    await page.getByLabel('Participants').press('Enter')
    await shot(page, '02-upload-form')
    await page.getByRole('button', { name: 'Upload and process' }).click()

    await expect(page).toHaveURL(/\/meetings\/[0-9a-f-]{36}$/)
    meetingUrl = new URL(page.url()).pathname
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('E2E planning')
    await expect(page.getByRole('heading', { name: 'Processing your recording…' })).toBeVisible()
    await expect(page.getByText('Transcribe & identify speakers')).toBeVisible()
    await shot(page, '03-processing')
  })

  test('background processing finishes and the summary appears', async () => {
    const { page } = owner
    await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible({ timeout: 300_000 })
    await expect(page.getByRole('heading', { name: 'Processing your recording…' })).toBeHidden()
    const overview = page.locator('.card', { has: page.getByRole('heading', { name: 'Overview' }) })
    await expect(overview.locator('p').first()).not.toBeEmpty()
    await expect(page.getByRole('heading', { name: 'Key points' })).toBeVisible()
    await shot(page, '04-summary')
  })

  test('transcript shows both speakers, searches, and seeks the audio', async () => {
    const { page } = owner
    await page.getByRole('tab', { name: 'Transcript' }).click()
    await expect(page.locator('.card-head .speaker', { hasText: 'Speaker 1' })).toBeVisible()
    await expect(page.locator('.card-head .speaker', { hasText: 'Speaker 2' })).toBeVisible()
    await expect(page.getByText(/model training/i)).toBeVisible()
    await expect(page.getByTestId('player')).toBeVisible()
    await shot(page, '05-transcript')

    // search highlights matches and hides the rest
    await page.getByLabel('Search transcript').fill('report')
    await expect(page.locator('mark')).toHaveText(/report/i)
    await expect(page.getByText(/model training/i)).toBeHidden()
    await page.getByLabel('Search transcript').fill('zzzz-not-there')
    await expect(page.getByText('No matches')).toBeVisible()
    await page.getByLabel('Search transcript').fill('')

    // clicking a line jumps the audio there
    await expect.poll(() => page.getByTestId('player').evaluate((a: HTMLAudioElement) => a.readyState)).toBeGreaterThan(0)
    await page.getByRole('button', { name: /project report/i }).click()
    await expect.poll(() => page.getByTestId('player').evaluate((a: HTMLAudioElement) => a.currentTime)).toBeGreaterThan(3.5)
  })

  test('action items carry the right owners and Python-resolved deadlines', async () => {
    const { page } = owner
    await page.getByRole('tab', { name: /Action items/ }).click()
    const rows = page.locator('tbody tr')
    await expect(rows).toHaveCount(2)

    const training = rows.filter({ has: taskInput(page, 'training') })
    const report = rows.filter({ has: taskInput(page, 'report') })
    await expect(training.getByLabel('Owner')).toHaveValue('Speaker 1')
    await expect(report.getByLabel('Owner')).toHaveValue('Speaker 2')

    // "by Friday" / "by Monday", resolved against the meeting date (today, UTC)
    const today = new Date()
    trainingDue = nextWeekday(today, 5)
    reportDue = nextWeekday(today, 1)
    await expect(training.getByLabel('Deadline')).toHaveValue(trainingDue)
    await expect(report.getByLabel('Deadline')).toHaveValue(reportDue)
    await shot(page, '06-actions')
  })

  test('edit tasks: complete, add, change owner and priority, delete — all persisted', async () => {
    const { page } = owner
    const rows = page.locator('tbody tr')
    const training = rows.filter({ has: taskInput(page, 'training') })

    await training.getByRole('checkbox').check()
    await page.reload()
    await page.getByRole('tab', { name: /Action items/ }).click()
    await expect(rows.filter({ has: taskInput(page, 'training') }).getByRole('checkbox')).toBeChecked()

    await page.getByRole('button', { name: 'Add' }).click()
    await page.locator('form').getByLabel('Task').fill('Book the demo room')
    await page.locator('form').getByLabel('Owner').fill('Bob')
    await page.locator('form').getByLabel('Deadline').fill('2031-03-04')
    await page.getByRole('button', { name: 'Add task' }).click()
    const added = rows.filter({ has: taskInput(page, 'Book the demo room') })
    await expect(added).toHaveCount(1)
    await expect(added.getByLabel('Owner')).toHaveValue('Bob')

    await added.getByLabel('Priority').selectOption('high')
    await added.getByLabel('Owner').fill('Anna')
    await added.getByLabel('Owner').blur()
    await page.reload()
    await page.getByRole('tab', { name: /Action items/ }).click()
    const again = rows.filter({ has: taskInput(page, 'Book the demo room') })
    await expect(again.getByLabel('Priority')).toHaveValue('high')
    await expect(again.getByLabel('Owner')).toHaveValue('Anna')

    // adding the same task twice is refused with a clear message
    await page.getByRole('button', { name: 'Add' }).click()
    await page.locator('form').getByLabel('Task').fill('book the DEMO room!')
    await page.getByRole('button', { name: 'Add task' }).click()
    await expect(page.getByRole('alert')).toContainText('identical action item already exists')
    await page.getByRole('button', { name: 'Cancel' }).click()

    await again.getByRole('button', { name: /^Delete/ }).click()
    await expect(rows.filter({ has: taskInput(page, 'Book the demo room') })).toHaveCount(0)
  })

  test('"My tasks" groups work by urgency and updates in place', async () => {
    const { page } = owner
    await page.getByRole('link', { name: 'My tasks' }).click()
    await expect(page.getByRole('heading', { name: 'My tasks' })).toBeVisible()
    // training is completed, so only the report task is open
    await expect(page.getByText(/finish|prepare|report/i).first()).toBeVisible()
    await expect(page.getByText(/training/i)).toBeHidden()
    await shot(page, '07-tasks')

    await page.getByRole('button', { name: 'Completed', exact: true }).click()
    await expect(page.getByText(/training/i)).toBeVisible()
    await page.getByRole('button', { name: 'Open', exact: true }).click()

    // completing updates in place (struck through under "Completed", so it can be undone)…
    await page.getByRole('checkbox', { name: /^Complete:/ }).first().click()
    await expect(page.getByRole('region', { name: 'Completed' })).toContainText(/report/i)
    // …and the Open list is empty after a refresh
    await page.reload()
    await expect(page.getByText('You are all caught up')).toBeVisible()
  })

  test('the calendar shows the deadlines on the correct days', async () => {
    const { page } = owner
    await page.getByRole('link', { name: 'Calendar' }).click()
    await expect(page.getByRole('grid')).toBeVisible()

    // jump to the month of the Friday deadline if it is not the current one
    const [y, m] = trainingDue.split('-').map(Number)
    const now = new Date()
    if (y !== now.getUTCFullYear() || m !== now.getUTCMonth() + 1) await page.getByRole('button', { name: 'Next month' }).click()
    const cell = page.getByRole('gridcell', { name: new RegExp(`${Number(trainingDue.slice(8))}`) }).filter({ hasText: /training|model/i })
    await expect(cell.first()).toBeVisible()
    await cell.first().click()
    await expect(page.getByRole('link', { name: /Complete the model training/ })).toBeVisible() // the day panel entry
    await shot(page, '08-calendar')

    // the filters work
    await page.getByLabel('Deadlines').uncheck()
    await expect(page.locator('.ev.submission, .ev.done')).toHaveCount(0)
    await page.getByLabel('Deadlines').check()

    await page.getByRole('link', { name: /Complete the model training/ }).click()
    await expect(page).toHaveURL(meetingUrl)
  })

  test('a listed participant can read everything but change nothing', async () => {
    const { page } = bob
    await page.getByRole('link', { name: 'Meetings', exact: true }).click()
    await expect(page.getByRole('link', { name: 'E2E planning' })).toBeVisible()
    await page.goto(meetingUrl)
    await expect(page.getByText(/You are a participant in this meeting/)).toBeVisible()
    await expect(page.getByRole('button', { name: 'Delete meeting' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: /Process|Re-process/ })).toHaveCount(0)
    await page.getByRole('tab', { name: /Action items/ }).click()
    await expect(page.locator('tbody input[type=checkbox]').first()).toBeDisabled()
    await page.getByRole('tab', { name: 'Transcript' }).click()
    await expect(page.getByText(/model training/i)).toBeVisible()
    await expect(page.getByTestId('player')).toBeVisible() // may listen to the recording
    await shot(page, '09-participant-view')
  })

  test('a stranger cannot see the meeting at all', async () => {
    const { page } = carol
    await page.goto(meetingUrl)
    await expect(page.getByRole('heading', { name: 'Meeting not found' })).toBeVisible()
    await page.goto('/meetings')
    await expect(page.getByText('No meetings yet')).toBeVisible()
  })

  test('export downloads a Markdown write-up', async () => {
    const { page } = owner
    await page.goto(meetingUrl)
    await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible()
    const download = page.waitForEvent('download')
    await page.getByRole('button', { name: /Export/ }).click()
    const file = await download
    expect(file.suggestedFilename()).toBe('E2E-planning.md')
    const text = fs.readFileSync((await file.path())!, 'utf8')
    expect(text).toContain('# E2E planning')
    expect(text).toContain('## Summary')
    expect(text).toContain('## Action items')
    expect(text).toContain('## Transcript')
    expect(text).toMatch(/\[0:0\d\] Speaker 1:/)
  })

  test('re-processing keeps the existing action items (no duplicates)', async () => {
    const { page } = owner
    await page.getByRole('button', { name: 'Re-process' }).click()
    await page.getByRole('button', { name: 'Start processing' }).click()
    await expect(page.getByRole('heading', { name: 'Processing your recording…' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Processing your recording…' })).toBeHidden({ timeout: 300_000 })
    await page.getByRole('tab', { name: /Action items/ }).click()
    await expect(page.locator('tbody tr')).toHaveCount(2)
  })

  test('deleting the meeting removes it for everyone', async () => {
    const { page } = owner
    await page.getByRole('button', { name: 'Delete meeting' }).click()
    await expect(page.getByRole('dialog')).toContainText('cannot be undone')
    await page.getByRole('button', { name: 'Delete forever' }).click()
    await expect(page).toHaveURL(/\/meetings$/)
    await expect(page.getByText('No meetings yet')).toBeVisible()
    await bob.page.goto(meetingUrl)
    await expect(bob.page.getByRole('heading', { name: 'Meeting not found' })).toBeVisible()
  })
})
