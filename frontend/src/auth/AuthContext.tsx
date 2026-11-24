import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { authApi } from '../api/endpoints'
import { onSessionLost, tokens } from '../api/client'
import type { Me } from '../api/types'

type Status = 'loading' | 'authed' | 'anon'

interface AuthValue {
  status: Status
  user: Me | null
  /** Set when the session ended on its own (revoked, expired, signed out elsewhere). */
  sessionEnded: boolean
  login: (email: string, password: string) => Promise<void>
  register: (email: string, password: string, name?: string) => Promise<void>
  /** Save the signed-in user's display name. */
  updateName: (name: string) => Promise<void>
  /** Change the name, time zone or e-mail-notification choice. */
  updateProfile: (patch: { name?: string; timezone?: string; email_notifications?: boolean }) => Promise<void>
  logout: () => Promise<void>
  /** Adopt tokens issued by the server (e.g. after a password change). */
  adoptSession: () => Promise<void>
}

const Ctx = createContext<AuthValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<Status>('loading')
  const [user, setUser] = useState<Me | null>(null)
  const [sessionEnded, setSessionEnded] = useState(false)
  const started = useRef(false)

  const loadMe = useCallback(async () => {
    const me = await authApi.me()
    setUser(me)
    setStatus('authed')
    setSessionEnded(false)
  }, [])

  // Restore a session after a page reload: reuse this tab's access token if it
  // is still valid; otherwise the first request gets a 401 and the client
  // transparently exchanges the refresh token.
  useEffect(() => {
    if (started.current) return
    started.current = true
    void (async () => {
      if (!tokens.refresh) {
        setStatus('anon')
        return
      }
      tokens.restoreAccess()
      try {
        await loadMe()
      } catch {
        setStatus('anon')
      }
    })()
  }, [loadMe])

  // The server ended the session (revoked from another device, expired…).
  useEffect(() => {
    onSessionLost(() => {
      setUser(null)
      setStatus('anon')
      setSessionEnded(true)
    })
    return () => onSessionLost(null)
  }, [])

  const login = useCallback(
    async (email: string, password: string) => {
      tokens.set(await authApi.login(email, password))
      await loadMe()
    },
    [loadMe],
  )

  const register = useCallback(
    async (email: string, password: string, name?: string) => {
      await authApi.register(email, password, name)
      await login(email, password)
    },
    [login],
  )

  const updateProfile = useCallback(async (patch: { name?: string; timezone?: string; email_notifications?: boolean }) => {
    setUser(await authApi.updateMe(patch))
  }, [])
  const updateName = useCallback((name: string) => updateProfile({ name }), [updateProfile])

  const logout = useCallback(async () => {
    const refresh = tokens.refresh
    try {
      await authApi.logout(refresh)
    } catch {
      /* signing out locally is enough if the server is unreachable */
    }
    tokens.clear()
    setUser(null)
    setStatus('anon')
    setSessionEnded(false)
  }, [])

  const value = useMemo<AuthValue>(
    () => ({ status, user, sessionEnded, login, register, updateName, updateProfile, logout, adoptSession: loadMe }),
    [status, user, sessionEnded, login, register, updateName, updateProfile, logout, loadMe],
  )
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useAuth(): AuthValue {
  const v = useContext(Ctx)
  if (!v) throw new Error('useAuth must be used inside AuthProvider')
  return v
}
