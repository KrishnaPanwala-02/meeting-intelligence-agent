import { Fragment, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { askApi, foldersApi, meetingsApi } from '../api/endpoints'
import type { AskSource } from '../api/types'
import { IconSparkle } from '../components/icons'
import { Button, Card, ConfirmDialog, ErrorBanner, Spinner, useToast } from '../components/ui'
import { fmtDateTime, fromServerUtc } from '../lib/format'
import { useAsync } from '../lib/useAsync'

interface Turn {
  id: string | number
  at?: string
  question: string
  answer?: string
  sources?: AskSource[]
  searched?: number
  error?: string
}

const GENERAL = ['What are my open tasks?', 'What did everyone commit to?', 'Summarise my latest meeting', 'What was decided in the last meeting?']
const FOLDER = ['What was decided across these meetings?', 'What is still open?', 'How has the project progressed?', 'Summarise each meeting']
const SCOPED = ['Summarise this meeting', 'What are the action items?', 'Who is responsible for what?', 'What was decided?']

/** The answer with its [M2]-style citations turned into links to the meetings they came from. */
export function AnswerText({ text, sources }: { text: string; sources: AskSource[] }) {
  const byTag = new Map(sources.map((s) => [s.tag, s]))
  const parts = text.split(/\[(M\d+)\]/g) // text, tag, text, tag, ...
  return (
    <>
      {parts.map((p, i) => {
        if (i % 2 === 0) return <Fragment key={i}>{p}</Fragment>
        const s = byTag.get(p)
        return s ? (
          <Link key={i} className="cite" to={`/meetings/${s.meeting_id}`} title={`${s.title}${s.date ? ` · ${s.date}` : ''}`}>
            {s.title}
          </Link>
        ) : null // a tag the model invented: drop it rather than show a broken reference
      })}
    </>
  )
}

export default function AskPage() {
  const [params, setParams] = useSearchParams()
  const folderParam = params.get('folder') ?? ''
  const scope = folderParam ? '' : (params.get('meeting') ?? '')      // one meeting, or one folder, or everything
  const folderScope = folderParam
  const toast = useToast()
  const [turns, setTurns] = useState<Turn[]>([])
  const [confirmClear, setConfirmClear] = useState(false)
  const [clearing, setClearing] = useState(false)
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const abort = useRef<AbortController | null>(null)
  const nextId = useRef(1)
  const end = useRef<HTMLDivElement>(null)

  const meetings = useAsync(() => meetingsApi.list({ limit: 200 }).then((r) => r.meetings.filter((m) => m.has_transcript || m.has_summary)), [])
  const scoped = meetings.data?.find((m) => m.id === scope)
  const folders = useAsync(() => foldersApi.list().catch(() => ({ count: 0, folders: [] })), [])
  const scopedFolder = folders.data?.folders.find((f) => f.id === folderScope)

  // The conversation is saved on the server: load it for this scope.
  const saved = useAsync(() => askApi.history(scope || undefined, folderScope || undefined), [scope, folderScope])
  useEffect(() => {
    if (!saved.data) return
    setTurns(
      saved.data.turns.map((t) => ({ id: t.id, at: t.created_at, question: t.question, answer: t.answer, sources: t.sources, searched: t.searched ?? undefined })),
    )
  }, [saved.data])

  useEffect(() => () => abort.current?.abort(), [])
  useEffect(() => {
    end.current?.scrollIntoView?.({ behavior: 'smooth', block: 'end' })
  }, [turns, busy])

  const suggestions = folderScope ? FOLDER : scope ? SCOPED : GENERAL
  const history = useMemo(
    () =>
      turns
        .filter((t) => t.answer)
        .slice(-3)
        .map((t) => ({ question: t.question, answer: (t.answer ?? '').slice(0, 2000) })),
    [turns],
  )

  async function ask(question: string, retryId?: string | number) {
    const q = question.trim()
    if (q.length < 3 || busy) return
    const id: string | number = retryId ?? nextId.current++
    setTurns((t) => (retryId !== undefined ? t.map((x) => (x.id === id ? { id, question: q } : x)) : [...t, { id, question: q }]))
    setText('')
    setBusy(true)
    const ctl = new AbortController()
    abort.current = ctl
    try {
      const r = await askApi.ask({ question: q, meeting_id: scope || undefined, folder_id: folderScope || undefined, history: retryId !== undefined ? history.filter((h) => h.question !== q) : history }, ctl.signal)
      setTurns((t) => t.map((x) => (x.id === id ? { ...x, id: r.id ?? id, at: r.created_at, answer: r.answer, sources: r.sources, searched: r.searched } : x)))
    } catch (e) {
      if (e instanceof DOMException && e.name === 'AbortError') {
        setTurns((t) => t.filter((x) => x.id !== id))
      } else {
        const msg =
          e instanceof ApiError && e.status === 429
            ? 'You have asked a lot of questions this hour. Please wait a little and try again.'
            : e instanceof Error
              ? e.message
              : 'Something went wrong.'
        setTurns((t) => t.map((x) => (x.id === id ? { ...x, error: msg } : x)))
      }
    } finally {
      setBusy(false)
      abort.current = null
    }
  }

  function submit(e: FormEvent) {
    e.preventDefault()
    void ask(text)
  }

  async function clearConversation() {
    setClearing(true)
    try {
      await askApi.clearHistory(scope || undefined, folderScope || undefined)
      setTurns([])
      toast.ok('Conversation deleted.')
    } catch (e) {
      toast.error(e)
    } finally {
      setClearing(false)
      setConfirmClear(false)
    }
  }

  function changeScope(value: string) {
    abort.current?.abort()
    setTurns([])
    if (value.startsWith('folder:')) setParams({ folder: value.slice('folder:'.length) }, { replace: true })
    else setParams(value ? { meeting: value } : {}, { replace: true })
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Ask your meetings</h1>
          <p>Ask a question in plain English. Answers come only from your own meetings, written by the AI model running on this computer.</p>
        </div>
        <div className="field" style={{ minWidth: 240 }}>
          <label htmlFor="scope">Search in</label>
          <select id="scope" className="input" value={folderScope ? `folder:${folderScope}` : scope} onChange={(e) => changeScope(e.target.value)}>
            <option value="">All my meetings</option>
            {(folders.data?.folders.length ?? 0) > 0 || folderScope ? (
              <optgroup label="A whole folder">
                {folderScope && !scopedFolder && <option value={`folder:${folderScope}`}>This folder</option>}
                {folders.data?.folders.map((f) => (
                  <option key={f.id} value={`folder:${f.id}`}>
                    {f.name}
                  </option>
                ))}
              </optgroup>
            ) : null}
            {scope && !scoped && <option value={scope}>This meeting</option>}
            {meetings.data?.map((m) => (
              <option key={m.id} value={m.id}>
                {m.title}
              </option>
            ))}
          </select>
        </div>
      </div>

      <Card>
        <div className="chat" aria-live="polite" aria-label="Conversation">
          {turns.length === 0 && saved.loading && <Spinner label="Loading your conversation…" />}
          {saved.error && !saved.data && <ErrorBanner error={saved.error} onRetry={saved.reload} />}
          {turns.length === 0 && !saved.loading && (
            <div className="chat-empty">
              <span style={{ color: 'var(--accent)' }}>
                <IconSparkle size={28} />
              </span>
              <strong>{scopedFolder ? `Ask about the folder “${scopedFolder.name}”` : scoped ? `Ask about “${scoped.title}”` : 'What would you like to know?'}</strong>
              <div className="row wrap" style={{ justifyContent: 'center' }}>
                {suggestions.map((s) => (
                  <button key={s} type="button" className="chip-btn" onClick={() => void ask(s)} disabled={busy}>
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}

          {turns.map((t) => (
            <div key={t.id} className="stack tight">
              <div className="bubble you">{t.question}</div>
              {t.at && <div className="small muted" style={{ alignSelf: 'flex-end' }}>{fmtDateTime(fromServerUtc(t.at))}</div>}
              {t.error ? (
                <div className="bubble bot" role="alert">
                  <span style={{ color: 'var(--danger)' }}>{t.error}</span>{' '}
                  <Button size="sm" onClick={() => void ask(t.question, t.id)} disabled={busy}>
                    Try again
                  </Button>
                </div>
              ) : t.answer === undefined ? (
                <div className="bubble bot">
                  <Spinner label="Thinking… a local model can take up to a minute" />
                </div>
              ) : (
                <div className="bubble bot">
                  <div style={{ whiteSpace: 'pre-wrap' }}>
                    <AnswerText text={t.answer} sources={t.sources ?? []} />
                  </div>
                  {t.sources && t.sources.length > 0 && (
                    <div className="small muted" style={{ marginTop: '0.5rem' }}>
                      {t.sources[0].cited ? 'Sources: ' : 'No source was cited. Searched: '}
                      {t.sources.map((s, i) => (
                        <Fragment key={s.tag}>
                          {i > 0 && ' · '}
                          <Link to={`/meetings/${s.meeting_id}`}>{s.title}</Link>
                          {s.date && ` (${s.date})`}
                        </Fragment>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          ))}
          <div ref={end} />
        </div>

        <form className="chat-input" onSubmit={submit}>
          <input
            className="input"
            aria-label="Your question"
            placeholder={scoped ? `Ask about ${scoped.title}…` : 'e.g. What did Rahul commit to this week?'}
            value={text}
            maxLength={500}
            onChange={(e) => setText(e.target.value)}
            disabled={busy}
          />
          {busy ? (
            <Button type="button" onClick={() => abort.current?.abort()}>
              Stop
            </Button>
          ) : (
            <Button variant="primary" type="submit" disabled={text.trim().length < 3}>
              Ask
            </Button>
          )}
          {turns.length > 0 && !busy && (
            <Button type="button" variant="ghost" onClick={() => setConfirmClear(true)}>
              Clear
            </Button>
          )}
        </form>
      </Card>
      <p className="muted small" style={{ marginTop: '0.8rem' }}>
        The AI can make mistakes, so check important answers in the linked meetings. Your conversation is saved, and only you can see it.
      </p>
      {confirmClear && (
        <ConfirmDialog
          title="Delete this conversation?"
          message="Your saved questions and answers here are removed. This cannot be undone."
          confirmLabel="Delete conversation"
          danger
          busy={clearing}
          onConfirm={() => void clearConversation()}
          onCancel={() => setConfirmClear(false)}
        />
      )}
    </>
  )
}
