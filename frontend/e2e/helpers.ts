import { expect, type Browser, type BrowserContext, type Page } from '@playwright/test'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
export const WAV = path.resolve(here, '../../tests/data/two_speakers.wav')
export const PASSWORD = 'e2e-passphrase-12345'
export const SHOTS = process.env.E2E_SHOTS ?? ''

export const uniqueEmail = (name: string) => `${name}-${Math.random().toString(36).slice(2, 8)}@e2e.test`

export async function register(page: Page, email: string, password = PASSWORD) {
  await page.goto('/register')
  await page.getByLabel('Full name').fill('Test Person')
  await page.getByLabel('E-mail').fill(email)
  await page.getByLabel('Password', { exact: true }).fill(password)
  await page.getByLabel('Confirm password').fill(password)
  await page.getByRole('button', { name: 'Create account' }).click()
  await expect(page.getByRole('heading', { level: 1 })).toContainText(/Good (morning|afternoon|evening)/)
}

export async function signIn(page: Page, email: string, password = PASSWORD) {
  await page.goto('/login')
  await page.getByLabel('E-mail').fill(email)
  await page.getByLabel('Password').fill(password)
  await page.getByRole('button', { name: 'Sign in' }).click()
}

/** A fresh browser context (separate "device") with a registered user. */
export async function newUser(browser: Browser, name: string, opts: { viewport?: { width: number; height: number } } = {}) {
  const email = uniqueEmail(name)
  const context: BrowserContext = await browser.newContext({ ...opts, timezoneId: 'UTC', locale: 'en-US' })
  const page = await context.newPage()
  await register(page, email)
  return { email, context, page }
}

export async function shot(page: Page, name: string) {
  if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: true })
}

/** YYYY-MM-DD of the next given weekday (0=Sun…6=Sat) strictly after `from` (UTC date). */
export function nextWeekday(from: Date, weekday: number): string {
  const d = new Date(Date.UTC(from.getUTCFullYear(), from.getUTCMonth(), from.getUTCDate()))
  let ahead = (weekday - d.getUTCDay() + 7) % 7
  if (ahead === 0) ahead = 7
  d.setUTCDate(d.getUTCDate() + ahead)
  return d.toISOString().slice(0, 10)
}
