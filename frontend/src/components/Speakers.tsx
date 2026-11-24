import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { meetingsApi, peopleApi, voicesApi } from '../api/endpoints'
import type { Person, SpeakerInfo, VoiceProfile } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { useAsync } from '../lib/useAsync'
import { Button, Card, CardHead, Check, ErrorBanner, useToast } from './ui'

const mmss = (t: number) => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')}`

/** Plays short parts of the meeting recording (fetched once, on the first play). */
function useClipPlayer(meetingId: string) {
  const audio = useRef<HTMLAudioElement | null>(null)
  const url = useRef<string | null>(null)
  const stopAt = useRef(0)
  const [playing, setPlaying] = useState<string | null>(null)
  const [loading, setLoading] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(
    () => () => {
      audio.current?.pause()
      if (url.current) URL.revokeObjectURL(url.current)
    },
    [],
  )

  async function toggle(key: string, start: number, end: number) {
    setError(null)
    if (playing === key) {
      audio.current?.pause()
      setPlaying(null)
      return
    }
    try {
      if (!url.current) {
        setLoading(key)
        url.current = URL.createObjectURL(await meetingsApi.audio(meetingId))
      }
      if (!audio.current) {
        const a = new Audio()
        a.addEventListener('timeupdate', () => {
          if (a.currentTime >= stopAt.current) {
            a.pause()
            setPlaying(null)
          }
        })
        a.addEventListener('ended', () => setPlaying(null))
        audio.current = a
      }
      const a = audio.current
      if (a.src !== url.current) a.src = url.current
      a.currentTime = start
      stopAt.current = end
      setPlaying(key)
      await a.play()
    } catch (err) {
      setPlaying(null)
      setError(err instanceof Error ? err.message : 'The recording could not be played')
    } finally {
      setLoading(null)
    }
  }
  return { playing, loading, error, toggle }
}

type ClipPlayer = ReturnType<typeof useClipPlayer>

/** "Listen" buttons: a few of the speaker's own turns, with what was said. */
function Clips({ speaker, player }: { speaker: SpeakerInfo; player: ClipPlayer }) {
  const clips = speaker.clips ?? []
  if (clips.length === 0) return null
  const active = clips.findIndex((_, i) => player.playing === `${speaker.label}:${i}`)
  const shown = clips[active >= 0 ? active : 0]
  return (
    <div style={{ flexBasis: '100%' }}>
      <div className="row wrap" style={{ gap: '0.4rem' }}>
        <span className="muted small">Listen to {speaker.named ? speaker.label : 'this voice'}:</span>
        {clips.map((c, i) => {
          const key = `${speaker.label}:${i}`
          const on = player.playing === key
          return (
            <Button
              key={key}
              size="sm"
              type="button"
              busy={player.loading === key}
              aria-pressed={on}
              aria-label={`${on ? 'Stop' : 'Play'} ${speaker.label} at ${mmss(c.start)}`}
              onClick={() => void player.toggle(key, c.start, c.end)}
            >
              {on ? '■' : '▶'} {mmss(c.start)}
            </Button>
          )
        })}
      </div>
      {shown.text && <div className="muted small">“{shown.text}” <span>({mmss(shown.start)})</span></div>}
    </div>
  )
}

