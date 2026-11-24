import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
} from 'react'
import { Link, type LinkProps } from 'react-router-dom'
import { ApiError } from '../api/client'
import { IconAlert } from './icons'

/* ───────── Buttons ───────── */

type Variant = 'default' | 'primary' | 'danger' | 'ghost'

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant
  size?: 'md' | 'sm'
  busy?: boolean
}

export function Button({ variant = 'default', size = 'md', busy, children, disabled, className, ...rest }: ButtonProps) {
  const cls = ['btn', variant !== 'default' && variant, size === 'sm' && 'sm', className].filter(Boolean).join(' ')
  return (
    <button {...rest} className={cls} disabled={disabled || busy} aria-busy={busy || undefined}>
      {busy && <span className="spinner" aria-hidden="true" />}
      {children}
    </button>
  )
}

export function LinkButton({
  variant = 'default',
  size = 'md',
  className,
  ...rest
}: LinkProps & { variant?: Variant; size?: 'md' | 'sm' }) {
  const cls = ['btn', variant !== 'default' && variant, size === 'sm' && 'sm', className].filter(Boolean).join(' ')
  return <Link {...rest} className={cls} />
}

/* ───────── Display ───────── */

export function Card({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={`card ${className ?? ''}`}>{children}</div>
}

export function CardHead({ title, children }: { title: ReactNode; children?: ReactNode }) {
  return (
    <div className="card-head">
      <h2>{title}</h2>
      <div className="row">{children}</div>
    </div>
  )
}

export type Tone = 'neutral' | 'ok' | 'warn' | 'danger' | 'info' | 'accent'

export function Badge({ tone = 'neutral', children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`badge ${tone === 'neutral' ? '' : tone}`}>{children}</span>
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="row" role="status">
      <span className="spinner" aria-hidden="true" />
      {label ? <span className="muted">{label}</span> : <span className="sr-only">Loading</span>}
    </span>
  )
}

export function Loading({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="empty">
      <Spinner label={label} />
    </div>
  )
}

export function EmptyState({
  icon,
  title,
  children,
  action,
}: {
  icon?: ReactNode
  title: string
  children?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="empty">
      {icon && <div className="icon">{icon}</div>}
      <h3>{title}</h3>
      {children && <p>{children}</p>}
      {action && <div style={{ marginTop: '0.9rem' }}>{action}</div>}
    </div>
  )
}

