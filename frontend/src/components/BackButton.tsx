import { useLocation, useNavigate } from 'react-router-dom'
import { IconArrowLeft } from './icons'

/** Where "back" goes when there is no earlier page in this tab (e.g. a bookmarked link). */
function parentOf(path: string, fallback?: string): string {
  if (fallback) return fallback
  const parts = path.split('/').filter(Boolean)
  return parts.length > 1 ? `/${parts[0]}` : '/'
}

export default function BackButton({ fallback }: { fallback?: string }) {
  const nav = useNavigate()
  const { pathname } = useLocation()
  // react-router stores its position in the history entry: > 0 means a previous page exists.
  const hasHistory = ((window.history.state as { idx?: number } | null)?.idx ?? 0) > 0

  return (
    <button
      type="button"
      className="back-btn"
      onClick={() => (hasHistory ? nav(-1) : nav(parentOf(pathname, fallback), { replace: true }))}
    >
      <IconArrowLeft size={16} /> Back
    </button>
  )
}