function SpeakerRow({
  meetingId,
  speaker,
  suggestionsId,
  canWrite,
  profiles,
  people,
  player,
  onSaved,
}: {
  meetingId: string
  speaker: SpeakerInfo
  suggestionsId: string
  canWrite: boolean
  profiles: VoiceProfile[]
  people: Person[]
  player: ClipPlayer | null
  onSaved: () => void
}) {
  const toast = useToast()
  // A name the speaker said about themselves is filled in for you to confirm or correct.
  const suggestion = speaker.named ? null : (speaker.suggestion ?? null)
  const [name, setName] = useState(speaker.named ? speaker.label : (suggestion?.name ?? ''))
  const [fixSpelling, setFixSpelling] = useState(true)
  const [noMatch, setNoMatch] = useState(false) // "No, that is not them" for the voice suggestion
  const voiceMatch = speaker.named || noMatch ? null : (speaker.voice_match ?? null)
  const [remember, setRemember] = useState(true)
  const [email, setEmail] = useState('')
  const [busy, setBusy] = useState(false)
  const [revoke, setRevoke] = useState(true)
  // A person already saved, or registered, with an e-mail is shared with automatically.
  const same = (a: string, b: string) => a.trim().replace(/\s+/g, ' ').toLowerCase() === b.trim().replace(/\s+/g, ' ').toLowerCase()
  const savedEmail =
    profiles.find((p) => same(p.name, name))?.email ?? people.find((p) => same(p.name, name))?.email ?? null
  // Changing the name of a speaker who was already named is fixing a wrong match.
  const correcting = speaker.named && name.trim() !== '' && !same(name, speaker.label)
  const previousEmail = correcting ? (speaker.email ?? null) : null
  // The transcript spells the name differently from the one chosen: offer to correct it everywhere.
  const offerFix = !!suggestion && fixSpelling && name.trim() !== '' && name.trim().toLowerCase() !== suggestion.heard.toLowerCase()
  const showFix = !!suggestion && name.trim() !== '' && name.trim().toLowerCase() !== suggestion.heard.toLowerCase()
  const changed = (name.trim() !== '' && name.trim() !== speaker.label) || (name.trim() !== '' && email.trim() !== '')

  async function confirmVoice() {
    if (!voiceMatch) return
    setBusy(true)
    try {
      const r = await meetingsApi.renameSpeaker(meetingId, { label: speaker.label, name: voiceMatch.name, remember_voice: true })
      toast.ok(`Saved as ${r.label}.${r.voice_saved ? ' Their voice will be recognised even better next time.' : ''}`)
      onSaved()
    } catch (err) {
      toast.error(err)
      setBusy(false)
    }
  }

  async function save(e: FormEvent) {
    e.preventDefault()
    if (!changed) return
    setBusy(true)
    try {
      const r = await meetingsApi.renameSpeaker(meetingId, {
        label: speaker.label,
        name: name.trim(),
        remember_voice: remember && speaker.has_voice,
        email: email.trim() || undefined,
        replace_heard: offerFix ? suggestion!.heard : undefined,
        revoke_previous: previousEmail && revoke ? true : undefined,
      })
      toast.ok(
        [
          r.text_fixed ? `Fixed “${suggestion?.heard}” in ${r.text_fixed} place${r.text_fixed === 1 ? '' : 's'}.` : '',
          r.voice_saved ? `Saved. ${r.label}’s voice will be recognised next time.` : `Saved as ${r.label}.`,
          r.invited ? `${r.invited} can now sign in and see this meeting.` : '',
          r.revoked ? `${r.revoked} no longer has access.` : '',
        ]
          .filter(Boolean)
          .join(' '),
      )
      setEmail('')
      onSaved()
    } catch (err) {
      toast.error(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="row wrap" onSubmit={save} style={{ padding: '0.5rem 0', gap: '0.6rem' }}>
      <div style={{ minWidth: 150 }}>
        <strong>{speaker.label}</strong>
        <div className="muted small">
          {Math.round(speaker.seconds)}s · {speaker.words} words
        </div>
        {speaker.named && speaker.email && <div className="muted small">{speaker.email}</div>}
      </div>
      {canWrite ? (
        <>
          <input
            className="input sm"
            aria-label={`Name for ${speaker.label}`}
            placeholder="Who is this? e.g. Rahul Patel"
            list={suggestionsId}
            maxLength={100}
            value={name}
            onChange={(e) => setName(e.target.value)}
            style={{ width: 240 }}
          />
          <input
            className="input sm"
            type="email"
            aria-label={`E-mail for ${speaker.label}`}
            placeholder={savedEmail ? `Saved: ${savedEmail}` : 'E-mail (optional)'}
            title="Gives this person access to the meeting so they can sign in and see their tasks"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            style={{ width: 210 }}
          />
          {speaker.has_voice && (
            <Check checked={remember} onChange={setRemember}>
              Remember this voice
            </Check>
          )}
          <Button size="sm" variant="primary" type="submit" busy={busy} disabled={!changed}>
            Save
          </Button>
        </>
      ) : null}
      {player && <Clips speaker={speaker} player={player} />}
      {canWrite && voiceMatch && (
        <div className="alert info" role="status" style={{ flexBasis: '100%' }}>
          <div className="grow">
            Sounds like <strong>{voiceMatch.name}</strong> ({Math.round(voiceMatch.score * 100)}% match{voiceMatch.strong ? '' : ', so check it'}). Is this them?
          </div>
          <Button size="sm" variant="primary" type="button" busy={busy} onClick={() => void confirmVoice()}>
            Yes, that is {voiceMatch.name.split(' ')[0]}
          </Button>
          <Button size="sm" type="button" disabled={busy} onClick={() => setNoMatch(true)}>
            No
          </Button>
        </div>
      )}
      {canWrite && correcting && (
        <div className="small" style={{ flexBasis: '100%' }}>
          {savedEmail ? `${name.trim()} will be given access (${savedEmail}). ` : 'Add their e-mail so they can see the meeting. '}
          {previousEmail && (
            <Check checked={revoke} onChange={setRevoke}>
              Remove {speaker.label}’s access ({previousEmail}): they were not in this meeting
            </Check>
          )}
        </div>
      )}
      {canWrite && suggestion && (
        <div className="small muted" style={{ flexBasis: '100%', marginLeft: 0 }}>
          Heard: “{suggestion.quote}”
          {suggestion.known ? ` — matches ${suggestion.name}.` : ' — check the spelling.'}
        </div>
      )}
      {canWrite && showFix && (
        <div style={{ flexBasis: '100%' }}>
          <Check checked={fixSpelling} onChange={setFixSpelling}>
            Also change “{suggestion!.heard}” to “{name.trim()}” in the transcript, summary and tasks
          </Check>
        </div>
      )}
    </form>
  )
}

/** Name the people in a recording; named voices are recognised in later meetings. */
export default function SpeakersCard({
  meetingId,
  canWrite,
  hasAudio,
  analyzing,
  refreshKey,
  onChanged,
  onAnalyze,
}: {
  meetingId: string
  canWrite: boolean
  hasAudio: boolean
  analyzing: boolean
  refreshKey: number
  onChanged: () => void
  onAnalyze: () => void
}) {
  const { user } = useAuth()
  const listId = useId()
  const speakers = useAsync(() => meetingsApi.speakers(meetingId), [meetingId, refreshKey])
  const profiles = useAsync(() => voicesApi.list().catch(() => ({ count: 0, profiles: [] })), [refreshKey])
  const people = useAsync(() => peopleApi.list().catch(() => ({ count: 0, people: [] as Person[] })), [])

  const player = useClipPlayer(meetingId)
  const list = speakers.data?.speakers ?? []
  if (speakers.error && !speakers.data) return <ErrorBanner error={speakers.error} />
  if (list.length === 0) return null

  const suggestions = Array.from(
    new Set([
      ...(people.data?.people.map((p) => p.name) ?? []),
      ...(profiles.data?.profiles.map((p) => p.name) ?? []),
      ...(user?.name ? [user.name] : []),
    ]),
  )
  const needsVoices = canWrite && hasAudio && list.some((s) => !s.has_voice && !s.named)

  return (
    <Card>
      <CardHead title="Who is speaking" />
      <div className="card-body">
        <datalist id={listId}>
          {suggestions.map((n) => (
            <option key={n} value={n} />
          ))}
        </datalist>
        {canWrite && (
          <p className="muted small" style={{ marginTop: 0 }}>
            Voices are matched to every member automatically, and the people recognised are given access. Name anyone left, or fix a wrong match by changing the name. The voice is remembered for future meetings.
          </p>
        )}
        {list.map((s) => (
          <SpeakerRow
            key={s.label}
            meetingId={meetingId}
            speaker={s}
            suggestionsId={listId}
            canWrite={canWrite}
            profiles={profiles.data?.profiles ?? []}
            people={people.data?.people ?? []}
            player={hasAudio ? player : null}
            onSaved={onChanged}
          />
        ))}
        {player.error && (
          <div className="alert warn" role="alert" style={{ marginTop: '0.6rem' }}>
            <div className="grow">Could not play the recording: {player.error}</div>
          </div>
        )}
        {needsVoices && (
          <div className="alert warn" role="status" style={{ marginTop: '0.8rem' }}>
            <div className="grow">
              Voices were not recorded for this meeting, so they cannot be remembered yet. Analysing takes about a minute and does not change the text.
            </div>
            <Button size="sm" onClick={onAnalyze} busy={analyzing} disabled={analyzing}>
              Analyse voices
            </Button>
          </div>
        )}
      </div>
    </Card>
  )
}
