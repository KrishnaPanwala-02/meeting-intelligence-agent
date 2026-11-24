import { expect, test, type BrowserContext, type Page } from '@playwright/test'
import fs from 'node:fs'
import { WAV } from './helpers'

// Records a pretend online meeting in a REAL Chromium: a second tab ("Fake Meet") plays the two-speaker
// recording, and the app shares that tab, mixes it with a fake microphone and records both.
// It needs no backend (the API calls are answered locally) and no real Google Meet / Zoom account.
//
// Chromium can only share a real tab in its "new" headless mode, and Playwright mutes audio by default,
// which would silence the tab before it is captured.
test.use({
  channel: 'chromium',
  permissions: ['microphone'],
  launchOptions: {
    ignoreDefaultArgs: ['--mute-audio'],
    args: [
      '--use-fake-device-for-media-stream', // a fake microphone (it beeps)
      '--auto-select-tab-capture-source-by-title=Fake Meet', // choose the meeting tab in the share dialog
      '--autoplay-policy=no-user-gesture-required',
    ],
  },
})

const me = { id: 'u1', email: 'k@example.com', name: 'Krishna Panwala', is_admin: false, session_id: 's1' }

async function openApp(context: BrowserContext): Promise<{ app: Page; meet: Page; uploads: { fields: string; bytes: number }[] }> {
  const wav = fs.readFileSync(WAV)
  await context.route('http://fake-meet.test/**', (route) => {
    const url = route.request().url()
    if (url.endsWith('/meeting.wav')) return route.fulfill({ body: wav, contentType: 'audio/wav' })
    return route.fulfill({
      contentType: 'text/html',
      body: '<!doctype html><title>Fake Meet</title><h1>Pretend meeting</h1><audio id="a" src="/meeting.wav" preload="auto"></audio>',
    })
  })
  await context.addInitScript(() => {
    sessionStorage.setItem('mia.refresh', 'r')
    sessionStorage.setItem('mia.access', JSON.stringify({ token: 't', exp: Date.now() + 3_600_000 }))
  })
  const meet = await context.newPage()
  await meet.goto('http://fake-meet.test/')

  const app = await context.newPage()
  const uploads: { fields: string; bytes: number }[] = []
  await app.route('**/auth/me', (r) => r.fulfill({ json: me }))
  await app.route('**/health/ready', (r) => r.fulfill({ json: { status: 'ready', checks: {} } }))
  await app.route('**/api/v1/notifications**', (r) => r.fulfill({ json: { unread: 0, items: [] } }))
  await app.route('**/api/v1/meetings/upload', async (r) => {
    const body = r.request().postDataBuffer() ?? Buffer.alloc(0)
    uploads.push({ fields: body.toString('latin1'), bytes: body.length })
    await r.fulfill({ status: 201, json: { meeting_id: 'm9' } })
  })
  await app.route('**/api/v1/meetings/m9/process', (r) => r.fulfill({ status: 202, json: { job_id: 'j', state: 'queued', steps: [], status_url: '' } }))
  await app.route('**/api/v1/meetings/m9**', (r) => r.fulfill({ status: 404, json: { detail: 'not needed' } }))
  await app.goto('/meetings/new')
  await app.bringToFront()
  await app.getByRole('button', { name: 'Online meeting' }).click()
  return { app, meet, uploads }
}

const meterOf = async (app: Page, name: string) => Number(await app.getByRole('meter', { name }).getAttribute('aria-valuenow'))

test('records the meeting tab and the microphone together, and uploads it as an online meeting', async ({ context }) => {
  const { app, meet, uploads } = await openApp(context)
  await app.getByRole('checkbox', { name: /Everyone in this meeting knows/ }).check()
  await app.getByRole('button', { name: 'Start recording' }).click()
  await expect(app.getByRole('timer')).toBeVisible()

  // The share is a real browser tab with audio: no "you shared your screen" warning.
  await expect(app.locator('.alert')).toHaveCount(0)

  // The "other participants" speak: the meeting meter must move, and no silence warning may appear.
  await meet.evaluate(() => (document.getElementById('a') as HTMLAudioElement).play())
  let heardMeeting = 0
  let heardMic = 0
  for (let i = 0; i < 40; i++) {
    heardMeeting = Math.max(heardMeeting, await meterOf(app, 'Meeting audio'))
    heardMic = Math.max(heardMic, await meterOf(app, 'Your microphone').catch(() => 0))
    if (heardMeeting > 5 && heardMic > 5) break
    await app.waitForTimeout(250)
  }
  expect(heardMeeting, 'the meeting tab’s audio reached the recorder').toBeGreaterThan(5)
  expect(heardMic, 'the microphone reached the recorder').toBeGreaterThan(5)
  await expect(app.getByText(/No meeting audio has been heard/)).toHaveCount(0)

  await app.waitForTimeout(2500)
  await app.getByRole('button', { name: 'Stop' }).click()
  await expect(app.getByTestId('recording-preview')).toBeVisible()
  await expect(app.getByText(/meeting audio and your microphone/)).toBeVisible()
  await app.getByRole('button', { name: 'Use this recording' }).click()
  await expect(app.getByText(/online meeting, recording confirmed with everyone/)).toBeVisible()

  await app.getByRole('button', { name: /Upload and process/ }).click()
  await expect.poll(() => uploads.length).toBe(1)
  expect(uploads[0].fields).toContain('name="source"\r\n\r\nonline_tab')
  expect(uploads[0].fields).toContain('name="recording_consent"\r\n\r\ntrue')
  expect(uploads[0].fields).toMatch(/filename="Online meeting .+\.webm"/)
  expect(uploads[0].bytes).toBeGreaterThan(10_000) // real audio, not an empty file
})

test('warns when the shared meeting is silent', async ({ context }) => {
  const { app } = await openApp(context) // the pretend meeting never plays anything
  await app.getByRole('checkbox', { name: /Everyone in this meeting knows/ }).check()
  await app.getByRole('button', { name: 'Start recording' }).click()
  await expect(app.getByRole('timer')).toBeVisible()
  await expect(app.getByText(/No meeting audio has been heard for a while/)).toBeVisible({ timeout: 20_000 })
  expect(await meterOf(app, 'Meeting audio')).toBe(0)
})

test('will not start without the consent tick', async ({ context }) => {
  const { app } = await openApp(context)
  await expect(app.getByRole('button', { name: 'Start recording' })).toBeDisabled()
})