export function ErrorBanner({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  if (!error) return null
  const msg = error instanceof Error ? error.message : String(error)
  return (
    <div className="alert danger" role="alert">
      <IconAlert />
      <div className="grow">{msg}</div>
      {onRetry && (
        <Button size="sm" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  )
}

export function ProgressBar({ value }: { value: number }) {
  const v = Math.max(0, Math.min(100, value))
  return (
    <div className="progress" role="progressbar" aria-valuenow={v} aria-valuemin={0} aria-valuemax={100}>
      <span style={{ width: `${v}%` }} />
    </div>
  )
}

/* ───────── Form fields ───────── */

interface FieldProps extends InputHTMLAttributes<HTMLInputElement> {
  label: string
  help?: string
  error?: string
}

export function TextField({ label, help, error, id, ...rest }: FieldProps) {
  const auto = useId()
  const fid = id ?? auto
  return (
    <div className="field">
      <label htmlFor={fid}>{label}</label>
      <input id={fid} className="input" aria-invalid={error ? true : undefined} aria-describedby={help || error ? `${fid}-h` : undefined} {...rest} />
      {(error || help) && (
        <span id={`${fid}-h`} className="help" style={error ? { color: 'var(--danger)' } : undefined}>
          {error ?? help}
        </span>
      )}
    </div>
  )
}

export function Check({
  checked,
  onChange,
  children,
  disabled,
}: {
  checked: boolean
  onChange: (v: boolean) => void
  children: ReactNode
  disabled?: boolean
}) {
  return (
    <label className="check">
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span>{children}</span>
    </label>
  )
}

/** Tag-style input for e-mail addresses / names. Enter, comma or blur adds. With `suggestions`, people can be picked from a list. */
export function ChipInput({
  label,
  values,
  onChange,
  placeholder,
  help,
  id,
  suggestions,
}: {
  label: string
  values: string[]
  onChange: (v: string[]) => void
  placeholder?: string
  help?: string
  id?: string
  /** People that can be picked instead of typing an e-mail. */
  suggestions?: { name: string; email: string }[]
}) {
  const auto = useId()
  const fid = id ?? auto
  const listId = `${fid}-list`
  const [draft, setDraft] = useState('')
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(-1)

  const q = draft.trim().toLowerCase()
  const options = (suggestions ?? []).filter(
    (p) =>
      !values.some((v) => v.toLowerCase() === p.email.toLowerCase()) &&
      (!q || p.name.toLowerCase().includes(q) || p.email.toLowerCase().includes(q)),
  )
  const showList = open && options.length > 0

  const commit = (raw: string) => {
    const parts = raw.split(/[,;\s]+/).map((s) => s.trim()).filter(Boolean)
    if (!parts.length) return
    const next = [...values]
    for (const p of parts) if (!next.some((v) => v.toLowerCase() === p.toLowerCase())) next.push(p)
    onChange(next)
    setDraft('')
  }

  const pick = (email: string) => {
    commit(email)
    setActive(-1)
  }

  return (
    <div className="field suggest-host">
      <label htmlFor={fid}>{label}</label>
      <div className="chips">
        {values.map((v) => (
          <span key={v} className="chip">
            {v}
            <button type="button" aria-label={`Remove ${v}`} onClick={() => onChange(values.filter((x) => x !== v))}>
              ×
            </button>
          </span>
        ))}
        <input
          id={fid}
          value={draft}
          placeholder={values.length ? '' : placeholder}
          role={suggestions ? 'combobox' : undefined}
          aria-expanded={suggestions ? showList : undefined}
          aria-controls={suggestions ? listId : undefined}
          aria-autocomplete={suggestions ? 'list' : undefined}
          autoComplete="off"
          onFocus={() => setOpen(true)}
          onChange={(e) => {
            const val = e.target.value
            setOpen(true)
            setActive(-1)
            if (/[,;]$/.test(val)) commit(val)
            else setDraft(val)
          }}
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown' && options.length) {
              e.preventDefault()
              setOpen(true)
              setActive((i) => (i + 1) % options.length)
            } else if (e.key === 'ArrowUp' && options.length) {
              e.preventDefault()
              setActive((i) => (i <= 0 ? options.length - 1 : i - 1))
            } else if (e.key === 'Escape' && showList) {
              e.preventDefault()
              setOpen(false)
            } else if (e.key === 'Enter') {
              e.preventDefault()
              if (showList && active >= 0 && options[active]) pick(options[active].email)
              else commit(draft)
            } else if (e.key === 'Backspace' && !draft && values.length) {
              onChange(values.slice(0, -1))
            }
          }}
          onBlur={() => {
            commit(draft)
            setOpen(false)
            setActive(-1)
          }}
        />
      </div>
      {showList && (
        <ul id={listId} className="suggest" role="listbox" aria-label={`${label} suggestions`}>
          {options.map((p, i) => (
            <li
              key={p.email}
              role="option"
              aria-selected={i === active}
              className={i === active ? 'on' : undefined}
              // mousedown, not click: the input would lose focus (and add the half-typed text) first
              onMouseDown={(e) => {
                e.preventDefault()
                pick(p.email)
              }}
            >
              <strong>{p.name}</strong>
              <span className="muted small">{p.email}</span>
            </li>
          ))}
        </ul>
      )}
      {help && <span className="help">{help}</span>}
    </div>
  )
}

/* ───────── Tabs ───────── */

