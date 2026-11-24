import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, buildQuery, describeDetail, onSessionLost, refreshSession, request, tokens } from '../src/api/client'

const pair = (n: number) => ({
  access_token: `access-${n}`,
  refresh_token: `refresh-${n}`,
  token_type: 'bearer' as const,
  expires_in: 900,
  refresh_expires_in: 1_209_600,
})

const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(status === 204 ? null : JSON.stringify(body), { status, headers })

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  vi.useFakeTimers()
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
  tokens.clear()
  onSessionLost(null)
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('describeDetail', () => {
  it('passes plain strings through', () => expect(describeDetail('Meeting not found', 404)).toBe('Meeting not found'))
  it('formats FastAPI validation errors', () => {
    const d = [{ loc: ['body', 'title'], msg: 'Field required', type: 'missing' }]
    expect(describeDetail(d, 422)).toBe('title: Field required')
  })
  it('reads the message of structured conflicts', () =>
    expect(describeDetail({ message: 'Conflicts with existing meetings', conflicts: [] }, 409)).toBe('Conflicts with existing meetings'))
  it('falls back to a sentence per status', () => {
    expect(describeDetail(undefined, 401)).toMatch(/sign in again/i)
    expect(describeDetail(undefined, 403)).toMatch(/permission/i)
    expect(describeDetail(undefined, 429)).toMatch(/too many/i)
    expect(describeDetail(undefined, 500)).toMatch(/server/i)
  })
})

describe('buildQuery', () => {
  it('skips empty values and encodes the rest', () => {
    expect(buildQuery({ a: 1, b: '', c: undefined, d: null, e: 'x y', f: false })).toBe('?a=1&e=x+y&f=false')
    expect(buildQuery({})).toBe('')
    expect(buildQuery()).toBe('')
  })
})

describe('request', () => {
  it('sends the bearer token and parses JSON', async () => {
    tokens.set(pair(1))
    fetchMock.mockResolvedValueOnce(json({ ok: true }))
    expect(await request('/api/v1/x')).toEqual({ ok: true })
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe('Bearer access-1')
  })

  it('sends JSON bodies and FormData correctly', async () => {
    fetchMock.mockImplementation(async () => json({}))
    await request('/a', { method: 'POST', json: { x: 1 }, auth: false })
    expect(fetchMock.mock.calls[0][1].headers['Content-Type']).toBe('application/json')
    expect(fetchMock.mock.calls[0][1].body).toBe('{"x":1}')
    const form = new FormData()
    await request('/b', { method: 'POST', form, auth: false })
    expect(fetchMock.mock.calls[1][1].headers['Content-Type']).toBeUndefined() // browser sets the boundary
    expect(fetchMock.mock.calls[1][1].body).toBe(form)
  })

  it('returns undefined for 204 and a Blob when asked', async () => {
    fetchMock.mockResolvedValueOnce(json(null, 204))
    expect(await request('/x', { method: 'DELETE' })).toBeUndefined()
    fetchMock.mockResolvedValueOnce(new Response('audio-bytes'))
    const blob = await request<Blob>('/audio', { as: 'blob' })
    expect(blob.size).toBe(11)
    expect(await blob.text()).toBe('audio-bytes')
  })

  it('turns errors into ApiError with a readable message and Retry-After', async () => {
    fetchMock.mockResolvedValueOnce(json({ detail: 'Too many requests.' }, 429, { 'Retry-After': '900' }))
    const err = await request('/x').catch((e) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect(err).toMatchObject({ status: 429, message: 'Too many requests.', retryAfter: 900 })
  })

  it('reports an unreachable backend plainly', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await expect(request('/x')).rejects.toMatchObject({ status: 0, message: expect.stringMatching(/backend running/i) })
  })

  it('does not send credentials for auth: false', async () => {
    tokens.set(pair(1))
    fetchMock.mockResolvedValueOnce(json({}))
    await request('/auth/login', { method: 'POST', json: {}, auth: false })
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBeUndefined()
  })
})

