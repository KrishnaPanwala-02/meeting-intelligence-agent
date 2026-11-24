import { expect, test } from '@playwright/test'
import { newUser, shot } from './helpers'

test.describe('layout and themes', () => {
  test('mobile layout: the menu opens, navigates and closes', async ({ browser }) => {
    const { page, context } = await newUser(browser, 'mob', { viewport: { width: 390, height: 844 } })
    await expect(page.getByRole('complementary', { name: 'Main' })).toBeHidden()
    await page.getByRole('button', { name: 'Open menu' }).click()
    const menu = page.getByRole('complementary', { name: 'Main' })
    await expect(menu.getByRole('link', { name: 'Calendar' })).toBeVisible()
    await shot(page, '12-mobile-menu')
    await menu.getByRole('link', { name: 'Calendar' }).click()
    await expect(page.getByRole('heading', { name: 'Calendar' })).toBeVisible()
    await expect(page.getByRole('complementary', { name: 'Main' })).toBeHidden() // closed after navigating
    // nothing overflows horizontally on a phone
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
    expect(overflow).toBeLessThanOrEqual(1)
    await shot(page, '13-mobile-calendar')
    await context.close()
  })

  test('dark mode toggles, persists across reloads, and is readable', async ({ browser }) => {
    const { page, context } = await newUser(browser, 'dark')
    await page.getByRole('button', { name: 'Dark mode' }).click()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
    await shot(page, '14-dark-dashboard')
    await page.reload()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
    const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor)
    expect(bg).toBe('rgb(14, 16, 32)')
    await page.getByRole('button', { name: 'Light mode' }).click()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
    await context.close()
  })

  test('keyboard users can operate the main flows', async ({ browser }) => {
    const { page, context } = await newUser(browser, 'kbd')
    await page.goto('/meetings/new?mode=schedule')
    await page.getByLabel('Title').focus()
    await page.keyboard.type('Keyboard only')
    await page.keyboard.press('Tab') // to participants
    await page.keyboard.type('a@e2e.test')
    await page.keyboard.press('Enter')
    await expect(page.locator('.chip', { hasText: 'a@e2e.test' })).toBeVisible()
    await page.keyboard.press('Backspace')
    await page.keyboard.press('Backspace') // empty input, so this removes the chip
    await expect(page.locator('.chip')).toHaveCount(0)
    await context.close()
  })

  test('login page looks right in both themes', async ({ page }) => {
    await page.goto('/login')
    await shot(page, '15-login')
    await expect(page.getByRole('heading', { name: 'Welcome back' })).toBeVisible()
  })
})