export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
}: {
  tabs: { id: T; label: ReactNode }[]
  value: T
  onChange: (v: T) => void
}) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button
          key={t.id}
          role="tab"
          className="tab"
          aria-selected={value === t.id}
          onClick={() => onChange(t.id)}
        >
          {t.label}
        </button>
      ))}
    </div>
  )
}

/* ───────── Modal / confirm ───────── */

export function Modal({
  title,
  children,
  footer,
  onClose,
}: {
  title: string
  children: ReactNode
  footer: ReactNode
  onClose: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const titleId = useId()
  // Keep the latest onClose without re-running the focus effect: parents often
  // pass a fresh function every render (e.g. while a background job polls).
  const closeRef = useRef(onClose)
  closeRef.current = onClose

  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null
    ref.current?.querySelector<HTMLElement>('button, [href], input, select, textarea')?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeRef.current()
    }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      prev?.focus?.()
    }
  }, [])

  return (
    <div className="modal-back" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby={titleId} ref={ref}>
        <div className="modal-head">
          <h2 id={titleId}>{title}</h2>
        </div>
        <div className="modal-body">{children}</div>
        <div className="modal-foot">{footer}</div>
      </div>
    </div>
  )
}

export function ConfirmDialog({
  title,
  message,
  confirmLabel,
  danger,
  busy,
  onConfirm,
  onCancel,
}: {
  title: string
  message: ReactNode
  confirmLabel: string
  danger?: boolean
  busy?: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  return (
    <Modal
      title={title}
      onClose={onCancel}
      footer={
        <>
          <Button onClick={onCancel}>Cancel</Button>
          <Button variant={danger ? 'danger' : 'primary'} busy={busy} onClick={onConfirm}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      {message}
    </Modal>
  )
}

/* ───────── Toasts ───────── */

interface Toast {
  id: number
  kind: 'ok' | 'danger' | 'info'
  text: string
}

const ToastCtx = createContext<{ push: (kind: Toast['kind'], text: string) => void } | null>(null)

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const nextId = useRef(1)

  const push = useCallback((kind: Toast['kind'], text: string) => {
    const id = nextId.current++
    setToasts((t) => [...t.slice(-3), { id, kind, text }])
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), kind === 'danger' ? 7000 : 4000)
  }, [])

  const value = useMemo(() => ({ push }), [push])
  return (
    <ToastCtx.Provider value={value}>
      {children}
      <div className="toasts" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.kind === 'info' ? '' : t.kind}`} role={t.kind === 'danger' ? 'alert' : 'status'}>
            {t.text}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  )
}

export function useToast() {
  const ctx = useContext(ToastCtx)
  if (!ctx) throw new Error('useToast must be used inside ToastProvider')
  return {
    ok: (t: string) => ctx.push('ok', t),
    info: (t: string) => ctx.push('info', t),
    error: (e: unknown) => ctx.push('danger', e instanceof Error ? e.message : String(e)),
  }
}

/* ───────── Domain badges ───────── */

const MEETING_TONE: Record<string, Tone> = {
  scheduled: 'info',
  in_progress: 'warn',
  completed: 'ok',
  uploaded: 'accent',
  cancelled: 'neutral',
}

const STATUS_LABEL: Record<string, string> = { uploaded: 'recorded', in_progress: 'in progress' }

export function MeetingStatusBadge({ status }: { status: string }) {
  return <Badge tone={MEETING_TONE[status] ?? 'neutral'}>{STATUS_LABEL[status] ?? status.replace('_', ' ')}</Badge>
}

const PRIORITY_TONE: Record<string, Tone> = { high: 'danger', medium: 'warn', low: 'neutral' }

export function PriorityBadge({ priority }: { priority: string }) {
  return <Badge tone={PRIORITY_TONE[priority] ?? 'neutral'}>{priority}</Badge>
}

export function isRateLimited(e: unknown): e is ApiError {
  return e instanceof ApiError && e.status === 429
}