describe('access-token persistence across reloads', () => {
  it('stores the access token per tab and restores it while valid', () => {
    tokens.set(pair(1))
    expect(sessionStorage.getItem('mia.access')).toContain('access-1')
    // simulate a reload: memory is lost, storage remains
    tokens.clear()
    tokens.set(pair(1))
    ;(tokens as unknown as { clear: () => void }).clear()
    sessionStorage.setItem('mia.access', JSON.stringify({ token: 'kept', exp: Date.now() + 300_000 }))
    expect(tokens.restoreAccess()).toBe(true)
    expect(tokens.access).toBe('kept')
  })
  it('refuses expired or nearly expired tokens and garbage', () => {
    sessionStorage.setItem('mia.access', JSON.stringify({ token: 't', exp: Date.now() - 1000 }))
    expect(tokens.restoreAccess()).toBe(false)
    sessionStorage.setItem('mia.access', JSON.stringify({ token: 't', exp: Date.now() + 10_000 }))
    expect(tokens.restoreAccess()).toBe(false)
    sessionStorage.setItem('mia.access', 'not json')
    expect(tokens.restoreAccess()).toBe(false)
    sessionStorage.removeItem('mia.access')
    expect(tokens.restoreAccess()).toBe(false)
  })
  it('clear() wipes both stores', () => {
    tokens.set(pair(1))
    tokens.clear()
    expect(sessionStorage.getItem('mia.access')).toBeNull()
    expect(localStorage.getItem('mia.refresh')).toBeNull()
  })
})

