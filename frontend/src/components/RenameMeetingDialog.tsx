import { useState, type FormEvent } from 'react'
import { meetingsApi } from '../api/endpoints'
import { Button, Modal, TextField, useToast } from './ui'

const MAX = 200

/** Change a meeting's name. Used from the meetings list and from the meeting page. */
export default function RenameMeetingDialog({
  meetingId,
  title,
  onClose,
  onSaved,
}: {
  meetingId: string
  title: string
  onClose: () => void
  onSaved: (title: string) => void
}) {
  const toast = useToast()
  const [value, setValue] = useState(title)
  const [busy, setBusy] = useState(false)
  const clean = value.split(/\s+/).filter(Boolean).join(' ')
  const problem = !clean ? 'Give the meeting a name.' : clean.length > MAX ? `At most ${MAX} characters.` : null

  async function save(e?: FormEvent) {
    e?.preventDefault()
    if (problem) return
    if (clean === title) return onClose()
    setBusy(true)
    try {
      const r = await meetingsApi.rename(meetingId, clean)
      toast.ok('Meeting renamed.')
      onSaved(r.title)
    } catch (err) {
      toast.error(err)
      setBusy(false)
    }
  }

  return (
    <Modal
      title="Rename meeting"
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" busy={busy} disabled={!!problem} onClick={() => void save()}>
            Save
          </Button>
        </>
      }
    >
      <form onSubmit={save}>
        <TextField label="Meeting name" autoFocus value={value} maxLength={MAX + 50} onChange={(e) => setValue(e.target.value)} error={value && problem ? problem : undefined} />
      </form>
    </Modal>
  )
}
