import { useState, type FormEvent, type ReactNode } from 'react'
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { authApi, voicesApi } from '../api/endpoints'
import { useAuth } from '../auth/AuthContext'
import { IconMic } from '../components/icons'
import BackButton from '../components/BackButton'
import { Button, Card, TextField } from '../components/ui'
import VoiceInput, { ChosenVoice } from '../components/VoiceInput'

const MIN_PASSWORD = 10

function AuthShell({ title, subtitle, children, back }: { title: string; subtitle?: string; children: ReactNode; back?: boolean }) {
  return (
    <div className="auth-wrap">
      <Card className="auth-card">
        {back && <BackButton fallback="/login" />}
        <div className="brand">
          <span className="logo">
            <IconMic size={17} />
          </span>
          Meeting Intelligence
        </div>
        <h1 style={{ fontSize: '1.4rem' }}>{title}</h1>
        {subtitle && <p className="sub" style={{ margin: '0.3rem 0 1.2rem' }}>{subtitle}</p>}
        {children}
      </Card>
    </div>
  )
}

function FormError({ message }: { message: string | null }) {
  if (!message) return null
  return (
    <div className="alert danger" role="alert">
      {message}
    </div>
  )
}

export function LoginPage() {
  const { login, sessionEnded } = useAuth()
  const nav = useNavigate()
  const loc = useLocation()
  const from = (loc.state as { from?: string } | null)?.from ?? '/'
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      await login(email.trim(), password)
      nav(from, { replace: true })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not sign in')
    } finally {
      setBusy(false)
    }
  }

  return (
    <AuthShell title="Welcome back" subtitle="Sign in to your meetings, tasks and calendar.">
      <form className="stack" onSubmit={submit}>
        {sessionEnded && (
          <div className="alert warn" role="status">
            Your session ended. Please sign in again.
          </div>
        )}
        <FormError message={error} />
        <TextField label="E-mail" type="email" autoComplete="username" required autoFocus value={email} onChange={(e) => setEmail(e.target.value)} />
        <TextField label="Password" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
        <Button variant="primary" type="submit" busy={busy}>
          Sign in
        </Button>
        <div className="row between small">
          <Link to="/forgot-password">Forgot password?</Link>
          <span>
            New here? <Link to="/register">Create an account</Link>
          </span>
        </div>
      </form>
    </AuthShell>
  )
}

export function RegisterPage() {
  const { register } = useAuth()
  const nav = useNavigate()
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [voice, setVoice] = useState<File | null>(null)

  const tooShort = password.length > 0 && password.length < MIN_PASSWORD
  const mismatch = confirm.length > 0 && confirm !== password

  async function submit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    if (!name.trim()) return setError('Please enter your full name.')
    if (password.length < MIN_PASSWORD) return setError(`Password must be at least ${MIN_PASSWORD} characters.`)
    if (password !== confirm) return setError('The passwords do not match.')
    if (!voice) return setError('Please record your voice or import an audio file. It is required.')
    setBusy(true)
    try {
      await register(email.trim(), password, name.trim())
      try {
        const form = new FormData()
        form.append('audio_file', voice)
        await voicesApi.recordMine(form)
      } catch (err) {
        // The account exists; send them to finish the voice step where they can retry.
        nav('/settings', { replace: true, state: { voiceFailed: err instanceof Error ? err.message : 'Your voice could not be saved.' } })
        return
      }
      nav('/', { replace: true })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not create the account')
    } finally {
      setBusy(false)
    }
  }

  return (
    <AuthShell back title="Create your account" subtitle="Your meetings stay on this machine.">
      <form className="stack" onSubmit={submit}>
        <FormError message={error} />
        <TextField
          label="Full name"
          autoComplete="name"
          required
          autoFocus
          maxLength={100}
          value={name}
          onChange={(e) => setName(e.target.value)}
          help="Used to recognise your voice and show you the tasks assigned to you."
        />
        <TextField label="E-mail" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
        <TextField
          label="Password"
          type="password"
          autoComplete="new-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          help={`At least ${MIN_PASSWORD} characters. A short passphrase works well.`}
          error={tooShort ? `${MIN_PASSWORD - password.length} more characters needed` : undefined}
        />
        <TextField
          label="Confirm password"
          type="password"
          autoComplete="new-password"
          required
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          error={mismatch ? 'Does not match' : undefined}
        />
        <div className="field">
          <label>Your voice (required)</label>
          <span className="help">
            Record about 15 seconds, alone and somewhere quiet, or import a recording from this device. Try saying: “My name is {name.trim() || 'your name'}, and I am recording my voice so the meeting assistant can recognise me.”
          </span>
          {voice ? <ChosenVoice file={voice} onRemove={() => setVoice(null)} /> : <VoiceInput onUse={setVoice} />}
        </div>
        <Button variant="primary" type="submit" busy={busy}>
          Create account
        </Button>
        <p className="small">
          Already have an account? <Link to="/login">Sign in</Link>
        </p>
      </form>
    </AuthShell>
  )
}

