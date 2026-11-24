import { useEffect, useState, type FormEvent } from 'react'
import { useLocation } from 'react-router-dom'
import type { VoiceProfile } from '../api/types'
import { authApi, exportApi, systemApi, voicesApi } from '../api/endpoints'
import { tokens } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { Badge, Button, Card, CardHead, Check, ConfirmDialog, ErrorBanner, Loading, TextField, useToast } from '../components/ui'
import VoiceInput from '../components/VoiceInput'
import { allZones, browserZone } from '../lib/zone'
import { fmtDateTime, fromServerUtc } from '../lib/format'
import { useAsync } from '../lib/useAsync'

const MIN_PASSWORD = 10

function userAgentLabel(ua: string | null): string {
  if (!ua) return 'Unknown device'
  const browser = /Edg\//.test(ua) ? 'Edge' : /Chrome\//.test(ua) ? 'Chrome' : /Firefox\//.test(ua) ? 'Firefox' : /Safari\//.test(ua) ? 'Safari' : null
  const os = /Mac OS X/.test(ua) ? 'macOS' : /Windows/.test(ua) ? 'Windows' : /Android/.test(ua) ? 'Android' : /iPhone|iPad/.test(ua) ? 'iOS' : /Linux/.test(ua) ? 'Linux' : null
  if (browser || os) return [browser, os].filter(Boolean).join(' on ')
  return ua.slice(0, 40)
}

