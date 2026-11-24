import { expect, test } from '@playwright/test'
import { newUser } from './helpers'

// Regression: wide content (filter buttons, calendar) must never stretch the
// page beyond a phone screen.
test('no page overflows horizontally on a phone', async ({ browser }) => {
  const { page, context } = await newUser(browser, 'ovf', { viewport: { width: 360, height: 780 } })
  for (const path of ['/', '/meetings', '/tasks', '/calendar', '/settings', '/meetings/new', '/meetings/new?mode=schedule']) {
    await page.goto(path)
    await expect(page.locator('main')).toBeVisible()
    await page.waitForTimeout(400)
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
    expect(overflow, `horizontal overflow on ${path}`).toBeLessThanOrEqual(1)
  }
  await context.close()
})