export function ForgotPage() {
  const [email, setEmail] = useState('')
  const [sent, setSent] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      await authApi.forgotPassword(email.trim())
      setSent(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not send the request')
    } finally {
      setBusy(false)
    }
  }

  return (
    <AuthShell back title="Reset your password" subtitle="We will send reset instructions if the account exists.">
      {sent ? (
        <div className="stack">
          <div className="alert ok" role="status">
            If an account exists for that address, reset instructions have been sent.
          </div>
          <div className="alert info small">
            <div>
              <strong>Running locally without e-mail?</strong> Ask the server administrator for a reset token, or run
              this on the machine hosting the backend:
              <pre className="mono small" style={{ margin: '0.4rem 0 0', whiteSpace: 'pre-wrap' }}>
                docker compose exec api python -m src.cli reset-token {email || 'you@example.com'}
              </pre>
            </div>
          </div>
          <Link className="btn primary" to="/reset-password">
            I have a reset token
          </Link>
          <Link to="/login" className="small">
            Back to sign in
          </Link>
        </div>
      ) : (
        <form className="stack" onSubmit={submit}>
          <FormError message={error} />
          <TextField label="E-mail" type="email" required autoFocus value={email} onChange={(e) => setEmail(e.target.value)} />
          <Button variant="primary" type="submit" busy={busy}>
            Send reset instructions
          </Button>
          <Link to="/login" className="small">
            Back to sign in
          </Link>
        </form>
      )}
    </AuthShell>
  )
}

export function ResetPage() {
  const [params] = useSearchParams()
  const nav = useNavigate()
  const [token, setToken] = useState(params.get('token') ?? '')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState(false)
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    if (password.length < MIN_PASSWORD) return setError(`Password must be at least ${MIN_PASSWORD} characters.`)
    if (password !== confirm) return setError('The passwords do not match.')
    setBusy(true)
    try {
      await authApi.resetPassword(token.trim(), password)
      setDone(true)
      setTimeout(() => nav('/login', { replace: true }), 1800)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not reset the password')
    } finally {
      setBusy(false)
    }
  }

  return (
    <AuthShell back title="Choose a new password" subtitle="Signing in on all devices will be required afterwards.">
      {done ? (
        <div className="alert ok" role="status">
          Password updated. Redirecting to sign in…
        </div>
      ) : (
        <form className="stack" onSubmit={submit}>
          <FormError message={error} />
          <TextField label="Reset token" required value={token} onChange={(e) => setToken(e.target.value)} help="From the e-mail link or your administrator." />
          <TextField label="New password" type="password" autoComplete="new-password" required value={password} onChange={(e) => setPassword(e.target.value)} help={`At least ${MIN_PASSWORD} characters.`} />
          <TextField label="Confirm new password" type="password" autoComplete="new-password" required value={confirm} onChange={(e) => setConfirm(e.target.value)} />
          <Button variant="primary" type="submit" busy={busy}>
            Update password
          </Button>
          <Link to="/login" className="small">
            Back to sign in
          </Link>
        </form>
      )}
    </AuthShell>
  )
}
