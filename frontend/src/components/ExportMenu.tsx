import { useEffect, useRef, useState } from 'react'
import { exportApi } from '../api/endpoints'
import type { ActionItem, Meeting, Summary, Transcript } from '../api/types'
import { downloadBlob, downloadText, meetingToHtml, meetingToMarkdown, printHtml, safeFilename } from '../lib/export'
import ShareDialog from './ShareDialog'
import { IconDownload } from './icons'
import { Button, useToast } from './ui'

/** Download or share a meeting: Markdown, PDF (print), calendar file, e-mail. */
export default function ExportMenu({
  meeting,
  summary,
  actions,
  transcript,
}: {
  meeting: Meeting
  summary?: Summary | null
  actions?: ActionItem[] | null
  transcript?: Transcript | null
}) {
  const toast = useToast()
  const [open, setOpen] = useState(false)
  const [share, setShare] = useState(false)
  const root = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const away = (e: MouseEvent) => !root.current?.contains(e.target as Node) && setOpen(false)
    const esc = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false)
    document.addEventListener('mousedown', away)
    document.addEventListener('keydown', esc)
    return () => {
      document.removeEventListener('mousedown', away)
      document.removeEventListener('keydown', esc)
    }
  }, [open])

  const name = safeFilename(meeting.title)
  // Errors, whether thrown straight away or later, are shown as a message rather than escaping.
  const run = (fn: () => void | Promise<void>) => () => {
    setOpen(false)
    void (async () => {
      try {
        await fn()
      } catch (e) {
        toast.error(e)
      }
    })()
  }

  return (
    <div className="menu-wrap" ref={root}>
      <Button aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <IconDownload /> Export
      </Button>
      {open && (
        <div className="menu" role="menu">
          <button role="menuitem" onClick={run(() => downloadText(`${name}.md`, meetingToMarkdown(meeting, summary, actions, transcript), 'text/markdown'))}>
            Markdown file (.md)
          </button>
          <button
            role="menuitem"
            onClick={run(() => {
              if (!printHtml(meetingToHtml(meeting, summary, actions, transcript)))
                throw new Error('The browser blocked the print window. Allow pop-ups for this site and try again.')
            })}
          >
            PDF (print or save as PDF)
          </button>
          <button role="menuitem" onClick={run(async () => downloadBlob(`${name}.ics`, await exportApi.meetingCalendar(meeting.id)))}>
            Add to calendar (.ics)
          </button>
          {summary && (
            <button
              role="menuitem"
              onClick={() => {
                setOpen(false)
                setShare(true)
              }}
            >
              E-mail the summary…
            </button>
          )}
        </div>
      )}
      {share && summary && <ShareDialog meeting={meeting} summary={summary} actions={actions ?? []} onClose={() => setShare(false)} />}
    </div>
  )
}
