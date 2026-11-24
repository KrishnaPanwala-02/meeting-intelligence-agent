import type { ActionItem, Meeting, Summary, Transcript } from '../api/types'
import { fmtClock, fmtDue } from './format'

/** Speakers in order of first appearance, e.g. ['Speaker 1', 'Speaker 2']. */
export function speakerOrder(t: Transcript | undefined): string[] {
  const seen: string[] = []
  for (const s of t?.segments ?? []) if (s.speaker && !seen.includes(s.speaker)) seen.push(s.speaker)
  return seen
}

/** The speaker's colour class (sp0…sp5), stable for a given transcript. */
export function speakerClass(order: string[], speaker: string | null): string {
  if (!speaker) return 'sp5'
  const i = order.indexOf(speaker)
  return `sp${(i < 0 ? 5 : i) % 6}`
}

/** Plain-text transcript with timestamps and speaker names. */
export function transcriptToText(t: Transcript): string {
  return t.segments
    .map((s) => `[${fmtClock(s.start_time)}] ${s.speaker ? `${s.speaker}: ` : ''}${s.text}`)
    .join('\n')
}

/** A shareable Markdown write-up of a meeting. */
export function meetingToMarkdown(
  meeting: Meeting,
  summary?: Summary | null,
  actions?: ActionItem[] | null,
  transcript?: Transcript | null,
): string {
  const out: string[] = [`# ${meeting.title}`, '']
  if (meeting.participants.length) out.push(`**Participants:** ${meeting.participants.join(', ')}`, '')

  if (summary) {
    out.push('## Summary', '', summary.executive_summary, '')
    if (summary.key_points.length) {
      out.push('## Key points', '')
      for (const k of summary.key_points) out.push(`- **${k.topic}:** ${k.description}`)
      out.push('')
    }
    if (summary.decisions.length) {
      out.push('## Decisions', '')
      for (const d of summary.decisions) out.push(`- ${d.decision}${d.owner && d.owner !== 'TBD' ? ` (${d.owner})` : ''}`)
      out.push('')
    }
    if (summary.next_steps.length) {
      out.push('## Next steps', '')
      for (const n of summary.next_steps) out.push(`- ${n}`)
      out.push('')
    }
  }

  if (actions?.length) {
    out.push('## Action items', '')
    for (const a of actions) {
      const box = a.status === 'completed' ? '[x]' : '[ ]'
      const who = a.assignee && a.assignee !== 'TBD' ? ` — ${a.assignee}` : ''
      out.push(`- ${box} ${a.task}${who} (${fmtDue(a.due_date)})`)
    }
    out.push('')
  }

  if (transcript?.segments.length) {
    out.push('## Transcript', '', transcriptToText(transcript), '')
  }
  return out.join('\n')
}

