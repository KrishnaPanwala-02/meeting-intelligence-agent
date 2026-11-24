import { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { searchApi } from '../api/endpoints'
import type { SearchHit, SearchMeeting, SearchResults } from '../api/types'
import { IconSearch } from '../components/icons'
import { Badge, Card, EmptyState, ErrorBanner, Spinner } from '../components/ui'
import { fmtClock } from '../lib/format'
import { highlightTerms } from '../lib/text'

const KIND_LABEL: Record<SearchHit['kind'], string> = { title: 'Title', summary: 'Summary', task: 'Task', transcript: 'Transcript' }

/** Where a result opens: the right tab, and for a transcript line, that line. */
export function hitLink(meetingId: string, h: SearchHit): string {
  const base = `/meetings/${meetingId}`
  if (h.kind === 'transcript') return `${base}?tab=transcript${h.segment_id !== undefined ? `&seg=${h.segment_id}` : ''}`
  if (h.kind === 'task') return `${base}?tab=actions`
  if (h.kind === 'summary') return `${base}?tab=summary`
  return base
}

function Marked({ text, terms }: { text: string; terms: string[] }) {
  return (
    <>
      {highlightTerms(text, terms).map((p, i) => (p.match ? <mark key={i}>{p.text}</mark> : <span key={i}>{p.text}</span>))}
    </>
  )
}

function MeetingResult({ m, terms }: { m: SearchMeeting; terms: string[] }) {
  return (
    <section aria-label={m.title}>
      <div className="group-title">
        <Link to={`/meetings/${m.meeting_id}`}>
          <Marked text={m.title} terms={terms} />
        </Link>
        {m.date && <span className="muted"> · {m.date}</span>}
        <span className="muted"> · {m.hit_count} match{m.hit_count === 1 ? '' : 'es'}</span>
      </div>
      {m.hits.map((h, i) => (
        <Link key={i} className="hit" to={hitLink(m.meeting_id, h)}>
          <Badge tone={h.kind === 'task' ? 'accent' : h.kind === 'summary' ? 'ok' : 'neutral'}>{KIND_LABEL[h.kind]}</Badge>
          <div className="grow">
            {h.kind === 'transcript' && (
              <div className="small muted">
                {h.speaker ?? 'Speaker'} · {fmtClock(h.time ?? 0)}
              </div>
            )}
            <div>
              <Marked text={h.text} terms={terms} />
            </div>
            {h.kind === 'task' && (
              <div className="small muted">
                {h.assignee && h.assignee !== 'TBD' ? `${h.assignee} · ` : ''}
                {h.status?.replace('_', ' ')}
                {h.due ? ` · due ${h.due}` : ''}
              </div>
            )}
          </div>
        </Link>
      ))}
      {m.hit_count > m.hits.length && (
        <div className="small muted" style={{ padding: '0.4rem 1.25rem' }}>
          and {m.hit_count - m.hits.length} more in this meeting — <Link to={`/meetings/${m.meeting_id}?tab=transcript`}>open it</Link> and use its search box.
        </div>
      )}
    </section>
  )
}

export default function SearchPage() {
  const [params, setParams] = useSearchParams()
  const q = params.get('q') ?? ''
  const [text, setText] = useState(q)
  const [results, setResults] = useState<SearchResults | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const [loading, setLoading] = useState(false)
  const abort = useRef<AbortController | null>(null)
  const [attempt, setAttempt] = useState(0) // bumped by "Retry" to run the same search again

  // Typing updates the address after a short pause, so the back button and reloads keep the search.
  useEffect(() => {
    const t = setTimeout(() => {
      const next = text.trim()
      if (next !== q) setParams(next ? { q: next } : {}, { replace: true })
    }, 300)
    return () => clearTimeout(t)
  }, [text, q, setParams])

  // Run the search for what is in the address; a newer search cancels an older one.
  useEffect(() => {
    abort.current?.abort()
    setError(null)
    if (q.trim().length < 2) {
      setResults(null)
      setLoading(false)
      return
    }
    const ctl = new AbortController()
    abort.current = ctl
    setLoading(true)
    searchApi
      .search(q.trim(), ctl.signal)
      .then((r) => {
        if (!ctl.signal.aborted) setResults(r)
      })
      .catch((e: unknown) => {
        if (e instanceof DOMException && e.name === 'AbortError') return
        setError(e instanceof Error ? e : new Error(String(e)))
        setResults(null)
      })
      .finally(() => {
        if (!ctl.signal.aborted) setLoading(false)
      })
    return () => ctl.abort()
  }, [q, attempt])

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Search</h1>
          <p>Find a word or phrase in the titles, summaries, tasks and transcripts of your meetings. Put “quotes” around a phrase.</p>
        </div>
      </div>

      <Card>
        <div className="card-head">
          <div className="row grow" style={{ position: 'relative' }}>
            <input
              className="input"
              type="search"
              autoFocus
              aria-label="Search your meetings"
              placeholder="e.g. budget, “project report”, Rahul"
              value={text}
              maxLength={200}
              onChange={(e) => setText(e.target.value)}
            />
          </div>
          {loading && <Spinner />}
        </div>

        {error ? (
          <div className="card-body">
            <ErrorBanner error={error} onRetry={() => setAttempt((n) => n + 1)} />
          </div>
        ) : q.trim().length < 2 ? (
          <EmptyState icon={<IconSearch />} title="Search all your meetings">
            Type at least two letters. Only meetings you own or were invited to are searched.
          </EmptyState>
        ) : results && results.meetings.length === 0 && !loading ? (
          <EmptyState icon={<IconSearch />} title="No matches">
            Nothing in your meetings contains {results.terms.map((t) => `“${t}”`).join(' and ')}. Every word must appear in the same sentence, task or title; try fewer words.
          </EmptyState>
        ) : results ? (
          <>
            <div className="card-body muted small" role="status">
              {results.total} match{results.total === 1 ? '' : 'es'} in {results.meetings.length} meeting{results.meetings.length === 1 ? '' : 's'}
            </div>
            {results.meetings.map((m) => (
              <MeetingResult key={m.meeting_id} m={m} terms={results.terms} />
            ))}
          </>
        ) : null}
      </Card>
    </>
  )
}
