import { expect, test } from '@playwright/test'
import { newUser, PASSWORD, signIn } from './helpers'

test.describe('security settings', () => {
  test('changing the password signs out every other device', async ({ browser }) => {
    const a = await newUser(browser, 'pw')
    // a second device signs in with the same account
    const ctxB = await browser.newContext({ timezoneId: 'UTC' })
    const b = await ctxB.newPage()
    await signIn(b, a.email)
    await expect(b.getByRole('heading', { level: 1 })).toContainText(/Good/)

    await a.page.goto('/settings')
    await expect(a.page.getByText('This device')).toBeVisible()
    await expect(a.page.locator('.list li')).toHaveCount(2) // two devices listed

    const newPassword = 'a brand new passphrase 99'
    await a.page.getByLabel('Current password').fill('wrong password here')
    await a.page.getByLabel('New password', { exact: true }).fill(newPassword)
    await a.page.getByLabel('Confirm new password').fill(newPassword)
    await a.page.getByRole('button', { name: 'Change password' }).click()
    await expect(a.page.getByRole('alert')).toContainText('Current password is incorrect')

    await a.page.getByLabel('Current password').fill(PASSWORD)
    await a.page.getByRole('button', { name: 'Change password' }).click()
    await expect(a.page.getByText('All other devices were signed out')).toBeVisible()
    await a.page.reload() // this device stays signed in
    await expect(a.page.getByRole('heading', { name: 'Settings' })).toBeVisible()

    // the other device lost its session
    await b.reload()
    await expect(b.getByRole('heading', { name: 'Welcome back' })).toBeVisible()
    // the old password no longer works, the new one does
    await signIn(b, a.email)
    await expect(b.getByRole('alert')).toHaveText('Invalid e-mail or password')
    await signIn(b, a.email, newPassword)
    await expect(b.getByRole('heading', { level: 1 })).toContainText(/Good/)
    await ctxB.close()
    await a.context.close()
  })

  test('a single device can be signed out remotely', async ({ browser }) => {
    const a = await newUser(browser, 'rev')
    const ctxB = await browser.newContext({ timezoneId: 'UTC' })
    const b = await ctxB.newPage()
    await signIn(b, a.email)
    await expect(b.getByRole('heading', { level: 1 })).toContainText(/Good/)

    await a.page.goto('/settings')
    await a.page.locator('.list li', { hasNotText: 'This device' }).getByRole('button', { name: 'Sign out' }).click()
    await expect(a.page.getByText('Device signed out.')).toBeVisible()
    await expect(a.page.locator('.list li')).toHaveCount(1)

    await b.goto('/meetings')
    await expect(b.getByRole('heading', { name: 'Welcome back' })).toBeVisible()
    await ctxB.close()
    await a.context.close()
  })

  test('"sign out everywhere" ends every session including this one', async ({ browser }) => {
    const a = await newUser(browser, 'all')
    await a.page.goto('/settings')
    await a.page.getByRole('button', { name: 'Sign out everywhere' }).first().click()
    await a.page.getByRole('dialog').getByRole('button', { name: 'Sign out everywhere' }).click()
    await expect(a.page.getByRole('heading', { name: 'Welcome back' })).toBeVisible()
    await a.page.goto('/')
    await expect(a.page).toHaveURL(/\/login/)
    await a.context.close()
  })

  test('system status reflects the running services', async ({ browser }) => {
    const a = await newUser(browser, 'sys')
    await a.page.goto('/settings')
    const card = a.page.locator('.card', { hasText: 'System status' })
    await expect(card.getByText('Database (PostgreSQL)')).toBeVisible()
    await expect(card.locator('.badge', { hasText: 'Running' }).first()).toBeVisible()
    await a.context.close()
  })
})
