import { expect, test } from '@playwright/test'
import { newUser, shot, uniqueEmail } from './helpers'

test.describe('scheduling', () => {
  test('schedule, hit a conflict, get suggestions, book, reschedule, cancel', async ({ browser }) => {
    const { page, context, email } = await newUser(browser, 'sched')

    // ── schedule the first meeting (defaults to tomorrow 10:00)
    await page.goto('/meetings/new?mode=schedule')
    await page.getByLabel('Title').fill('Design review')
    await page.getByLabel('Participants').fill(uniqueEmail('mate'))
    await page.getByLabel('Participants').press('Enter')
    const start = await page.getByLabel('Starts').inputValue()
    await page.getByRole('button', { name: 'Schedule meeting' }).click()
    await expect(page).toHaveURL(/\/meetings\/[0-9a-f-]{36}$/)
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Design review')
    await expect(page.locator('.badge', { hasText: 'scheduled' }).first()).toBeVisible()
    const firstUrl = page.url()

    // ── the same slot clashes (the organizer cannot be in two places)
    await page.goto('/meetings/new?mode=schedule')
    await page.getByLabel('Title').fill('Clashing meeting')
    await page.getByLabel('Starts').fill(start)
    await page.getByRole('button', { name: 'Schedule meeting' }).click()
    const warning = page.getByRole('alert').filter({ hasText: 'clashes' })
    await expect(warning).toContainText('Design review')
    await shot(page, '10-conflict')

    // ── ask for suggestions and take an alternative
    await warning.getByRole('button', { name: 'Suggest another time' }).click()
    const slots = page.locator('.slot')
    await expect(slots.first()).toBeVisible()
    await expect(slots.first().locator('.badge', { hasText: 'Best' })).toBeVisible()
    const suggested = slots.nth(1)
    await suggested.click()
    expect(await page.getByLabel('Starts').inputValue()).not.toBe(start)
    await shot(page, '11-suggestions')
    await page.getByRole('button', { name: 'Schedule meeting' }).click()
    await expect(page).toHaveURL(/\/meetings\/[0-9a-f-]{36}$/)
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Clashing meeting')

    // ── "book anyway" is available as a deliberate choice
    await page.goto('/meetings/new?mode=schedule')
    await page.getByLabel('Title').fill('Double booked on purpose')
    await page.getByLabel('Starts').fill(start)
    await page.getByRole('button', { name: 'Schedule meeting' }).click()
    await page.getByRole('button', { name: 'Book anyway' }).click()
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Double booked on purpose')

    // ── the meeting is on the calendar
    await page.goto('/calendar')
    await expect(page.locator('.ev.meeting', { hasText: 'Design review' }).first()).toBeVisible()

    // ── reschedule, then cancel
    await page.goto(firstUrl)
    await page.getByRole('button', { name: 'Reschedule' }).click()
    const dialog = page.getByRole('dialog')
    await dialog.getByLabel('Duration (minutes)').fill('30')
    await dialog.getByRole('button', { name: 'Save', exact: true }).click()
    // it still overlaps the meeting that was double-booked on purpose: a friendly warning, no API jargon
    await expect(dialog.getByRole('alert')).toContainText('clashes with another meeting')
    await expect(dialog.getByRole('alert')).toContainText('Double booked on purpose')
    await expect(dialog).not.toContainText('allow_conflicts')
    await dialog.getByRole('button', { name: 'Save anyway' }).click()
    await expect(page.getByText('· 30 min')).toBeVisible()

    await page.getByRole('button', { name: 'Cancel meeting' }).click()
    await page.getByRole('dialog').getByRole('button', { name: 'Cancel meeting' }).click()
    await expect(page.locator('.badge', { hasText: 'cancelled' }).first()).toBeVisible()
    await expect(page.getByRole('button', { name: 'Reschedule' })).toHaveCount(0)

    // ── a cancelled meeting frees its slot
    await page.goto('/meetings/new?mode=schedule')
    await page.getByLabel('Title').fill('Reuse the slot')
    await page.getByLabel('Starts').fill(start)
    await page.getByRole('button', { name: 'Schedule meeting' }).click()
    // still clashes with "Double booked on purpose", but NOT with the cancelled one
    const w2 = page.getByRole('alert').filter({ hasText: 'clashes' })
    await expect(w2).toContainText('Double booked on purpose')
    await expect(w2).not.toContainText('Design review')
    expect(email).toBeTruthy()
    await context.close()
  })

  test('the meetings list filters and searches', async ({ browser }) => {
    const { page, context } = await newUser(browser, 'list')
    for (const [title, days] of [['Alpha kickoff', 3], ['Beta retro', 4]] as const) {
      await page.goto('/meetings/new?mode=schedule')
      await page.getByLabel('Title').fill(title)
      const d = new Date(Date.now() + days * 86_400_000)
      await page.getByLabel('Starts').fill(`${d.toISOString().slice(0, 10)}T10:00`)
      await page.getByRole('button', { name: 'Schedule meeting' }).click()
      await expect(page.getByRole('heading', { level: 1 })).toHaveText(title)
    }
    await page.goto('/meetings')
    await expect(page.getByRole('link', { name: 'Alpha kickoff' })).toBeVisible()
    await expect(page.getByRole('link', { name: 'Beta retro' })).toBeVisible()
    await page.getByLabel('Search meetings').fill('beta')
    await expect(page.getByRole('link', { name: 'Alpha kickoff' })).toBeHidden()
    await page.getByLabel('Search meetings').fill('')
    await page.getByLabel('Filter by status').selectOption('cancelled')
    await expect(page.getByText('No matching meetings')).toBeVisible()
    await page.getByLabel('Filter by status').selectOption('scheduled')
    await expect(page.getByRole('link', { name: 'Alpha kickoff' })).toBeVisible()
    await page.getByRole('row', { name: /Beta retro/ }).click() // whole row is clickable
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Beta retro')
    await context.close()
  })
})