function VoiceRow({ profile, onForget, onSaved }: { profile: VoiceProfile; onForget: () => void; onSaved: () => void }) {
  const toast = useToast()
  const [email, setEmail] = useState(profile.email ?? '')
  const [busy, setBusy] = useState(false)
  const changed = email.trim().toLowerCase() !== (profile.email ?? '').toLowerCase()

  async function save(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    try {
      await voicesApi.setEmail(profile.id, email.trim() || null)
      toast.ok(email.trim() ? `${profile.name}’s meetings will be shared with ${email.trim()}.` : 'E-mail removed.')
      onSaved()
    } catch (err) {
      toast.error(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="row wrap" onSubmit={save} style={{ gap: '0.6rem', padding: '0.35rem 0' }}>
      <span style={{ minWidth: 170 }}>
        <strong>{profile.name}</strong> <span className="muted small">· {profile.samples} sample{profile.samples === 1 ? '' : 's'}</span>
      </span>
      <input
        className="input sm"
        type="email"
        aria-label={`E-mail for ${profile.name}`}
        placeholder="Their sign-in e-mail"
        title="New meetings where this voice is recognised are shared with this address"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        style={{ width: 230 }}
      />
      <Button size="sm" variant="primary" type="submit" busy={busy} disabled={!changed}>
        Save
      </Button>
      <Button size="sm" type="button" onClick={onForget} aria-label={`Forget ${profile.name}`}>
        Forget
      </Button>
    </form>
  )
}

export default function SettingsPage() {
  const { user, logout, adoptSession, updateName, updateProfile } = useAuth()
  const toast = useToast()
  const location = useLocation()
  useEffect(() => {
    const failed = (location.state as { voiceFailed?: string } | null)?.voiceFailed
    if (failed) toast.error(new Error(`Your account is ready, but your voice was not saved: ${failed} Record or import it under “My voice”.`))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const [nameDraft, setNameDraft] = useState(user?.name ?? '')
  const [nameBusy, setNameBusy] = useState(false)
  const [nameError, setNameError] = useState<string | null>(null)

  async function saveName(e: FormEvent) {
    e.preventDefault()
    setNameError(null)
    if (!nameDraft.trim()) return setNameError('Please enter your full name.')
    setNameBusy(true)
    try {
      await updateName(nameDraft.trim())
      toast.ok('Name saved.')
    } catch (err) {
      setNameError(err instanceof Error ? err.message : 'Could not save the name')
    } finally {
      setNameBusy(false)
    }
  }

  const [uploadingVoice, setUploadingVoice] = useState(false)
  const [recorderKey, setRecorderKey] = useState(0)
  const features = useAsync(() => exportApi.features().catch(() => ({ email_enabled: false })), [])
  const zones = allZones(user?.timezone)
  const [prefBusy, setPrefBusy] = useState(false)

  async function savePref(patch: { timezone?: string; email_notifications?: boolean }, message: string) {
    setPrefBusy(true)
    try {
      await updateProfile(patch)
      toast.ok(message)
    } catch (err) {
      toast.error(err)
    } finally {
      setPrefBusy(false)
    }
  }

  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [pwError, setPwError] = useState<string | null>(null)
  const [pwBusy, setPwBusy] = useState(false)

  const voiceProfiles = useAsync(() => voicesApi.list(), [])

  const myVoice = voiceProfiles.data?.profiles.find((p) => (p.email ?? '').toLowerCase() === (user?.email ?? '').toLowerCase())

  async function saveMyVoice(file: File) {
    setUploadingVoice(true)
    try {
      const form = new FormData()
      form.append('audio_file', file)
      const r = await voicesApi.recordMine(form)
      toast.ok(`Saved your voice (${Math.round(r.speech_seconds)} seconds of speech). You will now be named automatically.`)
      voiceProfiles.reload()
    } catch (e) {
      toast.error(e)
    } finally {
      setUploadingVoice(false)
      setRecorderKey((k) => k + 1) // a fresh recorder, ready for another try
    }
  }
  const [forgetId, setForgetId] = useState<string | null>(null)
  const sessions = useAsync(() => authApi.sessions().then((r) => r.sessions), [])
  const health = useAsync(() => systemApi.ready(), [])
  const [confirmAll, setConfirmAll] = useState(false)
  const [busy, setBusy] = useState(false)

  async function changePassword(e: FormEvent) {
    e.preventDefault()
    setPwError(null)
    if (next.length < MIN_PASSWORD) return setPwError(`New password must be at least ${MIN_PASSWORD} characters.`)
    if (next !== confirm) return setPwError('The new passwords do not match.')
    setPwBusy(true)
    try {
      // Every session is ended by the server; the reply carries a fresh one for this browser.
      tokens.set(await authApi.changePassword(current, next))
      await adoptSession()
      setCurrent('')
      setNext('')
      setConfirm('')
      sessions.reload()
      toast.ok('Password changed. All other devices were signed out.')
    } catch (err) {
      setPwError(err instanceof Error ? err.message : 'Could not change the password')
    } finally {
      setPwBusy(false)
    }
  }

  async function revoke(id: string) {
    try {
      await authApi.revokeSession(id)
      sessions.reload()
      toast.ok('Device signed out.')
    } catch (e) {
      toast.error(e)
    }
  }

  async function forgetVoice(id: string) {
    try {
      await voicesApi.remove(id)
      voiceProfiles.reload()
      toast.ok('Voice forgotten. Names already in meetings are unchanged.')
    } catch (e) {
      toast.error(e)
    } finally {
      setForgetId(null)
    }
  }

  async function signOutEverywhere() {
    setBusy(true)
    try {
      await authApi.logoutAll()
    } catch (e) {
      toast.error(e)
    }
    tokens.clear()
    await logout()
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Settings</h1>
          <p>Your account, devices and system status.</p>
        </div>
      </div>

      <div className="grid two" style={{ alignItems: 'start' }}>
        <div className="stack" style={{ gap: '1.1rem' }}>
          <Card>
            <CardHead title="Account" />
            <form className="card-body stack" onSubmit={saveName} style={{ paddingBottom: 0 }}>
              <ErrorBanner error={nameError} />
              <TextField
                label="Full name"
                autoComplete="name"
                required
                maxLength={100}
                value={nameDraft}
                onChange={(e) => setNameDraft(e.target.value)}
                help="Used to recognise your voice in recordings and to show you the tasks assigned to you."
              />
              <div>
                <Button variant="primary" type="submit" busy={nameBusy} disabled={!nameDraft.trim() || nameDraft.trim() === (user?.name ?? '')}>
                  Save name
                </Button>
              </div>
            </form>
            <div className="card-body stack tight">
              <div className="row between">
                <span className="muted">E-mail</span>
                <strong>{user?.email}</strong>
              </div>
              <div className="row between">
                <span className="muted">Role</span>
                {user?.is_admin ? <Badge tone="accent">Administrator</Badge> : <Badge>Member</Badge>}
              </div>
              {user?.is_admin && <p className="muted small">Administrators can see every meeting, including legacy meetings that have no owner.</p>}
            </div>
          </Card>

          <Card>
            <CardHead title="Notifications and time zone" />
            <div className="card-body stack">
              <div className="field">
                <label htmlFor="tz">Your time zone</label>
                <select id="tz" className="input" value={user?.timezone ?? ''} disabled={prefBusy} onChange={(e) => void savePref({ timezone: e.target.value }, e.target.value ? 'Time zone saved.' : 'Time zone cleared.')}>
                  <option value="">Not set (use the computer’s clock)</option>
                  {zones.map((z) => (
                    <option key={z} value={z}>
                      {z.replace(/_/g, ' ')}
                    </option>
                  ))}
                </select>
                <span className="help">New meetings you schedule use this zone, and reminders follow it.</span>
                {user?.timezone !== browserZone() && (
                  <div>
                    <Button size="sm" type="button" disabled={prefBusy} onClick={() => void savePref({ timezone: browserZone() }, 'Time zone saved.')}>
                      Use this browser’s zone ({browserZone().replace(/_/g, ' ')})
                    </Button>
                  </div>
                )}
              </div>
              <Check
                checked={!!user?.email_notifications}
                disabled={prefBusy || features.data?.email_enabled === false}
                onChange={(on) => void savePref({ email_notifications: on }, on ? 'You will get e-mail copies of notifications.' : 'E-mail copies are off.')}
              >
                E-mail me a copy of notifications
              </Check>
              {features.data?.email_enabled === false && (
                <p className="muted small" style={{ margin: 0 }}>
                  This computer is not set up to send e-mail, so notifications appear in the bell only. (An administrator can set EMAIL_ENABLED and the SMTP settings in .env.)
                </p>
              )}
            </div>
          </Card>

          <Card>
            <CardHead title="My voice" />
            <div className="card-body stack">
              {!user?.name ? (
                <p className="muted small" style={{ margin: 0 }}>Add your full name first (above), so your voice can be named.</p>
              ) : (
                <>
                  <p className="muted small" style={{ margin: 0 }}>
                    {myVoice ? (
                      <>
                        <Badge tone="ok">Saved</Badge> Your voice is saved ({myVoice.samples} sample{myVoice.samples === 1 ? '' : 's'}), so you are named automatically in meetings. Record again to make it more reliable.
                      </>
                    ) : (
                      'Record yourself for about 15 seconds, alone and somewhere quiet, or import a recording from this device. Then you are named automatically from your very first meeting.'
                    )}
                  </p>
                  <p className="small" style={{ margin: 0 }}>
                    Try saying: <q>My name is {user.name}, and I am recording my voice so the meeting assistant can recognise me.</q>
                  </p>
                  {uploadingVoice ? <Loading label="Analysing your voice…" /> : <VoiceInput key={recorderKey} onUse={(f) => void saveMyVoice(f)} />}
                </>
              )}
            </div>
          </Card>

          <Card>
            <CardHead title="Saved voices" />
            <div className="card-body stack tight">
              <p className="muted small" style={{ margin: 0 }}>
                People whose voices you named in a meeting. They are labelled automatically in new recordings. Add their e-mail and new meetings with their voice are shared with them, so they can sign in and see their tasks.
              </p>
              {voiceProfiles.error && <ErrorBanner error={voiceProfiles.error} onRetry={voiceProfiles.reload} />}
              {voiceProfiles.data && voiceProfiles.data.profiles.length === 0 && (
                <p className="muted small">None yet. Open a meeting’s Transcript tab and name a speaker.</p>
              )}
              {voiceProfiles.data?.profiles.map((p) => (
                <VoiceRow key={p.id} profile={p} onForget={() => setForgetId(p.id)} onSaved={voiceProfiles.reload} />
              ))}
            </div>
          </Card>

          <Card>
            <CardHead title="Change password" />
            <form className="card-body stack" onSubmit={changePassword}>
              <ErrorBanner error={pwError} />
              <TextField label="Current password" type="password" autoComplete="current-password" required value={current} onChange={(e) => setCurrent(e.target.value)} />
              <TextField label="New password" type="password" autoComplete="new-password" required value={next} onChange={(e) => setNext(e.target.value)} help={`At least ${MIN_PASSWORD} characters. You will stay signed in here; other devices are signed out.`} />
              <TextField label="Confirm new password" type="password" autoComplete="new-password" required value={confirm} onChange={(e) => setConfirm(e.target.value)} />
              <div>
                <Button variant="primary" type="submit" busy={pwBusy}>Change password</Button>
              </div>
            </form>
          </Card>
        </div>

        <div className="stack" style={{ gap: '1.1rem' }}>
          <Card>
            <CardHead title="Signed-in devices">
              <Button size="sm" variant="danger" onClick={() => setConfirmAll(true)}>Sign out everywhere</Button>
            </CardHead>
            {sessions.loading && !sessions.data ? (
              <Loading />
            ) : sessions.error ? (
              <div className="card-body"><ErrorBanner error={sessions.error} onRetry={sessions.reload} /></div>
            ) : (
              <ul className="list">
                {(sessions.data ?? []).map((s) => (
                  <li key={s.id} className="row between">
                    <div className="grow">
                      <div style={{ fontWeight: 600 }}>
                        {userAgentLabel(s.user_agent)} {s.current && <Badge tone="ok">This device</Badge>}
                      </div>
                      <div className="small muted">
                        {s.ip_address ?? 'unknown address'} · signed in {fmtDateTime(fromServerUtc(s.created_at))}
                      </div>
                    </div>
                    {!s.current && <Button size="sm" onClick={() => void revoke(s.id)}>Sign out</Button>}
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card>
            <CardHead title="System status">
              <Button size="sm" onClick={health.reload} busy={health.loading}>Check again</Button>
            </CardHead>
            <div className="card-body stack tight">
              {health.data === null ? (
                <div className="alert danger">Cannot reach the backend.</div>
              ) : health.data ? (
                <>
                  {(
                    [
                      ['database', 'Database (PostgreSQL)'],
                      ['redis', 'Job queue (Redis)'],
                      ['ollama', 'AI engine (Ollama)'],
                    ] as const
                  ).map(([key, label]) => (
                    <div key={key} className="row between">
                      <span>{label}</span>
                      <Badge tone={health.data!.checks[key] === 'ok' ? 'ok' : 'danger'}>
                        {health.data!.checks[key] === 'ok' ? 'Running' : 'Down'}
                      </Badge>
                    </div>
                  ))}
                  {health.data.checks.ollama === 'fail' && (
                    <p className="muted small">Start Ollama on this Mac and pull the model with <span className="mono">ollama pull llama3.2</span>.</p>
                  )}
                </>
              ) : (
                <Loading />
              )}
            </div>
          </Card>
        </div>
      </div>

      {forgetId && (
        <ConfirmDialog
          title="Forget this voice?"
          message="New recordings will no longer label this person automatically. Names already written into meetings stay as they are."
          confirmLabel="Forget voice"
          danger
          onConfirm={() => void forgetVoice(forgetId)}
          onCancel={() => setForgetId(null)}
        />
      )}

      {confirmAll && (
        <ConfirmDialog
          title="Sign out everywhere?"
          message="Every device, including this one, will be signed out and will need to sign in again."
          confirmLabel="Sign out everywhere"
          danger
          busy={busy}
          onConfirm={() => void signOutEverywhere()}
          onCancel={() => setConfirmAll(false)}
        />
      )}
    </>
  )
}
