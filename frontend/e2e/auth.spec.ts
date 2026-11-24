import { expect, test } from '@playwright/test'
import { PASSWORD, register, signIn, uniqueEmail } from './helpers'

test.describe('accounts and sessions', () => {
  test('protected pages redirect to sign-in and return afterwards', async ({ page }) => {
    await page.goto('/tasks')
    await expect(page).toHaveURL(/\/login/)
    await expect(page.getByRole('heading', { name: 'Welcome back' })).toBeVisible()
  })

  test('register, stay signed in across a reload, sign out, sign back in', async ({ page }) => {
    const email = uniqueEmail('auth')
    await register(page, email)
    await expect(page.getByText(email).first()).toBeVisible()

    await page.reload() // session restored from the refresh token
    await expect(page.getByRole('heading', { level: 1 })).toContainText(/Good/)

    await page.getByRole('button', { name: 'Sign out' }).click()
    await expect(page.getByRole('heading', { name: 'Welcome back' })).toBeVisible()
    await page.goto('/meetings')
    await expect(page).toHaveURL(/\/login/) // really signed out

    // signing in returns you to the page you were trying to reach
    await signIn(page, email)
    await expect(page.getByRole('heading', { level: 1, name: 'Meetings' })).toBeVisible()
    await expect(page).toHaveURL('/meetings')
    await page.getByRole('link', { name: 'Dashboard' }).click()
    await expect(page.getByRole('heading', { level: 1 })).toContainText(/Good/)
  })

  test('wrong password and duplicate registration show clear errors', async ({ page }) => {
    const email = uniqueEmail('dup')
    await register(page, email)
    await page.getByRole('button', { name: 'Sign out' }).click()

    await signIn(page, email, 'definitely-the-wrong-one')
    await expect(page.getByRole('alert')).toHaveText('Invalid e-mail or password')

    await page.goto('/register')
    await page.getByLabel('Full name').fill('Test Person')
    await page.getByLabel('E-mail').fill(email)
    await page.getByLabel('Password', { exact: true }).fill(PASSWORD)
    await page.getByLabel('Confirm password').fill(PASSWORD)
    await page.getByRole('button', { name: 'Create account' }).click()
    await expect(page.getByRole('alert')).toHaveText('E-mail already registered')
  })

  test('registration form validates locally', async ({ page }) => {
    await page.goto('/register')
    await page.getByLabel('Full name').fill('Test Person')
    await page.getByLabel('E-mail').fill(uniqueEmail('v'))
    await page.getByLabel('Password', { exact: true }).fill('short')
    await expect(page.getByText('more characters needed')).toBeVisible()
    await page.getByLabel('Confirm password').fill('different')
    await page.getByRole('button', { name: 'Create account' }).click()
    await expect(page.getByRole('alert')).toContainText('at least 10 characters')
  })

  test('forgot-password never reveals whether an account exists', async ({ page }) => {
    await page.goto('/forgot-password')
    await page.getByLabel('E-mail').fill(uniqueEmail('nobody')) // the server limits resets per address
    await page.getByRole('button', { name: /Send reset instructions/ }).click()
    await expect(page.getByText('If an account exists for that address')).toBeVisible()
    await expect(page.getByText(/src\.cli reset-token/)).toBeVisible()
  })

  test('unknown routes show a friendly page', async ({ page }) => {
    await register(page, uniqueEmail('nf'))
    await page.goto('/definitely/not/here')
    await expect(page.getByRole('heading', { name: 'Page not found' })).toBeVisible()
  })
})