describe('transparent token refresh', () => {
  it('refreshes once on 401, stores the rotated pair and retries the request', async () => {
    tokens.set(pair(1))
    fetchMock
      .mockResolvedValueOnce(json({ detail: 'Not authenticated' }, 401))
      .mockResolvedValueOnce(json(pair(2)))
      .mockResolvedValueOnce(json({ id: 'm1' }))
    expect(await request('/api/v1/meetings')).toEqual({ id: 'm1' })
    expect(fetchMock.mock.calls[1][0]).toBe('/auth/refresh')
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ refresh_token: 'refresh-1' })
    expect(fetchMock.mock.calls[2][1].headers.Authorization).toBe('Bearer access-2')
    expect(tokens.refresh).toBe('refresh-2')
  })

  it('shares ONE refresh between concurrent 401s (the server treats token reuse as theft)', async () => {
    tokens.set(pair(1))
    let refreshCalls = 0
    fetchMock.mockImplementation(async (url: string, init: RequestInit) => {
      if (url === '/auth/refresh') {
        refreshCalls++
        await Promise.resolve()
        return json(pair(2))
      }
      const bearer = (init.headers as Record<string, string>).Authorization
      return bearer === 'Bearer access-2' ? json({ url }) : json({ detail: 'x' }, 401)
    })
    const results = await Promise.all([request('/a'), request('/b'), request('/c')])
    expect(results.map((r) => (r as { url: string }).url)).toEqual(['/a', '/b', '/c'])
    expect(refreshCalls).toBe(1)
  })

  it('does not loop: a second 401 after refreshing is surfaced', async () => {
    tokens.set(pair(1))
    fetchMock
      .mockResolvedValueOnce(json({ detail: 'nope' }, 401))
      .mockResolvedValueOnce(json(pair(2)))
      .mockResolvedValueOnce(json({ detail: 'still nope' }, 401))
    await expect(request('/x')).rejects.toMatchObject({ status: 401 })
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })

  it('ends the session when the refresh token is rejected', async () => {
    tokens.set(pair(1))
    const lost = vi.fn()
    onSessionLost(lost)
    fetchMock.mockResolvedValueOnce(json({ detail: 'x' }, 401)).mockResolvedValueOnce(json({ detail: 'Invalid refresh token' }, 401))
    await expect(request('/x')).rejects.toMatchObject({ status: 401 })
    expect(lost).toHaveBeenCalledOnce()
    expect(tokens.refresh).toBeNull()
    expect(tokens.access).toBeNull()
  })

  it('keeps the refresh token when the backend is merely unreachable', async () => {
    tokens.set(pair(1))
    const lost = vi.fn()
    onSessionLost(lost)
    fetchMock.mockRejectedValueOnce(new TypeError('offline'))
    expect(await refreshSession()).toBe(false)
    expect(lost).not.toHaveBeenCalled()
    expect(tokens.refresh).toBe('refresh-1')
  })

  it('has nothing to refresh without a stored token', async () => {
    expect(await refreshSession()).toBe(false)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('does not try to refresh when the failing request was unauthenticated', async () => {
    tokens.set(pair(1))
    fetchMock.mockResolvedValueOnce(json({ detail: 'Invalid e-mail or password' }, 401))
    await expect(request('/auth/login', { method: 'POST', json: {}, auth: false })).rejects.toMatchObject({
      message: 'Invalid e-mail or password',
    })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('schedules a proactive refresh a minute before expiry', async () => {
    fetchMock.mockResolvedValueOnce(json(pair(2)))
    tokens.set(pair(1)) // expires_in 900s => refresh at 840s
    await vi.advanceTimersByTimeAsync(839_000)
    expect(fetchMock).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(2_000)
    expect(fetchMock).toHaveBeenCalledWith('/auth/refresh', expect.anything())
  })
})

describe('one session per tab', () => {
  const handoff = () => JSON.parse(localStorage.getItem('mia.handoff') ?? 'null')

  it('keeps the login in this tab only, never in the storage all tabs share', () => {
    tokens.set(pair(1))
    expect(sessionStorage.getItem('mia.refresh')).toBe('refresh-1')
    expect(localStorage.getItem('mia.refresh')).toBeNull()
    expect(tokens.refresh).toBe('refresh-1')
  })

  it('adopts a login left by the old version once, and removes it from shared storage', () => {
    localStorage.setItem('mia.refresh', 'legacy-token')
    expect(tokens.refresh).toBe('legacy-token')
    expect(sessionStorage.getItem('mia.refresh')).toBe('legacy-token')
    expect(localStorage.getItem('mia.refresh')).toBeNull()
  })

  it('does not let another tab’s sign-in replace this tab’s login', () => {
    tokens.set(pair(1)) // this tab: Krishna
    // another tab signs in as someone else: it writes only to ITS OWN sessionStorage and,
    // because it held no earlier token, leaves no hand-off.
    expect(handoff()).toBeNull()
    expect(tokens.refresh).toBe('refresh-1')
  })

  it('rotating the token leaves a hand-off for tabs that share the session', () => {
    tokens.set(pair(1))
    tokens.set(pair(2))
    expect(handoff().from).toBe('refresh-1')
    expect(handoff().pair.refresh_token).toBe('refresh-2')
    expect(tokens.refresh).toBe('refresh-2')
  })

  it('a silent update (following another tab) does not write a hand-off', () => {
    tokens.set(pair(1))
    localStorage.removeItem('mia.handoff')
    tokens.set(pair(2), { silent: true })
    expect(handoff()).toBeNull()
  })

  it('follows a rotation done by a tab with the same session instead of re-using the old token', async () => {
    tokens.set(pair(1))
    localStorage.setItem('mia.handoff', JSON.stringify({ from: 'refresh-1', pair: pair(2), at: Date.now() }))
    expect(await refreshSession()).toBe(true)
    expect(fetchMock).not.toHaveBeenCalled() // no second use of refresh-1
    expect(tokens.refresh).toBe('refresh-2')
    expect(tokens.access).toBe('access-2')
  })

  it('ignores a hand-off meant for a different session', async () => {
    tokens.set(pair(1))
    localStorage.setItem('mia.handoff', JSON.stringify({ from: 'someone-elses', pair: pair(9), at: Date.now() }))
    fetchMock.mockResolvedValueOnce(json(pair(2)))
    expect(await refreshSession()).toBe(true)
    expect(fetchMock).toHaveBeenCalledOnce()
    expect(tokens.refresh).toBe('refresh-2')
  })

  it('ignores a stale hand-off', async () => {
    tokens.set(pair(1))
    localStorage.setItem('mia.handoff', JSON.stringify({ from: 'refresh-1', pair: pair(2), at: Date.now() - 10 * 60_000 }))
    fetchMock.mockResolvedValueOnce(json(pair(3)))
    await refreshSession()
    expect(fetchMock).toHaveBeenCalledOnce()
    expect(tokens.refresh).toBe('refresh-3')
  })

  it('follows a hand-off announced by a storage event', () => {
    tokens.set(pair(1))
    const value = JSON.stringify({ from: 'refresh-1', pair: pair(2), at: Date.now() })
    localStorage.setItem('mia.handoff', value)
    window.dispatchEvent(new StorageEvent('storage', { key: 'mia.handoff', newValue: value }))
    expect(tokens.refresh).toBe('refresh-2')
  })

  it('signing out in one tab signs out the tabs that share the session, and only them', () => {
    const lost = vi.fn()
    onSessionLost(lost)
    tokens.set(pair(1))
    // an unrelated session signing out does nothing here
    const other = JSON.stringify({ from: 'other-session', loggedOut: true, at: Date.now() })
    localStorage.setItem('mia.handoff', other)
    window.dispatchEvent(new StorageEvent('storage', { key: 'mia.handoff', newValue: other }))
    expect(tokens.refresh).toBe('refresh-1')
    expect(lost).not.toHaveBeenCalled()
    // the same session signing out does
    const mine = JSON.stringify({ from: 'refresh-1', loggedOut: true, at: Date.now() })
    localStorage.setItem('mia.handoff', mine)
    window.dispatchEvent(new StorageEvent('storage', { key: 'mia.handoff', newValue: mine }))
    expect(tokens.refresh).toBeNull()
    expect(lost).toHaveBeenCalledOnce()
  })

  it('clear() announces the sign-out; a silent clear does not', () => {
    tokens.set(pair(1))
    tokens.clear()
    expect(handoff()).toMatchObject({ from: 'refresh-1', loggedOut: true })
    localStorage.removeItem('mia.handoff')
    tokens.set(pair(2))
    tokens.clear({ silent: true })
    expect(handoff()).toBeNull()
  })

  it('removes the hand-off after a while so no token lingers in shared storage', () => {
    tokens.set(pair(1))
    tokens.set(pair(2))
    expect(handoff()).not.toBeNull()
    vi.advanceTimersByTime(121_000)
    expect(localStorage.getItem('mia.handoff')).toBeNull()
  })
})

describe('a rejected session sends the user to sign in', () => {
  it('on 401 with no stored login at all', async () => {
    const lost = vi.fn()
    onSessionLost(lost)
    fetchMock.mockResolvedValueOnce(json({ detail: 'Not authenticated' }, 401))
    await expect(request('/api/v1/meetings/upload', { method: 'POST' })).rejects.toBeInstanceOf(ApiError)
    expect(lost).toHaveBeenCalledOnce()
    expect(fetchMock).toHaveBeenCalledOnce() // nothing to refresh with
  })

  it('on 401 that persists even after a successful refresh', async () => {
    const lost = vi.fn()
    onSessionLost(lost)
    tokens.set(pair(1))
    fetchMock
      .mockResolvedValueOnce(json({ detail: 'Not authenticated' }, 401))
      .mockResolvedValueOnce(json(pair(2)))
      .mockResolvedValueOnce(json({ detail: 'Not authenticated' }, 401))
    await expect(request('/api/v1/x')).rejects.toBeInstanceOf(ApiError)
    expect(lost).toHaveBeenCalledOnce()
    expect(tokens.refresh).toBeNull()
  })

  it('does not sign out for a login attempt with the wrong password', async () => {
    const lost = vi.fn()
    onSessionLost(lost)
    fetchMock.mockResolvedValueOnce(json({ detail: 'Invalid e-mail or password' }, 401))
    await expect(request('/auth/login', { method: 'POST', json: {}, auth: false })).rejects.toBeInstanceOf(ApiError)
    expect(lost).not.toHaveBeenCalled()
  })

  it('does not sign out because the network is down during a refresh', async () => {
    const lost = vi.fn()
    onSessionLost(lost)
    tokens.set(pair(1))
    fetchMock.mockResolvedValueOnce(json({ detail: 'Not authenticated' }, 401)).mockRejectedValueOnce(new TypeError('offline'))
    await expect(request('/api/v1/x')).rejects.toBeInstanceOf(ApiError)
    expect(lost).not.toHaveBeenCalled()
    expect(tokens.refresh).toBe('refresh-1') // still there for a retry
  })
})

describe('refreshing one tab at a time', () => {
  it('serialises refreshes through the Web Locks API when the browser has it', async () => {
    const order: string[] = []
    let chain = Promise.resolve()
    const request_ = vi.fn((_name: string, _opts: unknown, cb: () => Promise<boolean>) => {
      const run = chain.then(() => {
        order.push('start')
        return cb().then((r) => {
          order.push('end')
          return r
        })
      })
      chain = run.then(() => undefined)
      return run
    })
    vi.stubGlobal('navigator', { ...navigator, locks: { request: request_ } })
    tokens.set(pair(1))
    fetchMock.mockResolvedValueOnce(json(pair(2)))
    expect(await refreshSession()).toBe(true)
    expect(request_).toHaveBeenCalledWith('mia-refresh', { signal: expect.any(AbortSignal) }, expect.any(Function))
    expect(order).toEqual(['start', 'end'])
  })

  it('does not wait for ever for a lock that another (frozen) tab never lets go of', async () => {
    const wait = new AbortController()
    vi.spyOn(AbortSignal, 'timeout').mockImplementation((ms) => (ms < 10_000 ? wait.signal : new AbortController().signal))
    // a lock that is never granted, and gives up when its wait runs out
    const request_ = vi.fn((_n: string, opts: { signal: AbortSignal }) =>
      new Promise<boolean>((_resolve, reject) => {
        opts.signal.addEventListener('abort', () => reject(new DOMException('waited too long', 'TimeoutError')))
      }),
    )
    vi.stubGlobal('navigator', { ...navigator, locks: { request: request_ } })
    tokens.set(pair(1))
    fetchMock.mockResolvedValueOnce(json(pair(2)))
    const result = refreshSession()
    await Promise.resolve()
    expect(fetchMock).not.toHaveBeenCalled()                           // still waiting for the lock
    wait.abort(new DOMException('waited too long', 'TimeoutError'))
    expect(await result).toBe(true)                                     // then it goes ahead without it
    expect(tokens.refresh).toBe('refresh-2')
  })

  it('an unexpected lock error is not hidden', async () => {
    vi.stubGlobal('navigator', { ...navigator, locks: { request: vi.fn(() => Promise.reject(new TypeError('locks broke'))) } })
    tokens.set(pair(1))
    await expect(refreshSession()).rejects.toThrow('locks broke')
  })

  it('the refresh request has a time limit, and running out of time keeps the session for a retry', async () => {
    tokens.set(pair(1))
    fetchMock.mockRejectedValueOnce(new DOMException('timed out', 'TimeoutError'))
    expect(await refreshSession()).toBe(false)
    expect(fetchMock.mock.calls[0][1].signal).toBeInstanceOf(AbortSignal)
    expect(tokens.refresh).toBe('refresh-1')                            // not signed out: the backend may just be slow
  })

  it('shares one request between concurrent callers in the same tab', async () => {
    tokens.set(pair(1))
    fetchMock.mockResolvedValueOnce(json(pair(2)))
    const [a, b] = await Promise.all([refreshSession(), refreshSession()])
    expect(a && b).toBe(true)
    expect(fetchMock).toHaveBeenCalledOnce()
  })
})
