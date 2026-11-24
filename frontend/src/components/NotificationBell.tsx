import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { notificationsApi } from '../api/endpoints'
import type { AppNotification } from '../api/types'
import { ago } from '../lib/format'
import { useAsync, usePoll } from '../lib/useAsync'
import { IconBell } from './icons'
import { Button, useToast } from './ui'

const POLL_MS = 60_000

/** The bell: what needs your attention (new tasks, shared meetings, deadlines, replies). */
export default function NotificationBell() {
  const nav = useNavigate()
  const toast = useToast()
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  const list = useAsync(() => notificationsApi.list(), [])
  usePoll(() => list.reload(), POLL_MS, true)

  useEffect(() => {
    if (!open) return
    list.reload()
    const away = (e: MouseEvent) => !root.current?.contains(e.target as Node) && setOpen(false)
    const esc = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false)
    document.addEventListener('mousedown', away)
    document.addEventListener('keydown', esc)
    return () => {
      document.removeEventListener('mousedown', away)
      document.removeEventListener('keydown', esc)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const items = list.data?.items ?? []
  const unread = list.data?.unread ?? 0

  const patch = (fn: (n: AppNotification[], unread: number) => { items: AppNotification[]; unread: number }) =>
    list.setData((d) => (d ? fn(d.items, d.unread) : d))

  async function openItem(n: AppNotification) {
    setOpen(false)
    if (!n.read) {
      patch((all, u) => ({ items: all.map((x) => (x.id === n.id ? { ...x, read: true } : x)), unread: Math.max(0, u - 1) }))
      void notificationsApi.markRead([n.id]).catch(() => undefined)
    }
    if (n.link) nav(n.link)
  }

  async function markAll() {
    try {
      await notificationsApi.markRead()
      patch((all) => ({ items: all.map((x) => ({ ...x, read: true })), unread: 0 }))
    } catch (e) {
      toast.error(e)
    }
  }

  async function dismiss(n: AppNotification) {
    try {
      await notificationsApi.remove(n.id)
      patch((all, u) => ({ items: all.filter((x) => x.id !== n.id), unread: n.read ? u : Math.max(0, u - 1) }))
    } catch (e) {
      toast.error(e)
    }
  }

  return (
    <div className="bell-wrap" ref={root}>
      <button
        type="button"
        className="bell"
        aria-haspopup="true"
        aria-expanded={open}
        aria-label={unread ? `Notifications, ${unread} unread` : 'Notifications'}
        onClick={() => setOpen((v) => !v)}
      >
        <IconBell />
        {unread > 0 && <span className="bell-count">{unread > 9 ? '9+' : unread}</span>}
      </button>
      {open && (
        <div className="notif-panel" role="region" aria-label="Notifications">
          <div className="row between notif-head">
            <strong>Notifications</strong>
            <Button size="sm" variant="ghost" onClick={() => void markAll()} disabled={unread === 0}>
              Mark all read
            </Button>
          </div>
          {list.error && !list.data ? (
            <p className="muted small" style={{ padding: '0.8rem 1rem' }}>
              Could not load notifications. {list.error.message}
            </p>
          ) : items.length === 0 ? (
            <p className="muted small" style={{ padding: '1.2rem 1rem', textAlign: 'center' }}>
              {list.loading && !list.data ? 'Loading…' : 'You are all caught up.'}
            </p>
          ) : (
            <ul className="notif-list">
              {items.map((n) => (
                <li key={n.id} className={n.read ? 'read' : 'unread'}>
                  <button type="button" className="notif-item" onClick={() => void openItem(n)}>
                    {!n.read && <span className="dot" aria-label="unread" />}
                    <span className="grow">
                      <span className="notif-title">{n.title}</span>
                      {n.body && <span className="notif-body">{n.body}</span>}
                      <span className="muted small">{ago(n.created_at)}</span>
                    </span>
                  </button>
                  <button type="button" className="icon-btn" aria-label={`Dismiss: ${n.title}`} onClick={() => void dismiss(n)}>
                    ×
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
