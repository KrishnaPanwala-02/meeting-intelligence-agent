import { useRef, useState } from 'react'
import { fmtBytes } from '../lib/format'
import Recorder from './Recorder'
import { Button } from './ui'

const ACCEPT = 'audio/*,.m4a,.mp3,.wav,.webm,.ogg,.flac,.aac,.mp4'

/** Two ways to give a voice sample: record now, or import an audio file from this device. */
export default function VoiceInput({ onUse }: { onUse: (file: File) => void }) {
  const pick = useRef<HTMLInputElement>(null)
  const [note, setNote] = useState<string | null>(null)

  function chosen(files: FileList | null) {
    const f = files?.[0]
    if (!f) return
    if (f.type && !f.type.startsWith('audio/') && !f.type.startsWith('video/')) {
      setNote('That is not an audio file. Choose a recording such as .m4a, .mp3 or .wav.')
      return
    }
    setNote(null)
    onUse(f)
  }

  return (
    <div className="stack tight">
      <Recorder onUse={onUse} />
      <div className="row" style={{ justifyContent: 'center', gap: '.6rem' }}>
        <span className="muted small">or</span>
        <Button type="button" onClick={() => pick.current?.click()}>
          Import audio from this device
        </Button>
        <input
          ref={pick}
          type="file"
          accept={ACCEPT}
          hidden
          data-testid="voice-file"
          aria-label="Import a voice recording from this device"
          onChange={(e) => {
            chosen(e.target.files)
            e.target.value = ''
          }}
        />
      </div>
      {note && <div className="alert danger" role="alert">{note}</div>}
    </div>
  )
}

export function ChosenVoice({ file, onRemove }: { file: File; onRemove: () => void }) {
  return (
    <div className="row between" role="status">
      <span className="small">
        <strong>{file.name}</strong> · {fmtBytes(file.size)}
      </span>
      <Button size="sm" type="button" onClick={onRemove}>
        Choose a different one
      </Button>
    </div>
  )
}
