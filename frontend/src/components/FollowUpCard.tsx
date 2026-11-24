import { Card, CardHead } from './ui'

const LABELS = ['Follows up', 'New', 'Still open'] as const

/** "Follows up: …\nNew: …\nStill open: …" as labelled rows. Anything else is shown as plain text. */
export function parseFollowUp(text: string): { label: string; body: string }[] {
  const rows: { label: string; body: string }[] = []
  for (const line of text.split('\n')) {
    const hit = LABELS.find((l) => line.startsWith(`${l}:`))
    if (hit) rows.push({ label: hit, body: line.slice(hit.length + 1).trim() })
    else if (line.trim()) rows.push({ label: '', body: line.trim() })
  }
  return rows
}

/** How this meeting continues the earlier meetings in its folder. */
export default function FollowUpCard({ text, folderName, number }: { text: string; folderName?: string; number?: number | null }) {
  const rows = parseFollowUp(text)
  if (rows.length === 0) return null
  return (
    <Card>
      <CardHead title={folderName ? `Continues from earlier meetings in ${folderName}` : 'Continues from earlier meetings'} />
      <div className="card-body stack" style={{ gap: '0.6rem' }}>
        {rows.map((r, i) => (
          <div key={i}>
            {r.label && <strong style={{ marginRight: '0.4rem' }}>{r.label}</strong>}
            <span>{r.body}</span>
          </div>
        ))}
        <p className="muted small" style={{ margin: 0 }}>
          Written by the model from the earlier meetings’ summaries and open tasks{number ? ` (this is Meeting ${number})` : ''}. It can be wrong, so check it against the meetings.
        </p>
      </div>
    </Card>
  )
}