export function escapeHtml(text: unknown): string {
  return String(text ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

/** A plain-text write-up for an e-mail body: the summary, decisions, next steps and tasks. */
export function summaryToPlainText(meeting: Meeting, summary?: Summary | null, actions?: ActionItem[] | null): string {
  const out: string[] = [`Summary of “${meeting.title}”`, '']
  if (summary) {
    out.push(summary.executive_summary, '')
    if (summary.decisions.length) {
      out.push('Decisions:')
      for (const d of summary.decisions) out.push(`- ${d.decision}${d.owner && d.owner !== 'TBD' ? ` (${d.owner})` : ''}`)
      out.push('')
    }
    if (summary.next_steps.length) {
      out.push('Next steps:')
      for (const n of summary.next_steps) out.push(`- ${n}`)
      out.push('')
    }
  }
  if (actions?.length) {
    out.push('Tasks:')
    for (const a of actions) {
      const who = a.assignee && a.assignee !== 'TBD' ? ` — ${a.assignee}` : ''
      out.push(`- ${a.task}${who} (${fmtDue(a.due_date)})${a.status === 'completed' ? ' [done]' : ''}`)
    }
  }
  return out.join('\n').trim() + '\n'
}

/** A self-contained, print-friendly page for "Save as PDF". Every piece of text is escaped. */
export function meetingToHtml(
  meeting: Meeting,
  summary?: Summary | null,
  actions?: ActionItem[] | null,
  transcript?: Transcript | null,
): string {
  const e = escapeHtml
  const list = (items: string[]) => (items.length ? `<ul>${items.map((i) => `<li>${i}</li>`).join('')}</ul>` : '')
  const body: string[] = [`<h1>${e(meeting.title)}</h1>`]
  if (meeting.participants.length) body.push(`<p class="meta"><b>Participants:</b> ${e(meeting.participants.join(', '))}</p>`)
  if (summary) {
    body.push(`<h2>Summary</h2><p>${e(summary.executive_summary)}</p>`)
    if (summary.key_points.length) body.push(`<h2>Key points</h2>${list(summary.key_points.map((k) => `<b>${e(k.topic)}:</b> ${e(k.description)}`))}`)
    if (summary.decisions.length)
      body.push(`<h2>Decisions</h2>${list(summary.decisions.map((d) => `${e(d.decision)}${d.owner && d.owner !== 'TBD' ? ` <i>(${e(d.owner)})</i>` : ''}`))}`)
    if (summary.next_steps.length) body.push(`<h2>Next steps</h2>${list(summary.next_steps.map(e))}`)
  }
  if (actions?.length) {
    body.push(
      '<h2>Action items</h2><table><tr><th></th><th>Task</th><th>Owner</th><th>Deadline</th></tr>' +
        actions
          .map(
            (a) =>
              `<tr><td>${a.status === 'completed' ? '&#10003;' : '&#9744;'}</td><td>${e(a.task)}</td><td>${e(a.assignee && a.assignee !== 'TBD' ? a.assignee : '')}</td><td>${e(fmtDue(a.due_date))}</td></tr>`,
          )
          .join('') +
        '</table>',
    )
  }
  if (transcript?.segments.length) {
    body.push(
      '<h2>Transcript</h2>' +
        transcript.segments
          .map((s) => `<p class="line"><span class="t">${e(fmtClock(s.start_time))}</span> ${s.speaker ? `<b>${e(s.speaker)}:</b> ` : ''}${e(s.text)}</p>`)
          .join(''),
    )
  }
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>${e(meeting.title)}</title><style>
body{font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;color:#111;max-width:46rem;margin:2rem auto;padding:0 1rem}
h1{font-size:1.7rem;margin:0 0 .3rem}h2{font-size:1.1rem;margin:1.6rem 0 .4rem;border-bottom:1px solid #ccc;padding-bottom:.2rem}
.meta{color:#555}ul{padding-left:1.2rem}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccc;padding:.3rem .5rem;text-align:left;vertical-align:top}
.line{margin:.25rem 0}.t{color:#666;font-family:ui-monospace,monospace;font-size:.85em}
@media print{body{margin:0;max-width:none}h2{break-after:avoid}tr,.line{break-inside:avoid}}
</style></head><body>${body.join('')}</body></html>`
}

/** Open the print dialog for a page of HTML (choose "Save as PDF"). False if the browser blocked the window. */
export function printHtml(html: string): boolean {
  const w = window.open('', '_blank')
  if (!w) return false
  w.document.open()
  w.document.write(html)
  w.document.close()
  w.focus()
  setTimeout(() => w.print(), 300)
  return true
}

/** Trigger a browser download of a Blob (for files fetched with the bearer token). */
export function downloadBlob(filename: string, blob: Blob) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

/** Trigger a browser download of text content. */
export function downloadText(filename: string, text: string, type = 'text/plain') {
  const url = URL.createObjectURL(new Blob([text], { type: `${type};charset=utf-8` }))
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export function safeFilename(title: string): string {
  return title.replace(/[^\w\- ]+/g, '').trim().replace(/\s+/g, '-').slice(0, 60) || 'meeting'
}

/** Split text into plain and matching parts for highlighting a search term. */
export function highlightParts(text: string, term: string): { text: string; match: boolean }[] {
  const q = term.trim()
  if (!q) return [{ text, match: false }]
  const re = new RegExp(`(${q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi')
  return text
    .split(re)
    .filter((p) => p !== '')
    .map((p) => ({ text: p, match: p.toLowerCase() === q.toLowerCase() }))
}
